"""Inverter control module — observer + dispatcher behind one entry point.

The coordinator calls ``InverterControlModule.tick(...)`` once per cycle after
the DAG run. The module does three things in this fixed order:

  1. **Observe.** Compare this cycle's ``InverterModeReading`` against the
     last entry of the rolling history. If the decoded mode changed, append
     a new ``InverterModeChange`` and prune samples older than the start of
     yesterday (local time). The result is what feeds the chart's mode-band
     history.

  2. **Plan.** Look up the current ``Schedule`` slot and resolve its target
     mode into the driver's opaque spec via ``driver.spec_for(mode)``.

  3. **Act (conditional).** When ``automation_enabled`` is True, or when
     ``mode_override`` is set (operator intent always reaches the inverter),
     call ``driver.apply_mode(target, spec)``. With the switch off AND no
     override set, the module is observer-only — history grows, plan is
     exposed, but no writes are issued.

  4. **Verify (commanded-change only).** When the resolved target differs
     from the last-commanded mode, the dispatch is a *force-write* (bypasses
     the per-register cache idempotency in ``apply_mode``) and a verify
     callback is scheduled via ``async_call_later`` ~30 s later. The
     callback re-reads the driver's control surface and checks every row the
     commanded mode targets matches its observed value. On
     match, ``verify_state`` flips to ``ok``. On mismatch, the module
     force-writes once more and schedules a second verify; if that also
     mismatches, ``verify_state`` becomes ``mismatch`` and is surfaced on
     the diagnostic sensor so the operator can see that the write is not
     reaching the inverter (Modbus chain issue, mode lock, etc.).

  5. **Reconcile (slow drift recovery).** The verify loop only lives ~60 s.
     Past that, an engaged mode (``verify_state == "ok"``) is re-checked
     against the inverter every tick; if its registers stay drifted for
     ``_DRIFT_RECONCILE_CYCLES`` consecutive ticks, the unchanged target is
     re-commanded (back through step 4). A False→True ``automation_enabled``
     transition arms the same re-command so re-enabling automation re-asserts
     the scheduled mode. This is what makes "a drifted write is recovered"
     true beyond the verify window — without it, a held mode that drifts (or
     is changed at the inverter screen) would show red on the panel and never
     self-correct.

Persistence is the coordinator's responsibility — this module takes the
existing history in, returns the updated one, and the coordinator writes
it back through a ``PersistentStore``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from ..contract.models import (
    InverterModeChange,
    InverterModeHistory,
    InverterModeReading,
    Schedule,
    ScheduleSlot,
    StorageMode,
)
from .driver import ControlRow, InverterControlDriver

_LOGGER = logging.getLogger(__name__)

# Verify-tick cadence after a commanded-mode change (seconds).
#  - INITIAL: first poll fires almost immediately so a fast-engaging inverter
#    is detected without an arbitrary wait.
#  - POLL_INTERVAL: subsequent polls within the window — keeps the UX badge
#    snappy (~5 s) without saturating the Modbus chain.
#  - WINDOW: total time we'll wait per attempt before either retrying the
#    write (first time) or declaring the write stuck (second time). Sized to
#    outlast solis_modbus's slowest poll cycle (~30 s) so a mismatch verdict
#    means the inverter really hasn't applied the command.
_VERIFY_INITIAL_DELAY_S = 2
_VERIFY_POLL_INTERVAL_S = 5
_VERIFY_WINDOW_S = 30

# Slow drift-reconciliation threshold. Once the verify loop has confirmed a
# mode (``verify_state == "ok"``), the registers are re-checked every
# coordinator tick. If they disagree with the commanded spec for this many
# *consecutive* ticks, the mode is re-commanded. Two ticks (~5 min cadence →
# ~10 min) debounces a single transient readback glitch while still catching a
# real drift — an inverter-screen change or a register that slipped — long
# after the verify loop's ~60 s window has closed.
_DRIFT_RECONCILE_CYCLES = 2

# RC keep-alive cadence (seconds). The Solis RC active-power function expires
# ~5 min after the last *engaging* write — register 43282's 30-min request
# does not latch on S6 firmware (Pho3niX90/solis_modbus#352), so the inverter
# uses its ~5-min default. A 2026-06-15 production capture measured grid export
# collapsing ~4–5 min into a held discharge, which the 5-min coordinator tick
# is too slow to beat. A dedicated heartbeat re-arms the function (full
# selector→timeout→setpoint rewrite via ``refresh_rc``) every 3 min — ~2 min of
# margin under the ~5-min expiry — independent of the tick cadence.
_RC_KEEPALIVE_INTERVAL_S = 180


class InverterControlModule:
    """Observes inverter mode, maintains the rolling history, and (when enabled) dispatches."""

    def __init__(
        self,
        driver: InverterControlDriver,
        local_tz: Any,
        hass: HomeAssistant | None = None,
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        """Initialise with the platform control driver.

        Args:
            driver: Platform control driver owning spec composition, the control
                surface, observed-mode decoding, and the per-cycle mode
                observation. Built by ``make_inverter_driver`` and injected by
                the coordinator — this module is platform-neutral and never
                names a concrete driver.
            local_tz: Local timezone used to compute the start of yesterday
                for history pruning.
            hass: Home Assistant instance, used to schedule the verify-tick
                callbacks after a commanded-mode change. May be ``None`` in
                unit tests, in which case verify-ticks are disabled (a
                ``MagicMock`` works equally well — the module just calls
                ``async_call_later`` on it).
            on_state_change: Optional callback fired whenever the verify loop
                mutates engagement state *outside* a coordinator tick (i.e.
                from a scheduled verify-tick). Lets the coordinator push a
                fresh entity state so the panel's per-register colours track
                the verify loop in real time instead of lagging to the next
                5-minute cycle. ``None`` disables the push (unit tests).
        """
        self._hass = hass
        self._on_state_change = on_state_change
        self._local_tz = local_tz
        self._driver: InverterControlDriver = driver
        self._last_applied_mode: StorageMode | None = None
        # Phase 0 visibility: per-tick dispatch outcome surfaced on the
        # ObservedInverterModeSensor diagnostic attributes.
        self._last_dispatch_outcome: str | None = None
        self._last_dispatch_target: StorageMode | None = None
        self._last_dispatch_at: datetime | None = None
        self._last_automation_enabled: bool | None = None
        # Diagnostic mirror of the automation flag at the most recent dispatch
        # (tick *or* override). Distinct from ``_last_automation_enabled``,
        # which is the tick-only transition bookkeeping driving
        # ``_reassert_next``: the direct override path deliberately leaves that
        # untouched, so a separate field is needed to keep this diagnostic fresh
        # after a button press instead of reporting the previous tick's value.
        self._automation_enabled_at_last_dispatch: bool | None = None
        # Phase 2: commanded-mode tracking + verify loop. ``last_commanded_*``
        # are our own truth (what we asked the inverter to do); the verify
        # loop reads back from solis_modbus a beat later to confirm.
        self._last_commanded_mode: StorageMode | None = None
        self._last_commanded_at: datetime | None = None
        self._verify_state: str | None = None  # pending / ok / mismatch
        self._last_verify_at: datetime | None = None
        self._last_verify_observed_reg: int | None = None
        self._verify_cancel = None  # cancel-callback from async_call_later
        self._verify_retried: bool = False
        # Start of the current verify window — used to decide between "still
        # polling" and "window exhausted, retry or give up".
        self._verify_window_started_at: datetime | None = None
        # Per-register desired-vs-observed comparison for the last-commanded
        # mode. Refreshed every tick and every verify-tick; drives the panel's
        # green/amber/red register readout. Empty until the first command.
        self._register_status: list[ControlRow] = []
        # Slow drift reconciliation. ``_consecutive_drift_cycles`` counts
        # consecutive holding ticks where the verified mode's registers no
        # longer match; at ``_DRIFT_RECONCILE_CYCLES`` we re-command.
        # ``_reassert_next`` is armed by a False→True ``automation_enabled``
        # transition so re-enabling automation re-asserts the scheduled mode
        # (the inverter may have drifted while automation was paused).
        self._consecutive_drift_cycles: int = 0
        self._reassert_next: bool = False
        # Serializes every path that issues register writes or mutates the
        # commanded/verify state: ``_apply_dispatch`` (tick + dispatch_override)
        # and the verify-tick retry. Each ``apply_mode`` spans multiple Modbus
        # awaits, so without this a new command landing mid-flight could
        # interleave with — and be overwritten by — a stale verify retry (the
        # retry captured the old commanded mode and would re-write it *after*
        # the new one, then cancel the new command's verify). Holding the lock
        # across each path's full plan→act→verify-arm body makes the last write
        # win and keeps the commanded mode and its verify in lockstep.
        self._dispatch_lock = asyncio.Lock()
        # RC keep-alive heartbeat. A dedicated timer re-arms the inverter-side
        # RC function every ``_RC_KEEPALIVE_INTERVAL_S`` while an RC-backed mode
        # is the commanded target — armed in ``_force_write_and_verify`` when
        # the commanded spec carries an RC setpoint, cancelled when a non-RC
        # mode is commanded or on shutdown. It supersedes the 5-min tick's
        # ``_maybe_refresh_rc`` for liveness (the tick is too slow to beat the
        # ~5-min RC expiry); the tick refresh is kept as a secondary re-arm at
        # cycle boundaries. Holds ``_rc_keepalive_cancel``, the unsubscribe
        # from ``async_track_time_interval``.
        self._rc_keepalive_cancel: Callable[[], None] | None = None

    @callback
    def shutdown(self) -> None:
        """Tear down the module so no callbacks survive a config-entry unload.

        Cancels any pending ``async_call_later`` verify-tick and drops the
        ``on_state_change`` callback. Without this, a verify-tick scheduled
        before unload would still fire afterwards and its retry path would
        issue real switch/number service calls against the (still-valid)
        solis_modbus entities — ghost Modbus writes from a torn-down entry —
        while ``_notify_state_change`` would push into a coordinator that has
        already been removed from ``hass.data``. Idempotent: safe to call
        more than once.
        """
        if self._verify_cancel is not None:
            try:
                self._verify_cancel()
            except Exception:  # noqa: BLE001 — cancel must never raise
                _LOGGER.debug(
                    "inverter_control: shutdown verify-cancel raised — ignoring",
                    exc_info=True,
                )
            self._verify_cancel = None
        self._cancel_rc_keepalive()
        self._on_state_change = None

    async def tick(
        self,
        now: datetime,
        schedule: Schedule | None,
        reading: InverterModeReading,
        history: InverterModeHistory,
        automation_enabled: bool,
        mode_override: StorageMode | None = None,
    ) -> InverterModeHistory:
        """Run one observe → plan → act cycle and return the updated history.

        Args:
            now: Cycle timestamp (tz-aware).
            schedule: Latest DAG-produced Schedule, or ``None`` when the
                pipeline hasn't produced one yet.
            reading: This cycle's observed inverter state.
            history: Existing mode-change history; coordinator-owned.
            automation_enabled: When ``True``, the current slot's target is
                pushed to the inverter via ``apply_mode``. When ``False``,
                the scheduler path is silent; an explicit ``mode_override``
                still dispatches (operator intent bypasses this gate).
            mode_override: When set, overrides the scheduler's current-slot
                choice — this exact StorageMode is dispatched **regardless of
                ``automation_enabled``** (operator intent is honored even
                when scheduled automation is off). ``None`` keeps sunSale's
                scheduled choice.

        Returns:
            Updated ``InverterModeHistory``. The coordinator persists this
            back to the rolling-history store.
        """
        updated_history = self._record_observation(now, reading, history)
        # A False→True automation transition is an operator request to
        # re-assert the scheduled mode: while automation was paused the
        # inverter may have drifted (or been changed at its own screen), and
        # the "write once, then hold" rule would otherwise never re-write it.
        # ``is False`` excludes the first-ever tick (None→True is a fresh
        # start, not a re-enable).
        if automation_enabled and self._last_automation_enabled is False:
            self._reassert_next = True
        self._last_automation_enabled = automation_enabled

        await self._apply_dispatch(now, schedule, automation_enabled, mode_override)
        return updated_history

    async def dispatch_override(
        self,
        now: datetime,
        schedule: Schedule | None,
        mode_override: StorageMode | None,
        automation_enabled: bool,
    ) -> None:
        """Dispatch the operator's override immediately, skipping the DAG cycle.

        Lightweight entry point for the mode-override select: a button press
        reaches the inverter without triggering a full coordinator refresh
        (translators, DAG nodes, store saves). Runs the same plan → act →
        verify path as ``tick``'s dispatch phase but omits the observe step and
        the history return — the next regular ``tick`` records the resulting
        observation. The verify loop started here behaves identically to one
        started from a scheduled tick.

        Args:
            now: Cycle timestamp (tz-aware), used for slot resolution and the
                verify window start.
            schedule: Latest Schedule (the coordinator's last computed one),
                consulted only when ``mode_override`` is ``None`` — releasing
                the override back to ``sunsale`` dispatches the current slot's
                mode immediately rather than waiting for the next tick.
            mode_override: The override to dispatch, or ``None`` to release to
                the scheduler's current-slot choice.
            automation_enabled: Gates the scheduler path only; an explicit
                ``mode_override`` dispatches regardless (operator intent).

        Note:
            Unlike ``tick``, this does **not** touch the automation-transition
            bookkeeping (``_reassert_next`` / ``_last_automation_enabled``) —
            an override press is not an automation toggle.
        """
        await self._apply_dispatch(now, schedule, automation_enabled, mode_override)

    async def _apply_dispatch(
        self,
        now: datetime,
        schedule: Schedule | None,
        automation_enabled: bool,
        mode_override: StorageMode | None,
    ) -> None:
        """Resolve the target and run the act + verify path; update diagnostics.

        Shared dispatch core for ``tick`` (regular cycle) and
        ``dispatch_override`` (instant panel button). Records the dispatch
        timestamp, outcome, target, and per-register status; performs no
        observation and returns no history. Dispatches when scheduled
        automation is on OR an explicit override is set — the override bypasses
        ``automation_enabled`` by design, since selecting a mode in the UI is an
        operator command that must reach the inverter even when scheduled
        writes are paused.

        Args:
            now: Cycle timestamp (tz-aware).
            schedule: Latest Schedule, or ``None``.
            automation_enabled: Gates the scheduler path; ignored when
                ``mode_override`` is set.
            mode_override: Operator override, or ``None`` for the scheduler.
        """
        async with self._dispatch_lock:
            self._last_dispatch_at = now
            self._automation_enabled_at_last_dispatch = automation_enabled
            if not automation_enabled and mode_override is None:
                self._last_dispatch_outcome = "automation_disabled"
                # Surface the would-have-been-dispatched target (the current
                # slot's mode) even though the write is gated off, so the panel
                # can show "would have dispatched X" alongside the
                # automation_disabled outcome. ``mode_override`` is ``None`` on
                # this branch by construction, so this resolves to the slot.
                self._last_dispatch_target = self.current_target(
                    now, schedule, mode_override
                )
                self._register_status = self._build_register_status()
                return

            outcome, target = await self._dispatch_current_slot(
                now, schedule, mode_override
            )
            self._last_dispatch_outcome = outcome
            self._last_dispatch_target = target
            self._register_status = self._build_register_status()

    @property
    def last_dispatch_outcome(self) -> str | None:
        """Return the outcome label of the most recent ``tick``.

        One of ``ok`` (a write was issued — the resolved target differed from
        the last-commanded mode), ``reconcile`` (a re-write was issued for an
        *unchanged* target — sustained register drift or an automation
        re-enable; see ``_reconcile_reason``), ``holding`` (target unchanged
        and engaged — no write, observer + verify only), ``no_target`` (no
        override + no schedule slot covering now), ``no_spec`` (target mode
        lacks a register spec), ``automation_disabled`` (no override and
        automation off — observer-only), or ``None`` before the first tick.
        """
        return self._last_dispatch_outcome

    @property
    def register_status(self) -> list[dict[str, Any]]:
        """Return the per-control-point desired-vs-observed comparison.

        One row per control point the last-commanded mode targets, taken from
        the driver's control surface (Solis names: ``reg_43110`` plus whichever
        of ``charge_a`` / ``discharge_a`` / ``export_limit_w`` / ``rc_setpoint_w``
        the mode writes). Each row carries ``name``, ``label``, ``desired``,
        ``observed`` and ``match``. Empty before any mode has been commanded. The panel
        colours each row from ``match`` plus the overall ``verify_state``
        (green = matched, amber = still verifying, red = mismatch after the
        verify window closed).
        """
        return [r.as_dict() for r in self._register_status]

    @property
    def register_panel(self) -> list[dict[str, Any]]:
        """Return the always-populated canonical register comparison for the panel.

        Unlike ``register_status`` (which lists only the control points the
        last-commanded mode writes, and is empty before any command), this is
        never empty: it surfaces the live observed value of every control point
        the driver exposes (Solis: ``reg_43110``, the battery currents, the
        export cap, and the RC setpoint) alongside the commanded mode's target
        (``None`` when the mode leaves it at the hardware default). It lets the
        panel always show "what the inverter is actually doing" next to "what we
        asked for", even when no mode is commanded or the observed state maps to
        no named mode (``observed_mode == UNKNOWN``).
        """
        return [r.as_dict() for r in self._build_register_panel()]

    @property
    def observed_mode(self) -> StorageMode:
        """Decode the inverter's current StorageMode from live register readbacks.

        Independent of the last coordinator-cycle ``InverterModeReading``: reads
        the same live readback cache as ``register_panel`` so the panel's
        observed-mode name and observed register values stay consistent during
        the verify window, when the cache updates faster than the 5-min cycle.

        Returns:
            Best-fit StorageMode; ``UNKNOWN`` when the raw state is unreadable
            or maps to no named mode.
        """
        return self._driver.decode_observed()

    @property
    def last_dispatch_target(self) -> StorageMode | None:
        """Return the StorageMode the most recent ``tick`` attempted to dispatch.

        Set even when the dispatch was blocked (e.g. by disabled
        automation), so the diagnostic sensor can show "would-have-been
        dispatched" alongside the outcome.
        """
        return self._last_dispatch_target

    @property
    def last_dispatch_at(self) -> datetime | None:
        """Return the timestamp of the most recent ``tick`` invocation."""
        return self._last_dispatch_at

    @property
    def automation_enabled_at_last_dispatch(self) -> bool | None:
        """Return the ``automation_enabled`` value at the most recent dispatch.

        Reflects the flag passed to the latest ``tick`` *or* ``dispatch_override``
        call, so the diagnostic stays accurate after an instant override press
        (which deliberately leaves the tick-transition bookkeeping untouched).
        """
        return self._automation_enabled_at_last_dispatch

    @property
    def last_commanded_mode(self) -> StorageMode | None:
        """Return the mode most recently force-written to the inverter.

        This is our own truth (set when ``_dispatch_current_slot`` detects a
        commanded change), independent of the solis_modbus state cache. The
        verify loop checks the inverter's actual register against this.
        """
        return self._last_commanded_mode

    @property
    def last_commanded_at(self) -> datetime | None:
        """Return the timestamp of the most recent commanded-mode change."""
        return self._last_commanded_at

    @property
    def verify_state(self) -> str | None:
        """Return the current verify state.

        One of ``pending`` (commanded change issued, verify hasn't run yet
        or is mid-retry), ``ok`` (verify saw the inverter at the commanded
        register value), ``mismatch`` (still wrong after one retry — the
        write isn't taking; check the inverter / Modbus chain), or ``None``
        before any command has been issued this run.
        """
        return self._verify_state

    @property
    def last_verify_at(self) -> datetime | None:
        """Return the timestamp of the most recent verify-tick reading."""
        return self._last_verify_at

    @property
    def last_verify_observed_reg(self) -> int | None:
        """Return the raw mode-state code the most recent verify-tick read.

        Sourced from the driver's ``observed_raw_state`` (Solis: register 43110);
        a platform-neutral diagnostic of "what raw state we saw this verify-tick".
        """
        return self._last_verify_observed_reg

    def current_target(
        self,
        now: datetime,
        schedule: Schedule | None,
        mode_override: StorageMode | None = None,
    ) -> StorageMode | None:
        """Return the StorageMode that would be dispatched for ``now``.

        Args:
            now: Cycle timestamp.
            schedule: Latest Schedule, or ``None``.
            mode_override: When set, this is returned directly — it is what
                the dispatcher will push to the inverter.

        Returns:
            Target StorageMode, or ``None`` when no override is set and no
            schedule slot covers ``now``.
        """
        if mode_override is not None:
            return mode_override
        slot = self._current_slot(now, schedule)
        return slot.mode if slot is not None else None

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _record_observation(
        self,
        now: datetime,
        reading: InverterModeReading,
        history: InverterModeHistory,
    ) -> InverterModeHistory:
        """Append a new history entry on mode change and prune old samples.

        Strictly append-on-change: when the observed mode matches the last
        recorded mode the history is unchanged. Samples older than the start
        of yesterday (computed in the coordinator's local timezone) are
        dropped.

        Args:
            now: Cycle timestamp.
            reading: Observed inverter state this cycle.
            history: Existing history.

        Returns:
            Updated history (possibly identical to the input).
        """
        samples = list(history.samples)
        last_mode = samples[-1].mode if samples else None
        if reading.mode != last_mode and reading.raw_state is not None:
            samples.append(
                InverterModeChange(
                    timestamp=now,
                    mode=reading.mode,
                    raw_state=reading.raw_state,
                )
            )

        cutoff = self._yesterday_local_midnight(now)
        if samples:
            samples = [s for s in samples if s.timestamp >= cutoff]

        return InverterModeHistory(samples=tuple(samples))

    async def _dispatch_current_slot(
        self,
        now: datetime,
        schedule: Schedule | None,
        mode_override: StorageMode | None = None,
    ) -> tuple[str, StorageMode | None]:
        """Apply the current-slot target mode (or the override) to the inverter.

        Args:
            now: Cycle timestamp.
            schedule: Latest Schedule, or ``None``.
            mode_override: When set, dispatched directly and the schedule slot
                is ignored. ``None`` falls back to the slot covering ``now``.

        Returns:
            ``(outcome, target)`` — outcome is one of ``ok`` (a write was
            issued because the target changed), ``reconcile`` (a re-write was
            issued for an unchanged target — drift or automation re-enable),
            ``holding`` (target unchanged and engaged, no write),
            ``no_target``, or ``no_spec``; target is the StorageMode the
            dispatcher resolved to (``None`` only when ``outcome == no_target``).
        """
        source = "override" if mode_override is not None else "schedule"
        if mode_override is not None:
            target: StorageMode | None = mode_override
        else:
            slot = self._current_slot(now, schedule)
            target = slot.mode if slot is not None else None
        if target is None:
            _LOGGER.debug(
                "inverter_control: no target this cycle (source=%s)", source,
            )
            return ("no_target", None)
        spec = self._driver.spec_for(target)
        if spec is None:
            _LOGGER.warning(
                "inverter_control: no spec for target mode %s (source=%s) — "
                "skipping dispatch",
                target.value, source,
            )
            return ("no_spec", target)
        # The mode is written exactly once, on the cycle the resolved target
        # changes (a button press, or a schedule slot boundary). Holding the
        # same target re-asserts nothing on the common path — the inverter was
        # already commanded and the verify loop confirms it took. Re-writing
        # every cycle is what the operator experiences as the mode being
        # "constantly set". The verify loop only lives ~60 s, though, so two
        # things still warrant a re-command of an *unchanged* target: a
        # sustained register drift after engagement, and an automation
        # re-enable. ``_reconcile_reason`` decides; everything else holds.
        if target == self._last_commanded_mode:
            drifted = self._registers_drifted()
            # Self-heal a stale terminal ``mismatch``: the verify loop's
            # mismatch verdict never re-checks, but if the inverter has since
            # returned to the commanded spec (operator fixed it, comms
            # restored) reflect that instead of showing red forever.
            if self._verify_state == "mismatch" and not drifted:
                self._verify_state = "ok"
                self._consecutive_drift_cycles = 0
                _LOGGER.info(
                    "inverter_control: %s re-converged after mismatch — "
                    "verify_state back to ok", target.value,
                )
                await self._maybe_refresh_rc(spec)
                return ("holding", target)
            reason = self._reconcile_reason(drifted)
            if reason is None:
                _LOGGER.debug(
                    "inverter_control: holding %s (source=%s, no commanded "
                    "change — observe + verify only)",
                    target.value, source,
                )
                await self._maybe_refresh_rc(spec)
                return ("holding", target)
            _LOGGER.warning(
                "inverter_control: reconciling %s (source=%s, %s) — "
                "re-commanding",
                target.value, source, reason,
            )
            await self._force_write_and_verify(now, target, spec, source)
            return ("reconcile", target)
        # Commanded-mode change → force-write (bypass the solis_modbus cache,
        # which can lag a successful write by up to a poll interval) and start
        # the verify loop.
        await self._force_write_and_verify(now, target, spec, source)
        return ("ok", target)

    def _reconcile_reason(self, drifted: bool) -> str | None:
        """Decide whether an unchanged-target hold must be re-commanded.

        Called only on the holding path (resolved target equals
        ``last_commanded_mode``). Returns a short human-readable reason when a
        re-command is warranted, or ``None`` to keep holding.

        Two conditions break the hold:

          * **Automation re-enabled** — ``_reassert_next`` was armed by a
            False→True ``automation_enabled`` transition. Always wins; the
            flag is consumed by the subsequent ``_force_write_and_verify``.
          * **Sustained drift** — once ``verify_state == "ok"`` (the steady
            engaged state), a drifted readback increments the consecutive-cycle
            counter; at ``_DRIFT_RECONCILE_CYCLES`` it trips. Conditioning on
            ``ok`` keeps this off the verify loop's own retry window (pending)
            and respects its terminal ``mismatch`` verdict — a write that
            demonstrably won't take is not re-spammed every few cycles.

        Args:
            drifted: Whether the commanded mode's registers currently disagree
                with the readback (from ``_registers_drifted``), computed once
                by the caller so it isn't read twice.

        Returns:
            A reason string to re-command, or ``None`` to keep holding. The
            consecutive-drift counter is reset whenever the hold is kept for a
            non-drift reason.
        """
        if self._reassert_next:
            return "automation re-enabled"
        if self._verify_state == "ok" and drifted:
            self._consecutive_drift_cycles += 1
            if self._consecutive_drift_cycles >= _DRIFT_RECONCILE_CYCLES:
                return (
                    f"register drift sustained {self._consecutive_drift_cycles} "
                    "cycles"
                )
            return None
        self._consecutive_drift_cycles = 0
        return None

    async def _maybe_refresh_rc(self, spec: Any) -> None:
        """Refresh the RC deadman registers while holding an engaged RC-backed mode.

        The inverter expires the RC function at most 30 min after the last RC
        write (register 43282), so a held GridCharge / Discharge must be
        re-asserted every tick — the write-once rule deliberately does not
        apply to the RAM-only RC registers. Runs only from the steady engaged
        state: ``pending`` is owned by the verify loop (the initial apply just
        wrote a full window), and a terminal ``mismatch`` is not re-spammed.

        Args:
            spec: Spec of the held mode; ``refresh_rc`` no-ops unless it
                carries a non-zero RC setpoint.
        """
        if self._verify_state != "ok":
            return
        await self._driver.refresh_rc(spec)

    def _arm_rc_keepalive(self) -> None:
        """(Re)start the RC keep-alive heartbeat for the commanded RC mode.

        Cancels any running heartbeat and schedules a fresh
        ``async_track_time_interval`` at ``_RC_KEEPALIVE_INTERVAL_S`` so the
        timer is anchored to the most recent full RC write. No-op when ``hass``
        is ``None`` (unit-test wiring without a real event loop) — the module
        then degrades to the 5-min tick's ``_maybe_refresh_rc`` only.
        """
        self._cancel_rc_keepalive()
        if self._hass is None:
            return
        self._rc_keepalive_cancel = async_track_time_interval(
            self._hass,
            self._on_rc_keepalive_tick,
            timedelta(seconds=_RC_KEEPALIVE_INTERVAL_S),
        )

    def _cancel_rc_keepalive(self) -> None:
        """Cancel the RC keep-alive heartbeat if one is running. Idempotent."""
        if self._rc_keepalive_cancel is not None:
            try:
                self._rc_keepalive_cancel()
            except Exception:  # noqa: BLE001 — cancel must never raise
                _LOGGER.debug(
                    "inverter_control: RC keep-alive cancel raised — ignoring",
                    exc_info=True,
                )
            self._rc_keepalive_cancel = None

    async def _on_rc_keepalive_tick(self, now: datetime) -> None:
        """Re-arm the inverter-side RC function on the keep-alive heartbeat.

        Fired by ``async_track_time_interval`` every ``_RC_KEEPALIVE_INTERVAL_S``
        while an RC-backed mode is commanded. Holds the dispatch lock so the
        re-arm write can't interleave with a mode change or a verify retry, and
        re-reads the commanded mode *under* the lock so a heartbeat queued
        behind an in-flight command re-arms the mode that command left in
        place — never a stale one. Refreshes only from the steady engaged state
        (``verify_state == "ok"``): the verify loop owns ``pending`` (its
        initial apply already wrote a full window), and a terminal ``mismatch``
        is not re-spammed — mirroring ``_maybe_refresh_rc``.

        Args:
            now: Heartbeat time from HA; unused — the re-arm is stateless.
        """
        async with self._dispatch_lock:
            commanded = self._last_commanded_mode
            if commanded is None:
                return
            spec = self._driver.spec_for(commanded)
            if spec is None or not self._driver.needs_keepalive(spec):
                return
            if self._verify_state != "ok":
                return
            _LOGGER.debug(
                "inverter_control: RC keep-alive re-arming %s", commanded.value,
            )
            await self._driver.refresh_rc(spec)

    def _registers_drifted(self) -> bool:
        """Return whether the last-commanded mode's registers have drifted.

        Reads the same per-register comparison the panel shows. ``True`` means
        at least one register the commanded mode writes no longer matches its
        target (or has become unreadable); ``False`` means every register still
        matches. An empty status (no mode commanded yet) is "not drifted".

        Returns:
            ``True`` when any participating register fails to match.
        """
        rows = self._build_register_status()
        return bool(rows) and not all(r.match for r in rows)

    async def _force_write_and_verify(
        self,
        now: datetime,
        target: StorageMode,
        spec: Any,
        source: str,
    ) -> None:
        """Force-write ``target`` and (re)start the verify loop.

        Shared by the commanded-change path and the reconcile path. Bypasses
        the solis_modbus cache (``force=True``), which can lag a successful
        write by up to a poll interval, records the new commanded truth, resets
        verify bookkeeping to ``pending``, and schedules the first verify-tick.
        Clears both reconciliation triggers so a write satisfies any pending
        re-assert and starts the drift counter fresh.

        Args:
            now: Cycle timestamp — recorded as commanded-at and the verify
                window start.
            target: Mode being (re-)commanded.
            spec: Register spec for ``target``.
            source: ``override`` or ``schedule``; logging only.
        """
        await self._driver.apply_mode(target, spec, force=True)
        self._last_commanded_mode = target
        self._last_commanded_at = now
        self._verify_state = "pending"
        self._last_verify_at = None
        self._last_verify_observed_reg = None
        self._verify_retried = False
        self._verify_window_started_at = now
        self._reassert_next = False
        self._consecutive_drift_cycles = 0
        self._last_applied_mode = target
        # Anchor the RC keep-alive to this full write: an RC-backed target gets
        # a fresh 3-min heartbeat; a non-RC target stops any heartbeat left
        # running by a prior RC mode (the function is released by apply_mode).
        if self._driver.needs_keepalive(spec):
            self._arm_rc_keepalive()
        else:
            self._cancel_rc_keepalive()
        _LOGGER.info(
            "inverter_control: dispatched %s (source=%s, force-write, "
            "first verify in %ds, polling every %ds up to %ds)",
            target.value, source,
            _VERIFY_INITIAL_DELAY_S,
            _VERIFY_POLL_INTERVAL_S,
            _VERIFY_WINDOW_S,
        )
        self._schedule_verify(_VERIFY_INITIAL_DELAY_S)

    @staticmethod
    def _current_slot(
        now: datetime, schedule: Schedule | None
    ) -> ScheduleSlot | None:
        """Return the slot covering ``now`` (no fallback to first slot)."""
        if schedule is None or not schedule.slots:
            return None
        return next((s for s in schedule.slots if s.start <= now < s.end), None)

    def _build_register_status(self) -> list[ControlRow]:
        """Build the desired-vs-observed comparison for the last-commanded mode.

        The verify/drift view: the driver's control-surface rows that carry a
        target — i.e. dropping the unconstrained rows (``desired is None``),
        which a commanded mode leaves at the hardware default and are not part of
        "did the write take". The which-rows-and-why detail is the driver's
        concern (see ``control_surface``); this method is platform-neutral. The
        early return keeps the no-command case read-free (no readbacks issued).

        Returns:
            One :class:`ControlRow` per targeted control point. Empty when no
            mode has been commanded yet.
        """
        commanded = self._last_commanded_mode
        if commanded is None or self._driver.spec_for(commanded) is None:
            return []
        return [r for r in self._build_register_panel() if r.desired is not None]

    def _build_register_panel(self) -> list[ControlRow]:
        """Return the driver's always-populated canonical register comparison.

        Thin pass-through to the platform driver's ``control_surface`` for the
        last-commanded mode — the register layout and comparison tolerances are
        the driver's concern, not this module's.

        Returns:
            Canonical :class:`ControlRow` list in ``apply_mode`` write order.
        """
        return self._driver.control_surface(self._last_commanded_mode)

    def _notify_state_change(self) -> None:
        """Fire the coordinator's state-change callback, if one was supplied.

        Called from the verify loop (which runs between coordinator ticks) so
        the panel's engagement badge and per-register colours refresh in real
        time instead of waiting for the next 5-minute cycle. Never raises — a
        push failure must not break the verify loop.
        """
        if self._on_state_change is None:
            return
        try:
            self._on_state_change()
        except Exception:  # noqa: BLE001 — push is best-effort
            _LOGGER.debug(
                "inverter_control: on_state_change callback raised — ignoring",
                exc_info=True,
            )

    def _yesterday_local_midnight(self, now: datetime) -> datetime:
        """Compute ``00:00`` of yesterday in the configured local timezone."""
        local_now = now.astimezone(self._local_tz)
        yday_local = (local_now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return yday_local.astimezone(now.tzinfo)

    # ------------------------------------------------------------------ #
    # Verify loop                                                          #
    # ------------------------------------------------------------------ #

    async def force_verify_now(self) -> None:
        """Run a verify cycle immediately, bypassing the +30 s wait.

        Public entry point for the ``sun_sale.force_verify_inverter_mode``
        service. Cancels any pending verify, then re-runs the same logic
        the scheduled callback would: re-read the driver's control surface,
        compare to ``last_commanded_mode``, log + flip ``verify_state``
        accordingly, and trigger a single retry on first mismatch (matching
        the regular verify-loop semantics).
        """
        if self._verify_cancel is not None:
            try:
                self._verify_cancel()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "inverter_control: force_verify_now cancel raised — ignoring",
                    exc_info=True,
                )
            self._verify_cancel = None
        if self._last_commanded_mode is None:
            _LOGGER.info(
                "inverter_control: force_verify_now — no commanded mode yet, "
                "nothing to verify",
            )
            return
        # Use real wall-clock time for elapsed calculations — the operator
        # explicitly asked for a "now" check.
        await self._on_verify_tick(datetime.now(self._local_tz))

    def _schedule_verify(self, delay_s: int) -> None:
        """Schedule a single verify-tick after ``delay_s`` seconds.

        Cancels any previously-scheduled verify so an in-flight commanded
        change always supersedes a stale one. When ``hass`` is ``None``
        (unit-test wiring) the call is a no-op and the verify loop is
        effectively disabled — tests that need to exercise it inject a
        mock that captures the scheduled callback for manual firing.

        Args:
            delay_s: Wall-clock delay until the verify callback fires.
        """
        if self._verify_cancel is not None:
            try:
                self._verify_cancel()
            except Exception:  # noqa: BLE001 — cancel must never raise
                _LOGGER.debug(
                    "inverter_control: prior verify-cancel raised — ignoring",
                    exc_info=True,
                )
            self._verify_cancel = None
        if self._hass is None:
            return
        self._verify_cancel = async_call_later(
            self._hass, delay_s, self._on_verify_tick,
        )

    async def _on_verify_tick(self, now: datetime) -> None:
        """Read the inverter back and decide whether commanded was applied.

        Fired by ``async_call_later`` (or ``force_verify_now``). On match,
        ``verify_state`` flips to ``ok`` and the loop stops. On mismatch,
        the elapsed time since the current window started decides:

          * Within window → schedule the next poll at +``POLL_INTERVAL`` s.
          * Window exhausted, not yet retried → re-issue the force-write,
            reset the window, and resume polling.
          * Window exhausted, already retried → ``mismatch``, log error,
            stop. Manual operator intervention required.

        The check spans **every** register the commanded mode writes — the
        43110 bitmask plus whichever currents / RC setpoint / export cap the
        spec carries. ``verify_state`` flips to ``ok`` only when all of them
        match (within ``apply_mode``'s write tolerance), so an engaged badge
        means the full mode composition is in place, not just the bitmask.

        Args:
            now: Tick time supplied by HA's ``async_call_later`` (or by
                ``force_verify_now``). Used for both the verify timestamp
                and the elapsed-time computation so tests can drive the
                loop deterministically.
        """
        # Hold the dispatch lock for the whole tick. This serialises the retry
        # write against ``_apply_dispatch`` and reads ``_last_commanded_mode``
        # *after* acquiring it, so a tick that was queued behind an in-flight
        # command verifies (and, on retry, re-writes) the mode that command
        # left in place — never a stale one captured before the await.
        async with self._dispatch_lock:
            self._verify_cancel = None
            commanded = self._last_commanded_mode
            if commanded is None:
                return  # commanded was cleared since the verify was scheduled
            spec = self._driver.spec_for(commanded)
            if spec is None:
                # Shouldn't happen — _dispatch_current_slot already guarded —
                # but if a future StorageMode lacks a spec, fail closed.
                self._verify_state = "mismatch"
                self._notify_state_change()
                return

            rows = self._build_register_status()
            self._register_status = rows
            self._last_verify_at = now
            self._last_verify_observed_reg = self._driver.observed_raw_state()

            if rows and all(r.match for r in rows):
                self._verify_state = "ok"
                _LOGGER.info(
                    "inverter_control: verify OK — commanded=%s all %d "
                    "registers match", commanded.value, len(rows),
                )
                self._notify_state_change()
                return

            mismatched = [r.name for r in rows if not r.match]
            window_start = self._verify_window_started_at or now
            elapsed = (now - window_start).total_seconds()

            if elapsed < _VERIFY_WINDOW_S:
                # Still within the current window — keep polling. Log at DEBUG
                # so a normal pending→ok transition stays quiet.
                _LOGGER.debug(
                    "inverter_control: verify pending — commanded=%s "
                    "mismatched=%s elapsed=%.0fs; next poll in %ds",
                    commanded.value, mismatched, elapsed,
                    _VERIFY_POLL_INTERVAL_S,
                )
                self._schedule_verify(_VERIFY_POLL_INTERVAL_S)
                self._notify_state_change()
                return

            # Window exhausted.
            if not self._verify_retried:
                _LOGGER.warning(
                    "inverter_control: verify mismatch after %.0fs — "
                    "commanded=%s mismatched=%s; re-issuing force-write",
                    elapsed, commanded.value, mismatched,
                )
                self._verify_retried = True
                await self._driver.apply_mode(commanded, spec, force=True)
                self._verify_window_started_at = now  # fresh retry window
                self._schedule_verify(_VERIFY_INITIAL_DELAY_S)
                # verify_state stays "pending" — the retry is still in flight
                self._notify_state_change()
                return

            self._verify_state = "mismatch"
            _LOGGER.error(
                "inverter_control: verify mismatch persists after retry "
                "(%.0fs elapsed in retry window) — commanded=%s mismatched=%s. "
                "The write is not taking effect; check the Modbus chain and "
                "inverter.",
                elapsed, commanded.value, mismatched,
            )
            self._notify_state_change()
