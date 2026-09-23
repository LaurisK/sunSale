"""Recovery for a price sensor whose integration dropped it and never retries.

Twice in three days (2026-09-20, 2026-09-23) the HACS nordpool integration lost
its sensor for good. It fetches inside ``async_added_to_hass`` and lets the
exception escape, so *any* transient failure of that one call at startup — no
DNS the first time, an upstream HTTP 502 the second — aborts the entity add.
The entity then sits in the registry as ``restored`` / ``unavailable`` forever:
no ``ConfigEntryNotReady``, no retry, and ``config_entries`` still reports the
entry as ``loaded``, so nothing looks wrong from the outside.

sunSale notices long before the user does — an empty feed collapses the priced
slots to whatever yesterday it has persisted, and the inverter is left with no
plan at all. Reloading the offending config entry fixes it in one step, which
is a thing this integration can simply do.

So: reload it, a few times, spaced out — and if that does not bring the sensor
back, stop trying and raise a repair issue, because at that point the fault is
not the one this module knows how to fix and a silent retry loop would only
hide it.

The decision is a pure function (:func:`decide`); this module's class holds the
HA calls. State is deliberately in memory only: a restart is itself a recovery
attempt, and persisting the "gave up" flag across one would stop the reload
that might have worked.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from ..contract.const import (
    CONF_NORDPOOL_ENTITY,
    DOMAIN,
    PRICE_FEED_DEAD_GRACE_MINUTES,
    PRICE_FEED_MAX_RELOADS,
    PRICE_FEED_RELOAD_INTERVAL_MINUTES,
    PRICE_SOURCE_FIXED,
)
from ..contract.install_capabilities import CAP_PRICES, resolve_install_capabilities
from ..ha_state import available_state

_LOGGER = logging.getLogger(__name__)

ACTION_NONE = "none"
ACTION_RELOAD = "reload"
ACTION_RAISE_ISSUE = "raise_issue"
ACTION_CLEAR_ISSUE = "clear_issue"

# When each target config entry was last reloaded, shared by every watchdog in
# the process. A multi-device install runs one coordinator per sunSale entry and
# they commonly watch the *same* price sensor, so without this they would both
# reload it in the same cycle — the second call landing while the first reload
# is still setting the integration up. Keyed by the reloaded entry, so installs
# on different price sources never see each other.
_LAST_RELOAD: dict[str, datetime] = {}


def reset_reload_history() -> None:
    """Forget every recorded reload. For tests; the process holds no other state."""
    _LAST_RELOAD.clear()


@dataclass(frozen=True)
class WatchdogState:
    """What the watchdog has seen and done about the current outage.

    A fresh instance means "the feed is healthy and nothing is outstanding",
    which is what recovery resets to.
    """
    dead_since: datetime | None = None
    reloads: int = 0
    last_reload: datetime | None = None
    issue_raised: bool = False


def decide(
    state: WatchdogState,
    *,
    alive: bool,
    now: datetime,
    grace: timedelta,
    retry_after: timedelta,
    max_reloads: int,
) -> tuple[WatchdogState, str]:
    """Return the next state and the single action to take this cycle.

    The grace period is what keeps this from fighting normal operation: an HA
    restart, a brief integration reload or a source that is slow to publish all
    look like a dead feed for a cycle or two, and none of them want a reload on
    top. Only an outage that outlasts the grace gets acted on.

    Args:
        state: State carried from the previous cycle.
        alive: Whether the price sensor currently has a usable state.
        now: Cycle timestamp.
        grace: How long the feed must stay dead before the first reload.
        retry_after: Minimum spacing between reload attempts.
        max_reloads: Reloads to try before giving up and raising the issue.

    Returns:
        ``(next_state, action)`` where action is one of the ``ACTION_*``
        constants. Exactly one action per cycle — the caller never has to
        sequence two.
    """
    if alive:
        if state == WatchdogState():
            return state, ACTION_NONE
        # Recovered: forget the outage, and retract the issue if one is up.
        return WatchdogState(), (
            ACTION_CLEAR_ISSUE if state.issue_raised else ACTION_NONE
        )

    if state.dead_since is None:
        return replace(state, dead_since=now), ACTION_NONE
    if now - state.dead_since < grace:
        return state, ACTION_NONE

    if state.reloads >= max_reloads:
        if state.issue_raised:
            return state, ACTION_NONE
        return replace(state, issue_raised=True), ACTION_RAISE_ISSUE

    if state.last_reload is not None and now - state.last_reload < retry_after:
        return state, ACTION_NONE
    return (
        replace(state, reloads=state.reloads + 1, last_reload=now),
        ACTION_RELOAD,
    )


class PriceFeedWatchdog:
    """Watches one install's price sensor and reloads the integration behind it."""

    def __init__(
        self,
        hass: Any,
        own_entry_id: str,
        *,
        grace: timedelta | None = None,
        retry_after: timedelta | None = None,
        max_reloads: int = PRICE_FEED_MAX_RELOADS,
    ) -> None:
        """Initialise the watchdog for one sunSale config entry.

        Args:
            hass: Home Assistant instance.
            own_entry_id: This sunSale entry's id — never reloaded, since
                reloading ourselves would restart the loop doing the watching.
            grace: Override for the dead-feed grace period.
            retry_after: Override for the spacing between reloads.
            max_reloads: Override for the attempts before giving up.
        """
        self._hass = hass
        self._own_entry_id = own_entry_id
        self._grace = grace or timedelta(minutes=PRICE_FEED_DEAD_GRACE_MINUTES)
        self._retry_after = retry_after or timedelta(
            minutes=PRICE_FEED_RELOAD_INTERVAL_MINUTES
        )
        self._max_reloads = max_reloads
        self._state = WatchdogState()

    @property
    def state(self) -> WatchdogState:
        """Return the current watchdog state (diagnostics and tests)."""
        return self._state

    @property
    def issue_id(self) -> str:
        """Return this install's repair-issue id.

        Keyed by the sunSale entry rather than the price entity so a
        multi-device install raises one issue per install, matching how the
        rest of its state is scoped.
        """
        return f"price_feed_dead_{self._own_entry_id}"

    async def async_tick(
        self, config: Mapping[str, Any], price_source: str, now: datetime,
    ) -> None:
        """Check the price sensor and act once on what it finds.

        Args:
            config: Merged config-entry data + options.
            price_source: The *effective* price source from ``SunSaleConfig``.
                Not the raw config key: a tariff with both sides fixed reads no
                market sensor however the entry is configured.
            now: Cycle timestamp.
        """
        entity_id = self._watched_entity(config, price_source)
        if entity_id is None:
            # Nothing to watch (fixed prices, prices switched off, or no sensor
            # mapped). Drop any outstanding issue rather than leaving a stale
            # one up after a reconfigure.
            if self._state.issue_raised:
                self._clear_issue()
            self._state = WatchdogState()
            return

        alive = available_state(self._hass, entity_id) is not None
        self._state, action = decide(
            self._state,
            alive=alive,
            now=now,
            grace=self._grace,
            retry_after=self._retry_after,
            max_reloads=self._max_reloads,
        )

        if action == ACTION_RELOAD:
            await self._reload_owner(entity_id)
        elif action == ACTION_RAISE_ISSUE:
            self._raise_issue(entity_id)
        elif action == ACTION_CLEAR_ISSUE:
            _LOGGER.info("Price sensor %s is back; clearing repair issue", entity_id)
            self._clear_issue()

    # -- internals ----------------------------------------------------------

    def _watched_entity(
        self, config: Mapping[str, Any], price_source: str,
    ) -> str | None:
        """Return the price sensor to watch, or None when there is nothing to watch.

        Args:
            config: Merged config-entry data + options.
            price_source: The effective price source.

        Returns:
            The configured price entity id, or None for a fixed-price install,
            one with prices switched off, or one with no sensor mapped.
        """
        if not resolve_install_capabilities(config)[CAP_PRICES]:
            return None
        if price_source == PRICE_SOURCE_FIXED:
            return None
        return config.get(CONF_NORDPOOL_ENTITY) or None

    def _owner_entry_id(self, entity_id: str) -> str | None:
        """Return the config entry that owns the price sensor, if any.

        Args:
            entity_id: The price sensor's entity id.

        Returns:
            The owning entry id, or None when the entity is not registry-backed
            (a YAML template sensor, say) or is one of our own — neither can be
            fixed by the reload this module performs.
        """
        registry = er.async_get(self._hass)
        entry = registry.async_get(entity_id)
        owner = getattr(entry, "config_entry_id", None) if entry is not None else None
        if not owner or owner == self._own_entry_id:
            return None
        return owner

    async def _reload_owner(self, entity_id: str) -> None:
        """Reload the config entry behind the price sensor.

        A sensor with no reloadable owner goes straight to the repair issue:
        there is no point counting down attempts for a reload that cannot
        happen.

        Args:
            entity_id: The price sensor's entity id.
        """
        owner = self._owner_entry_id(entity_id)
        if owner is None:
            _LOGGER.warning(
                "Price sensor %s is unavailable and has no reloadable config "
                "entry behind it; raising a repair issue instead", entity_id,
            )
            self._state = replace(
                self._state, reloads=self._max_reloads, issue_raised=True,
            )
            self._raise_issue(entity_id)
            return

        now = self._state.last_reload
        previous = _LAST_RELOAD.get(owner)
        if now is not None and previous is not None and now - previous < self._retry_after:
            # A sibling install already reloaded this entry moments ago. It
            # still counts as this watchdog's attempt — both are waiting on the
            # same sensor, so they should give up together rather than one
            # carrying on reloading after the other has raised the issue.
            _LOGGER.debug(
                "Config entry %s was reloaded at %s by another sunSale install; "
                "counting it as this one's attempt %d of %d",
                owner, previous, self._state.reloads, self._max_reloads,
            )
            return

        _LOGGER.warning(
            "Price sensor %s has been unavailable since %s; reloading config "
            "entry %s (attempt %d of %d)",
            entity_id, self._state.dead_since, owner,
            self._state.reloads, self._max_reloads,
        )
        if now is not None:
            _LAST_RELOAD[owner] = now
        try:
            await self._hass.config_entries.async_reload(owner)
        except Exception:  # a failed reload is just an attempt that did not work
            _LOGGER.exception("Reload of config entry %s failed", owner)

    def _raise_issue(self, entity_id: str) -> None:
        """Raise the repair issue for a price feed the reloads did not revive.

        Args:
            entity_id: The price sensor's entity id.
        """
        _LOGGER.error(
            "Price sensor %s is still unavailable after %d reload attempts; "
            "sunSale has no market prices and cannot plan",
            entity_id, self._state.reloads,
        )
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="price_feed_dead",
            translation_placeholders={
                "entity_id": entity_id,
                "reloads": str(self._state.reloads),
            },
        )

    def _clear_issue(self) -> None:
        """Retract the repair issue once the feed is healthy again."""
        ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)
