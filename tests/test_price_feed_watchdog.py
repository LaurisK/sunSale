"""Tests for the dead-price-feed watchdog.

The failure it answers is silent by construction: the sensor stays in the
registry, the config entry still reports ``loaded``, and only sunSale notices
that the prices stopped. So the risk runs both ways — a watchdog that acts too
eagerly reloads someone's working integration every restart, and one that gives
up too quietly leaves the inverter planning on nothing. Both bounds are pinned
below.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.const import (
    CONF_NORDPOOL_ENTITY,
    CONF_PRICE_SOURCE,
    PRICE_SOURCE_FIXED,
    PRICE_SOURCE_NORDPOOL,
)
from custom_components.sun_sale.contract.install_capabilities import (
    CAP_PRICES,
    CONF_INSTALL_CAPABILITIES,
)
from custom_components.sun_sale.orchestration import price_feed_watchdog as wd

_NOW = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)
_GRACE = timedelta(minutes=15)
_RETRY = timedelta(minutes=10)
_ENTITY = "sensor.nordpool_kwh_lt_eur_3_10_0"
_OWNER = "01K4NBN0FJGWE5BJ6E3CAC4XQE"
_OWN = "01KW65QG726VBHN8A8SYHAW4AA"


def _decide(state, *, alive, now, max_reloads=3):
    """Run the decision with this module's fixed timings."""
    return wd.decide(
        state, alive=alive, now=now,
        grace=_GRACE, retry_after=_RETRY, max_reloads=max_reloads,
    )


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_a_healthy_feed_is_left_alone():
    """The common case must be inert — no state, no action, every cycle."""
    state, action = _decide(wd.WatchdogState(), alive=True, now=_NOW)
    assert state == wd.WatchdogState()
    assert action == wd.ACTION_NONE


def test_a_brief_outage_is_ridden_out():
    """An HA restart looks exactly like a dead feed for a cycle or two, and
    reloading on top of one would be worse than waiting."""
    state, action = _decide(wd.WatchdogState(), alive=False, now=_NOW)
    assert state.dead_since == _NOW
    assert action == wd.ACTION_NONE

    state, action = _decide(state, alive=False, now=_NOW + timedelta(minutes=14))
    assert action == wd.ACTION_NONE
    assert state.reloads == 0


def test_an_outage_past_the_grace_triggers_one_reload():
    """This is the whole point: the fix is a reload, applied without the user."""
    state = wd.WatchdogState(dead_since=_NOW)
    state, action = _decide(state, alive=False, now=_NOW + _GRACE)
    assert action == wd.ACTION_RELOAD
    assert state.reloads == 1
    assert state.last_reload == _NOW + _GRACE


def test_reloads_are_spaced_and_capped_then_the_issue_goes_up():
    """Three attempts, ten minutes apart, then stop and say so."""
    state = wd.WatchdogState(dead_since=_NOW)
    now = _NOW + _GRACE
    actions = []
    for _ in range(40):  # far more cycles than attempts
        state, action = _decide(state, alive=False, now=now)
        actions.append(action)
        now += timedelta(minutes=5)

    assert actions.count(wd.ACTION_RELOAD) == 3
    assert actions.count(wd.ACTION_RAISE_ISSUE) == 1
    # The issue is raised once and never re-raised, and nothing follows it.
    assert actions.index(wd.ACTION_RAISE_ISSUE) > max(
        i for i, a in enumerate(actions) if a == wd.ACTION_RELOAD
    )
    assert set(actions[actions.index(wd.ACTION_RAISE_ISSUE) + 1:]) == {wd.ACTION_NONE}


def test_recovery_retracts_the_issue_and_forgets_the_outage():
    """The next outage must get its own full budget of attempts."""
    state = wd.WatchdogState(
        dead_since=_NOW, reloads=3, last_reload=_NOW, issue_raised=True,
    )
    state, action = _decide(state, alive=True, now=_NOW + timedelta(hours=1))
    assert action == wd.ACTION_CLEAR_ISSUE
    assert state == wd.WatchdogState()


def test_recovery_without_an_issue_raises_nothing_to_clear():
    """A reload that worked should leave no trace in the repairs panel."""
    state = wd.WatchdogState(dead_since=_NOW, reloads=1, last_reload=_NOW)
    state, action = _decide(state, alive=True, now=_NOW + timedelta(minutes=20))
    assert action == wd.ACTION_NONE
    assert state == wd.WatchdogState()


# ---------------------------------------------------------------------------
# The HA side
# ---------------------------------------------------------------------------


def _hass(state: str | None) -> MagicMock:
    """Build a fake hass whose price sensor reports ``state`` (None = missing)."""
    hass = MagicMock()
    hass.states.get.return_value = (
        None if state is None else SimpleNamespace(state=state, attributes={})
    )
    hass.config_entries.async_reload = AsyncMock()
    return hass


def _config(**overrides) -> dict:
    """Build a config dict for a market-priced install."""
    return {
        CONF_NORDPOOL_ENTITY: _ENTITY,
        CONF_PRICE_SOURCE: PRICE_SOURCE_NORDPOOL,
        **overrides,
    }


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """Point the price sensor at a config entry that is not sunSale's own."""
    wd.reset_reload_history()
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(config_entry_id=_OWNER)
    monkeypatch.setattr(wd.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(wd.ir, "async_create_issue", MagicMock())
    monkeypatch.setattr(wd.ir, "async_delete_issue", MagicMock())
    return registry


def _watchdog(hass) -> wd.PriceFeedWatchdog:
    """Build a watchdog with this module's fixed timings."""
    return wd.PriceFeedWatchdog(
        hass, _OWN, grace=_GRACE, retry_after=_RETRY, max_reloads=3,
    )


async def _run(watchdog, hass, *, cycles: int, start=_NOW, step=timedelta(minutes=5),
               config=None):
    """Tick the watchdog ``cycles`` times, five minutes apart."""
    now = start
    for _ in range(cycles):
        await watchdog.async_tick(config or _config(), PRICE_SOURCE_NORDPOOL, now)
        now += step


@pytest.mark.asyncio
async def test_a_dead_sensor_gets_its_owning_entry_reloaded():
    """End to end, on the shape of the live 2026-09-23 outage."""
    hass = _hass("unavailable")
    watchdog = _watchdog(hass)
    await _run(watchdog, hass, cycles=4)  # 0, 5, 10, 15 min → one reload at 15

    hass.config_entries.async_reload.assert_awaited_once_with(_OWNER)
    assert watchdog.state.reloads == 1


@pytest.mark.asyncio
async def test_a_healthy_sensor_is_never_reloaded():
    """The bound that matters for everyone whose feed is fine."""
    hass = _hass("0.124")
    watchdog = _watchdog(hass)
    await _run(watchdog, hass, cycles=20)

    hass.config_entries.async_reload.assert_not_awaited()
    assert watchdog.state == wd.WatchdogState()


@pytest.mark.asyncio
async def test_sunsales_own_entry_is_never_reloaded(_registry):
    """Reloading ourselves would restart the loop doing the watching, so a
    self-owned sensor goes to the repair issue instead."""
    _registry.async_get.return_value = SimpleNamespace(config_entry_id=_OWN)
    hass = _hass("unavailable")
    watchdog = _watchdog(hass)
    await _run(watchdog, hass, cycles=4)

    hass.config_entries.async_reload.assert_not_awaited()
    wd.ir.async_create_issue.assert_called_once()
    assert watchdog.state.issue_raised


@pytest.mark.asyncio
async def test_a_sensor_with_no_config_entry_goes_straight_to_the_issue(_registry):
    """A YAML template sensor cannot be reloaded; counting down three attempts
    at a reload that cannot happen would only delay telling the user."""
    _registry.async_get.return_value = None
    hass = _hass("unavailable")
    watchdog = _watchdog(hass)
    await _run(watchdog, hass, cycles=4)

    hass.config_entries.async_reload.assert_not_awaited()
    wd.ir.async_create_issue.assert_called_once()


@pytest.mark.asyncio
async def test_a_failed_reload_is_contained():
    """The reload is best-effort; the cycle behind it carries the inverter."""
    hass = _hass("unavailable")
    hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))
    watchdog = _watchdog(hass)
    await _run(watchdog, hass, cycles=4)

    assert watchdog.state.reloads == 1  # counted as an attempt, not a crash


@pytest.mark.asyncio
async def test_the_issue_is_retracted_once_the_prices_come_back():
    """Live, the reload fixed it — the repairs panel must not keep the scar."""
    hass = _hass("unavailable")
    watchdog = _watchdog(hass)
    await _run(watchdog, hass, cycles=20)
    assert watchdog.state.issue_raised

    hass.states.get.return_value = SimpleNamespace(state="0.124", attributes={})
    await watchdog.async_tick(_config(), PRICE_SOURCE_NORDPOOL, _NOW + timedelta(hours=3))

    wd.ir.async_delete_issue.assert_called_once()
    assert watchdog.state == wd.WatchdogState()


@pytest.mark.asyncio
@pytest.mark.parametrize("config,source", [
    (_config(**{CONF_NORDPOOL_ENTITY: ""}), PRICE_SOURCE_NORDPOOL),
    (_config(), PRICE_SOURCE_FIXED),
    (_config(**{CONF_INSTALL_CAPABILITIES: {CAP_PRICES: False}}), PRICE_SOURCE_NORDPOOL),
])
async def test_installs_with_no_market_sensor_are_not_watched(config, source):
    """No sensor mapped, a fixed tariff, or prices switched off — nothing to
    reload, and an unavailable entity means nothing in any of them."""
    hass = _hass(None)
    watchdog = _watchdog(hass)
    now = _NOW
    for _ in range(20):
        await watchdog.async_tick(config, source, now)
        now += timedelta(minutes=5)

    hass.config_entries.async_reload.assert_not_awaited()
    wd.ir.async_create_issue.assert_not_called()
    assert watchdog.state == wd.WatchdogState()


@pytest.mark.asyncio
async def test_two_installs_watching_one_sensor_reload_it_once():
    """The author's own box runs two sunSale entries off a single Nordpool
    sensor. Without a shared cooldown the second reload lands while the first
    is still setting the integration up."""
    hass = _hass("unavailable")
    first, second = _watchdog(hass), _watchdog(hass)

    now = _NOW
    for _ in range(4):  # 0, 5, 10, 15 min — both cross the grace together
        await first.async_tick(_config(), PRICE_SOURCE_NORDPOOL, now)
        await second.async_tick(_config(), PRICE_SOURCE_NORDPOOL, now)
        now += timedelta(minutes=5)

    hass.config_entries.async_reload.assert_awaited_once_with(_OWNER)
    # Both spent the attempt, so they exhaust their budgets together rather
    # than one reloading on after the other has given up.
    assert first.state.reloads == second.state.reloads == 1
