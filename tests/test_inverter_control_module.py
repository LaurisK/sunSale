"""Tests for the InverterControlModule (observer / dispatcher behind one entry point)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import (
    UNKNOWN_LIMIT,
    InverterModeChange,
    InverterModeHistory,
    InverterModeReading,
    Schedule,
    ScheduleSlot,
    StorageMode,
)
from custom_components.sun_sale.outbound.inverter import RC_ADJUSTMENT_AC_PORT_VALUE
from custom_components.sun_sale.outbound.inverter_control_module import (
    _DRIFT_RECONCILE_CYCLES,
    _RC_KEEPALIVE_INTERVAL_S,
    _VERIFY_WINDOW_S,
    InverterControlModule,
)
from custom_components.sun_sale.outbound.solis_driver import SolisDriver
from tests.conftest import default_battery_config


def _driver_for(inverter) -> SolisDriver:
    """Wrap a (mock) inverter in a real SolisDriver, as the coordinator does."""
    return SolisDriver(inverter, default_battery_config(), 10_000, 10_000)


NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


def _mock_inverter() -> MagicMock:
    inv = MagicMock()
    inv.apply_mode = AsyncMock()
    # Default every readback to None so the register-status comparison treats
    # ancillary registers as "unavailable" (no match) rather than choking on a
    # bare MagicMock. Tests that need a match wire concrete values via
    # ``_wire_inverter_to_spec``.
    inv.get_storage_control_word = MagicMock(return_value=None)
    inv.get_charge_current_a = MagicMock(return_value=None)
    inv.get_discharge_current_a = MagicMock(return_value=None)
    inv.get_backflow_power_w = MagicMock(return_value=None)
    inv.get_rc_setpoint_w = MagicMock(return_value=None)
    inv.get_rc_adjustment_value = MagicMock(return_value=None)
    inv.refresh_rc = AsyncMock()
    # No advertised bounds by default, so ``effective_spec`` leaves the composed
    # spec untouched. A bare MagicMock here would coerce every reduced target to
    # a stray int and silently rewrite the spec under the tests.
    inv.limit_for = MagicMock(return_value=UNKNOWN_LIMIT)
    return inv


def _wire_inverter_to_spec(
    inv: MagicMock, mod: InverterControlModule, mode: StorageMode
) -> None:
    """Make every inverter readback return the spec values for ``mode``.

    After this, the module's all-register verify sees a full match for
    ``mode`` — the standard "inverter accepted the command" fixture.
    """
    spec = mod._driver.spec_for(mode)
    inv.get_storage_control_word = MagicMock(return_value=spec.reg_43110_value)
    inv.get_charge_current_a = MagicMock(return_value=spec.charge_a)
    inv.get_discharge_current_a = MagicMock(return_value=spec.discharge_a)
    inv.get_backflow_power_w = MagicMock(return_value=spec.export_limit_w)
    inv.get_rc_setpoint_w = MagicMock(return_value=spec.rc_setpoint_w)
    inv.get_rc_adjustment_value = MagicMock(
        return_value=RC_ADJUSTMENT_AC_PORT_VALUE if spec.rc_setpoint_w != 0 else 0,
    )


def _module(inverter=None) -> InverterControlModule:
    return InverterControlModule(
        driver=_driver_for(inverter or _mock_inverter()),
        local_tz=UTC,
    )


def _reading(mode: StorageMode, reg: int | None = None, ts: datetime = NOW) -> InverterModeReading:
    return InverterModeReading(
        timestamp=ts,
        raw_state=reg if reg is not None else 1,
        mode=mode,
        charge_a=0.0,
        discharge_a=0.0,
        rc_setpoint_w=0,
    )


def _schedule_with(mode: StorageMode, now: datetime = NOW) -> Schedule:
    slot = ScheduleSlot(
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=50),
        mode=mode,
        power_kw=2.0,
        expected_soc_after=0.5,
        expected_profit_eur=0.1,
        reason="test",
    )
    return Schedule(slots=[slot], total_expected_profit_eur=0.1,
                    degradation_cost_per_kwh=0.02, computed_at=now)


# ---------------------------------------------------------------------------
# Observer-only path (automation OFF)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observer_only_does_not_call_apply_mode():
    inv = _mock_inverter()
    mod = _module(inv)
    result = await mod.tick(
        now=NOW,
        schedule=_schedule_with(StorageMode.GridCharge),
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
    )
    inv.apply_mode.assert_not_awaited()
    # Observation recorded even though we never dispatched.
    assert len(result.samples) == 1
    assert result.samples[0].mode == StorageMode.StandBy


@pytest.mark.asyncio
async def test_first_observation_appends_to_history():
    mod = _module()
    history = InverterModeHistory(samples=())
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=history, automation_enabled=False,
    )
    assert len(result.samples) == 1
    assert result.samples[0].mode == StorageMode.SelfUse
    assert result.samples[0].timestamp == NOW


@pytest.mark.asyncio
async def test_unchanged_mode_does_not_grow_history():
    existing = InverterModeChange(
        timestamp=NOW - timedelta(hours=2),
        mode=StorageMode.StandBy,
        raw_state=1,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=(existing,)),
        automation_enabled=False,
    )
    assert result.samples == (existing,)


@pytest.mark.asyncio
async def test_mode_change_appends_new_entry():
    existing = InverterModeChange(
        timestamp=NOW - timedelta(hours=2),
        mode=StorageMode.StandBy,
        raw_state=1,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.GridCharge, reg=33),
        history=InverterModeHistory(samples=(existing,)),
        automation_enabled=False,
    )
    assert len(result.samples) == 2
    assert result.samples[-1].mode == StorageMode.GridCharge
    assert result.samples[-1].raw_state == 33


@pytest.mark.asyncio
async def test_skip_append_when_register_readback_unavailable():
    # ``reading.raw_state is None`` is the driver-side "unavailable"
    # signal — we must not pollute history with phantom UNKNOWN entries.
    mod = _module()
    reading = InverterModeReading(
        timestamp=NOW, raw_state=None, mode=StorageMode.UNKNOWN,
        charge_a=None, discharge_a=None, rc_setpoint_w=None,
    )
    result = await mod.tick(
        now=NOW, schedule=None, reading=reading,
        history=InverterModeHistory(samples=()), automation_enabled=False,
    )
    assert result.samples == ()


@pytest.mark.asyncio
async def test_old_samples_pruned_at_yesterday_midnight():
    # Yesterday-local-midnight cutoff in UTC tz = NOW.date() - 1 day at 00:00.
    very_old = InverterModeChange(
        timestamp=NOW - timedelta(days=5),
        mode=StorageMode.StandBy, raw_state=1,
    )
    recent = InverterModeChange(
        timestamp=NOW - timedelta(hours=3),
        mode=StorageMode.GridCharge, raw_state=33,
    )
    mod = _module()
    result = await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.GridCharge, reg=33),
        history=InverterModeHistory(samples=(very_old, recent)),
        automation_enabled=False,
    )
    assert very_old not in result.samples
    assert recent in result.samples


# ---------------------------------------------------------------------------
# Active dispatch path (automation ON)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_calls_apply_mode_with_current_slot_target():
    inv = _mock_inverter()
    mod = _module(inv)
    schedule = _schedule_with(StorageMode.GridCharge)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_awaited_once()
    target_mode = inv.apply_mode.await_args.args[0]
    assert target_mode == StorageMode.GridCharge


@pytest.mark.asyncio
async def test_dispatch_skipped_when_no_schedule():
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_skipped_when_no_slot_covers_now():
    inv = _mock_inverter()
    mod = _module(inv)
    # Build a schedule whose only slot is in the past.
    past_slot = ScheduleSlot(
        start=NOW - timedelta(hours=5), end=NOW - timedelta(hours=4),
        mode=StorageMode.GridCharge, power_kw=2.0,
        expected_soc_after=0.5, expected_profit_eur=0.1, reason="x",
    )
    schedule = Schedule(slots=[past_slot], total_expected_profit_eur=0.0,
                        degradation_cost_per_kwh=0.0, computed_at=NOW)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_target_returns_slot_mode():
    mod = _module()
    schedule = _schedule_with(StorageMode.SelfUse)
    assert mod.current_target(NOW, schedule) == StorageMode.SelfUse


@pytest.mark.asyncio
async def test_current_target_returns_none_when_no_schedule():
    assert _module().current_target(NOW, None) is None


# ---------------------------------------------------------------------------
# Manual StorageMode override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_dispatches_overridden_mode_not_slot_mode():
    inv = _mock_inverter()
    mod = _module(inv)
    schedule = _schedule_with(StorageMode.SelfUse)
    await mod.tick(
        now=NOW, schedule=schedule,
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
        mode_override=StorageMode.Discharge,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.Discharge


@pytest.mark.asyncio
async def test_override_dispatches_when_no_schedule_slot_covers_now():
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.StandBy, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
        mode_override=StorageMode.GridCharge,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.GridCharge


@pytest.mark.asyncio
async def test_override_dispatched_even_when_automation_off():
    """Operator override bypasses the automation_enabled gate (Phase 1)."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
        mode_override=StorageMode.Discharge,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.Discharge


@pytest.mark.asyncio
async def test_no_dispatch_when_automation_off_and_no_override():
    """With automation off and no override, the module stays observer-only."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
        mode_override=None,
    )
    inv.apply_mode.assert_not_awaited()
    assert mod.last_dispatch_outcome == "automation_disabled"


@pytest.mark.asyncio
async def test_automation_disabled_surfaces_would_have_been_target():
    """A gated-off dispatch still reports the slot's mode as the target.

    Matches the ``last_dispatch_target`` contract — "set even when the dispatch
    was blocked" — so the panel can show "would have dispatched X".
    """
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.GridCharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
        mode_override=None,
    )
    assert mod.last_dispatch_outcome == "automation_disabled"
    assert mod.last_dispatch_target == StorageMode.GridCharge


@pytest.mark.asyncio
async def test_automation_disabled_target_none_without_slot():
    """No covering schedule slot → no would-have-been target to surface."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.tick(
        now=NOW, schedule=None,
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=False,
        mode_override=None,
    )
    assert mod.last_dispatch_outcome == "automation_disabled"
    assert mod.last_dispatch_target is None


@pytest.mark.asyncio
async def test_current_target_returns_override_when_set():
    mod = _module()
    schedule = _schedule_with(StorageMode.SelfUse)
    assert mod.current_target(
        NOW, schedule, mode_override=StorageMode.FeedIn,
    ) == StorageMode.FeedIn


@pytest.mark.asyncio
async def test_current_target_override_wins_even_without_schedule():
    mod = _module()
    assert mod.current_target(
        NOW, None, mode_override=StorageMode.Discharge,
    ) == StorageMode.Discharge


# ---------------------------------------------------------------------------
# Direct dispatch entry point (panel button — no full coordinator refresh)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_override_force_writes_the_override():
    """dispatch_override pushes the override straight to the inverter."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.dispatch_override(
        now=NOW, schedule=None,
        mode_override=StorageMode.Discharge,
        automation_enabled=False,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.Discharge
    # Force-write bypasses the solis_modbus readback cache, same as a tick.
    assert inv.apply_mode.await_args.kwargs.get("force") is True
    assert mod.last_dispatch_outcome == "ok"
    assert mod.last_commanded_mode == StorageMode.Discharge


@pytest.mark.asyncio
async def test_dispatch_override_release_falls_back_to_schedule_slot():
    """Releasing to sunsale (None) dispatches the current slot's mode at once."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.dispatch_override(
        now=NOW, schedule=_schedule_with(StorageMode.GridCharge),
        mode_override=None,
        automation_enabled=True,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.args[0] == StorageMode.GridCharge


@pytest.mark.asyncio
async def test_dispatch_override_observer_only_when_off_and_released():
    """No override + automation off → observer-only, no write."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.dispatch_override(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        mode_override=None,
        automation_enabled=False,
    )
    inv.apply_mode.assert_not_awaited()
    assert mod.last_dispatch_outcome == "automation_disabled"


@pytest.mark.asyncio
async def test_dispatch_override_does_not_touch_automation_tracking():
    """An override press is not an automation toggle — reassert stays disarmed."""
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.dispatch_override(
        now=NOW, schedule=None,
        mode_override=StorageMode.FeedIn,
        automation_enabled=True,
    )
    # tick's False→True re-assert bookkeeping is untouched by a direct dispatch.
    assert mod._reassert_next is False
    assert mod._last_automation_enabled is None


@pytest.mark.asyncio
async def test_dispatch_override_records_automation_flag_for_diagnostics():
    """The direct path still reports the live automation flag as a diagnostic.

    ``dispatch_override`` skips the tick-transition bookkeeping
    (``_last_automation_enabled`` stays ``None``), but the
    ``automation_enabled_at_last_dispatch`` diagnostic must reflect the flag
    passed to this very dispatch — not a stale value from a previous tick.
    """
    inv = _mock_inverter()
    mod = _module(inv)
    await mod.dispatch_override(
        now=NOW, schedule=None,
        mode_override=StorageMode.FeedIn,
        automation_enabled=True,
    )
    assert mod.automation_enabled_at_last_dispatch is True
    # The tick-transition field is deliberately left untouched.
    assert mod._last_automation_enabled is None


# ---------------------------------------------------------------------------
# Phase 2: commanded-mode tracking + verify loop
# ---------------------------------------------------------------------------


def _module_with_hass(inverter=None) -> InverterControlModule:
    """Build a module with a non-None hass so the verify scheduler runs."""
    return InverterControlModule(
        driver=_driver_for(inverter or _mock_inverter()),
        local_tz=UTC,
        hass=MagicMock(),
    )


class _ScheduledCallback:
    """Captures the most recent ``async_call_later`` invocation.

    Replaces the real helper for tests so we can verify (a) the verify-tick
    is scheduled with the right delay and (b) the captured async callback
    runs the correct read-back logic when manually fired.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []
        self.cancels: int = 0

    def __call__(self, _hass, delay, callback):
        self.calls.append((delay, callback))

        def _cancel() -> None:
            self.cancels += 1

        return _cancel

    @property
    def last_callback(self):
        return self.calls[-1][1] if self.calls else None


@pytest.fixture
def call_later(monkeypatch):
    """Patch ``async_call_later`` in the control module for verify-loop tests."""
    sched = _ScheduledCallback()
    monkeypatch.setattr(
        "custom_components.sun_sale.outbound.inverter_control_module."
        "async_call_later",
        sched,
    )
    return sched


class _TrackedInterval:
    """Captures ``async_track_time_interval`` for RC keep-alive tests.

    Records each registration's ``(interval, action)`` and counts cancels, so a
    test can assert the heartbeat was armed at the right cadence, fire its
    captured callback manually, and confirm it was cancelled.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[timedelta, object]] = []
        self.cancels: int = 0

    def __call__(self, _hass, action, interval, *args, **kwargs):
        self.calls.append((interval, action))

        def _cancel() -> None:
            self.cancels += 1

        return _cancel

    @property
    def interval(self):
        return self.calls[-1][0] if self.calls else None

    @property
    def action(self):
        return self.calls[-1][1] if self.calls else None


@pytest.fixture
def track_interval(monkeypatch):
    """Patch ``async_track_time_interval`` in the control module for keep-alive tests."""
    tracked = _TrackedInterval()
    monkeypatch.setattr(
        "custom_components.sun_sale.outbound.inverter_control_module."
        "async_track_time_interval",
        tracked,
    )
    return tracked


@pytest.mark.asyncio
async def test_commanded_change_force_writes_and_schedules_verify(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    inv.apply_mode.assert_awaited_once()
    assert inv.apply_mode.await_args.kwargs.get("force") is True
    assert mod.last_commanded_mode == StorageMode.Discharge
    assert mod.last_commanded_at == NOW
    assert mod.verify_state == "pending"
    assert len(call_later.calls) == 1
    assert call_later.calls[0][0] == 2  # _VERIFY_INITIAL_DELAY_S


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_verify_and_drops_callback(call_later):
    """Shutdown must cancel the scheduled verify so no callback fires post-unload."""
    inv = _mock_inverter()
    on_change = MagicMock()
    mod = InverterControlModule(
        driver=_driver_for(inv),
        local_tz=UTC,
        hass=MagicMock(),
        on_state_change=on_change,
    )
    # Command a change so a verify-tick is scheduled (i.e. _verify_cancel set).
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert len(call_later.calls) == 1
    assert call_later.cancels == 0

    mod.shutdown()

    assert call_later.cancels == 1  # pending verify cancelled
    assert mod._verify_cancel is None
    assert mod._on_state_change is None  # no push into a torn-down coordinator
    # Idempotent — a second shutdown is a no-op, not a crash.
    mod.shutdown()
    assert call_later.cancels == 1


@pytest.mark.asyncio
async def test_unchanged_command_holds_without_rewriting(call_later):
    """Holding the same target re-asserts nothing — the mode is set once."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    # First tick commands Discharge — force-write, one verify scheduled.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert inv.apply_mode.await_count == 1
    assert len(call_later.calls) == 1
    # Second tick same target — no write, no new verify, "holding" outcome.
    later = NOW + timedelta(minutes=5)
    await mod.tick(
        now=later, schedule=_schedule_with(StorageMode.Discharge, now=later),
        reading=_reading(StorageMode.Discharge, reg=2, ts=later),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert inv.apply_mode.await_count == 1  # no re-write
    assert mod.last_dispatch_outcome == "holding"
    assert len(call_later.calls) == 1  # no new schedule


@pytest.mark.asyncio
async def test_verify_tick_marks_state_ok_when_observed_matches(call_later):
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    spec_reg_for_discharge = mod._driver.spec_for(StorageMode.Discharge).reg_43110_value
    # Every register reads back its commanded value → full match.
    _wire_inverter_to_spec(inv, mod, StorageMode.Discharge)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    # First poll fires at +2 s — observed already matches commanded, so we
    # land on "ok" without burning the rest of the window.
    await call_later.last_callback(NOW + timedelta(seconds=2))
    assert mod.verify_state == "ok"
    assert mod.last_verify_observed_reg == spec_reg_for_discharge
    assert mod.last_verify_at == NOW + timedelta(seconds=2)
    assert all(r["match"] for r in mod.register_status)


@pytest.mark.asyncio
async def test_verify_keeps_polling_within_window_on_mismatch(call_later):
    """Mismatch within the 30 s window must reschedule, not retry."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # wrong reg
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    initial_write_count = inv.apply_mode.await_count
    # Fire several polls within the 30 s window — each should schedule the
    # next at +5 s without re-issuing the write.
    for elapsed in (2, 7, 12, 17, 22, 27):
        cb = call_later.last_callback
        await cb(NOW + timedelta(seconds=elapsed))
        assert mod.verify_state == "pending"
    assert inv.apply_mode.await_count == initial_write_count  # no retry yet
    # Each poll past the first scheduled a follow-up at the 5 s cadence.
    assert all(c[0] == 5 for c in call_later.calls[1:])


@pytest.mark.asyncio
async def test_verify_mismatch_retries_then_marks_mismatch(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # wrong reg
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    initial_write_count = inv.apply_mode.await_count
    # First window — fire a poll past the 30 s mark. This triggers the
    # single force-write retry and resets the window.
    await call_later.last_callback(NOW + timedelta(seconds=31))
    assert mod.verify_state == "pending"  # mid-retry, still polling
    assert inv.apply_mode.await_count == initial_write_count + 1
    assert inv.apply_mode.await_args.kwargs.get("force") is True
    # Retry window — fire a poll past the 30 s retry mark.
    await call_later.last_callback(NOW + timedelta(seconds=31 + 31))
    assert mod.verify_state == "mismatch"
    # No further retries.
    assert inv.apply_mode.await_count == initial_write_count + 1


@pytest.mark.asyncio
async def test_force_verify_now_noop_before_any_command(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    await mod.force_verify_now()
    # No commanded mode → no read, no apply_mode call.
    inv.get_storage_control_word.assert_not_called()
    inv.apply_mode.assert_not_awaited()
    assert mod.verify_state is None


@pytest.mark.asyncio
async def test_force_verify_now_runs_verify_immediately(call_later):
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    _wire_inverter_to_spec(inv, mod, StorageMode.Discharge)
    # Establish a commanded mode first.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    pending_before = call_later.cancels
    # Force-verify — should cancel pending scheduled verify and re-run now.
    await mod.force_verify_now()
    assert call_later.cancels == pending_before + 1
    assert mod.verify_state == "ok"


@pytest.mark.asyncio
async def test_new_command_cancels_pending_verify(call_later):
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=None)
    mod = _module_with_hass(inv)
    # First command — schedules verify@30s.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert call_later.cancels == 0
    # Second command (different mode) — should cancel pending verify and
    # schedule a fresh one.
    later = NOW + timedelta(seconds=5)
    await mod.tick(
        now=later, schedule=_schedule_with(StorageMode.GridCharge, now=later),
        reading=_reading(StorageMode.SelfUse, reg=1, ts=later),
        history=InverterModeHistory(samples=()),
        automation_enabled=True,
    )
    assert call_later.cancels == 1
    assert len(call_later.calls) == 2
    assert mod.last_commanded_mode == StorageMode.GridCharge


# ---------------------------------------------------------------------------
# Phase 2.5: slow drift reconciliation + automation re-enable re-assert
# ---------------------------------------------------------------------------


async def _engage_ok(mod, inv, call_later, mode=StorageMode.Discharge, now=NOW):
    """Command ``mode`` and drive the verify loop to ``ok``.

    Leaves the inverter wired so every register reads back its commanded
    value — the steady "engaged" state drift reconciliation watches.
    """
    _wire_inverter_to_spec(inv, mod, mode)
    await mod.tick(
        now=now, schedule=_schedule_with(mode, now=now),
        reading=_reading(StorageMode.SelfUse, reg=1, ts=now),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    await call_later.last_callback(now + timedelta(seconds=2))
    assert mod.verify_state == "ok"


async def _hold_tick(mod, mode, minutes, automation_enabled=True):
    """Run one same-target holding tick ``minutes`` after NOW."""
    t = NOW + timedelta(minutes=minutes)
    await mod.tick(
        now=t, schedule=_schedule_with(mode, now=t),
        reading=_reading(mode, reg=2, ts=t),
        history=InverterModeHistory(samples=()),
        automation_enabled=automation_enabled,
    )


@pytest.mark.asyncio
async def test_drift_after_ok_reconciles_after_threshold_cycles(call_later):
    """An engaged mode that drifts is re-commanded once the debounce trips."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)
    assert inv.apply_mode.await_count == 1
    # Drift: the storage control word slips away from the commanded spec.
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)
    # First _DRIFT_RECONCILE_CYCLES-1 drifted ticks are debounced → holding.
    for i in range(1, _DRIFT_RECONCILE_CYCLES):
        await _hold_tick(mod, StorageMode.Discharge, minutes=5 * i)
        assert mod.last_dispatch_outcome == "holding"
        assert inv.apply_mode.await_count == 1
    # The threshold-th consecutive drifted tick re-commands.
    await _hold_tick(mod, StorageMode.Discharge, minutes=5 * _DRIFT_RECONCILE_CYCLES)
    assert mod.last_dispatch_outcome == "reconcile"
    assert inv.apply_mode.await_count == 2
    assert inv.apply_mode.await_args.kwargs.get("force") is True
    assert mod.verify_state == "pending"  # reconcile restarts the verify loop


@pytest.mark.asyncio
async def test_single_drift_cycle_resets_counter_on_recovery(call_later):
    """A drift that clears before the threshold must not carry over."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)
    spec_reg = mod._driver.spec_for(StorageMode.Discharge).reg_43110_value
    # One drifted tick (counter → 1, below threshold of 2).
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)
    await _hold_tick(mod, StorageMode.Discharge, minutes=5)
    assert mod.last_dispatch_outcome == "holding"
    # Recover: register matches again → counter resets.
    inv.get_storage_control_word = MagicMock(return_value=spec_reg)
    await _hold_tick(mod, StorageMode.Discharge, minutes=10)
    assert mod.last_dispatch_outcome == "holding"
    # Drift once more — because the counter reset, a single cycle is again
    # below threshold and must not reconcile.
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)
    await _hold_tick(mod, StorageMode.Discharge, minutes=15)
    assert mod.last_dispatch_outcome == "holding"
    assert inv.apply_mode.await_count == 1  # never re-commanded


@pytest.mark.asyncio
async def test_reconcile_skipped_while_verify_pending(call_later):
    """Drift reconciliation defers to the verify loop while it owns the window."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # never matches
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    assert mod.verify_state == "pending"
    # Several drifted holding ticks while verify is still pending → no rewrite.
    for i in range(1, _DRIFT_RECONCILE_CYCLES + 3):
        await _hold_tick(mod, StorageMode.Discharge, minutes=5 * i)
        assert mod.last_dispatch_outcome == "holding"
    assert inv.apply_mode.await_count == 1


@pytest.mark.asyncio
async def test_reconcile_skipped_while_verify_mismatch(call_later):
    """A terminal mismatch is not re-spammed by the drift path."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    # Drive the verify loop to its terminal mismatch (retry, then give up).
    await call_later.last_callback(NOW + timedelta(seconds=31))
    await call_later.last_callback(NOW + timedelta(seconds=62))
    assert mod.verify_state == "mismatch"
    writes = inv.apply_mode.await_count  # initial + one retry
    # Continued drift across many holding ticks must not re-command.
    for i in range(1, _DRIFT_RECONCILE_CYCLES + 3):
        await _hold_tick(mod, StorageMode.Discharge, minutes=5 * i)
        assert mod.last_dispatch_outcome == "holding"
        assert mod.verify_state == "mismatch"
    assert inv.apply_mode.await_count == writes


@pytest.mark.asyncio
async def test_mismatch_self_heals_when_registers_reconverge(call_later):
    """A stale mismatch flips back to ok once the inverter matches again."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    await call_later.last_callback(NOW + timedelta(seconds=31))
    await call_later.last_callback(NOW + timedelta(seconds=62))
    assert mod.verify_state == "mismatch"
    writes = inv.apply_mode.await_count
    # Inverter returns to the commanded spec (operator fixed it / comms back).
    _wire_inverter_to_spec(inv, mod, StorageMode.Discharge)
    await _hold_tick(mod, StorageMode.Discharge, minutes=5)
    assert mod.verify_state == "ok"
    assert mod.last_dispatch_outcome == "holding"
    assert inv.apply_mode.await_count == writes  # self-heal issues no write


@pytest.mark.asyncio
async def test_automation_reenable_reasserts_held_mode(call_later):
    """Toggling automation off→on re-commands the held mode (no drift needed)."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)
    assert inv.apply_mode.await_count == 1
    # Automation OFF — observer-only, no rewrite, commanded truth preserved.
    await _hold_tick(mod, StorageMode.Discharge, minutes=5, automation_enabled=False)
    assert mod.last_dispatch_outcome == "automation_disabled"
    assert inv.apply_mode.await_count == 1
    # Automation back ON — re-assert even though the target is unchanged and
    # the registers never drifted.
    await _hold_tick(mod, StorageMode.Discharge, minutes=10, automation_enabled=True)
    assert mod.last_dispatch_outcome == "reconcile"
    assert inv.apply_mode.await_count == 2
    assert mod.verify_state == "pending"


@pytest.mark.asyncio
async def test_first_tick_does_not_spuriously_reassert(call_later):
    """The None→True automation start must not be read as a re-enable."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    # First-ever tick with automation already on commands once (a genuine
    # commanded change), not a reconcile.
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    assert mod.last_dispatch_outcome == "ok"
    assert inv.apply_mode.await_count == 1


# ---------------------------------------------------------------------------
# RC deadman keep-alive (registers 43132 / 43282 / 43128)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holding_engaged_rc_mode_refreshes_deadman(call_later):
    """Every ok-holding tick re-asserts the RC registers of the held mode."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)  # Discharge — RC-backed
    assert inv.refresh_rc.await_count == 0  # the initial apply wrote a full window
    await _hold_tick(mod, StorageMode.Discharge, minutes=5)
    assert mod.last_dispatch_outcome == "holding"
    assert inv.refresh_rc.await_count == 1
    assert inv.refresh_rc.await_args.args[0] == mod._driver.spec_for(StorageMode.Discharge)
    await _hold_tick(mod, StorageMode.Discharge, minutes=10)
    assert inv.refresh_rc.await_count == 2


@pytest.mark.asyncio
async def test_no_rc_refresh_while_verify_pending_or_mismatch(call_later):
    """The keep-alive runs only from the steady engaged state."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # never matches
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    assert mod.verify_state == "pending"
    await _hold_tick(mod, StorageMode.Discharge, minutes=5)
    assert inv.refresh_rc.await_count == 0
    # Drive to terminal mismatch — still no keep-alive (not re-spammed).
    await call_later.last_callback(NOW + timedelta(seconds=31))
    await call_later.last_callback(NOW + timedelta(seconds=62))
    assert mod.verify_state == "mismatch"
    await _hold_tick(mod, StorageMode.Discharge, minutes=10)
    assert inv.refresh_rc.await_count == 0


@pytest.mark.asyncio
async def test_rc_keepalive_armed_and_fires_on_engaged_rc_mode(call_later, track_interval):
    """Engaging an RC mode arms a 3-min heartbeat that re-arms the RC function."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)  # Discharge — RC-backed, verify ok
    # Heartbeat armed at the documented cadence; no refresh yet (the initial
    # apply already wrote a full RC window).
    assert track_interval.interval == timedelta(seconds=_RC_KEEPALIVE_INTERVAL_S)
    assert inv.refresh_rc.await_count == 0
    # Fire the heartbeat → full re-arm of the held mode's RC registers.
    await track_interval.action(NOW + timedelta(seconds=_RC_KEEPALIVE_INTERVAL_S))
    assert inv.refresh_rc.await_count == 1
    assert inv.refresh_rc.await_args.args[0] == mod._driver.spec_for(StorageMode.Discharge)


@pytest.mark.asyncio
async def test_rc_keepalive_skips_when_not_engaged(call_later, track_interval):
    """The heartbeat re-arms only from the steady ``ok`` state, never pending."""
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=0xDEAD)  # never matches
    mod = _module_with_hass(inv)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.Discharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    assert mod.verify_state == "pending"
    # Heartbeat is armed, but firing it while pending is a no-op.
    await track_interval.action(NOW + timedelta(seconds=_RC_KEEPALIVE_INTERVAL_S))
    assert inv.refresh_rc.await_count == 0


@pytest.mark.asyncio
async def test_rc_keepalive_cancelled_when_non_rc_mode_commanded(call_later, track_interval):
    """Commanding a non-RC mode cancels the heartbeat left by an RC mode."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later, mode=StorageMode.Discharge)
    assert track_interval.cancels == 0  # first arm: nothing to cancel yet
    # Command SelfUse (rc_setpoint_w == 0) → keep-alive cancelled.
    await _engage_ok(
        mod, inv, call_later, mode=StorageMode.SelfUse, now=NOW + timedelta(minutes=15),
    )
    assert track_interval.cancels == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_rc_keepalive(call_later, track_interval):
    """Shutdown stops the heartbeat so no re-arm write fires post-unload."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later, mode=StorageMode.Discharge)
    assert track_interval.cancels == 0
    mod.shutdown()
    assert track_interval.cancels == 1


@pytest.mark.asyncio
async def test_register_status_rc_enable_row_only_for_rc_modes(call_later):
    """RC-backed modes expose the 43132 selector row; passive modes don't."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later, mode=StorageMode.Discharge)
    rows = {r["name"]: r for r in mod.register_status}
    assert "rc_enable" in rows
    assert rows["rc_enable"]["match"] is True
    await _engage_ok(mod, inv, call_later, mode=StorageMode.SelfUse, now=NOW + timedelta(minutes=15))
    assert "rc_enable" not in {r["name"] for r in mod.register_status}


@pytest.mark.asyncio
async def test_register_status_rc_enable_flags_dropped_selector(call_later):
    """A selector that reads OFF shows as a mismatching row."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later, mode=StorageMode.Discharge)
    inv.get_rc_adjustment_value = MagicMock(return_value=0)  # dropped to OFF
    await _hold_tick(mod, StorageMode.Discharge, minutes=5)
    rows = {r["name"]: r for r in mod.register_status}
    assert rows["rc_enable"]["match"] is False


# ---------------------------------------------------------------------------
# register_panel: always-populated canonical comparison + observed_mode
# ---------------------------------------------------------------------------


def test_register_panel_populated_before_any_command():
    """register_panel surfaces live observed values even with no commanded mode.

    The whole point of the panel view: register values must be visible (with
    no target to compare against) before any dispatch and when the observed
    bitmask maps to no named mode.
    """
    inv = _mock_inverter()
    inv.get_storage_control_word = MagicMock(return_value=99)  # maps to no mode
    inv.get_charge_current_a = MagicMock(return_value=12.5)
    mod = _module(inv)
    assert mod.last_commanded_mode is None
    rows = {r["name"]: r for r in mod.register_panel}
    # The full canonical set is present, observed-only (no target).
    assert {"reg_43110", "charge_a", "discharge_a", "export_limit_w",
            "rc_setpoint_w"} <= set(rows)
    assert rows["reg_43110"]["observed"] == 99
    assert rows["reg_43110"]["desired"] is None
    assert rows["reg_43110"]["match"] is None          # no target → neutral
    assert rows["charge_a"]["observed"] == 12.5
    # No RC-backed command → no selector row in the always-on set.
    assert "rc_enable" not in rows


@pytest.mark.asyncio
async def test_register_panel_pairs_target_with_observed_when_commanded(call_later):
    """Once a mode is commanded, register_panel pairs spec target vs observed."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    _wire_inverter_to_spec(inv, mod, StorageMode.GridCharge)
    spec = mod._driver.spec_for(StorageMode.GridCharge)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.GridCharge),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    rows = {r["name"]: r for r in mod.register_panel}
    assert rows["reg_43110"]["desired"] == spec.reg_43110_value
    assert rows["reg_43110"]["observed"] == spec.reg_43110_value
    assert rows["reg_43110"]["match"] is True
    # GridCharge is RC-backed → the 43132 selector row participates.
    assert "rc_enable" in rows
    assert rows["rc_enable"]["match"] is True


@pytest.mark.asyncio
async def test_register_status_is_constrained_subset_of_panel(call_later):
    """register_status == the panel rows that carry a target (desired not None)."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    _wire_inverter_to_spec(inv, mod, StorageMode.SelfUse)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    status_names = [r["name"] for r in mod.register_status]
    constrained = [r["name"] for r in mod.register_panel if r["desired"] is not None]
    assert status_names == constrained
    # Every status row has a concrete (non-None) match — never the neutral state.
    assert all(r["match"] is not None for r in mod.register_status)


def test_observed_mode_decodes_from_live_readbacks():
    """observed_mode decodes the live register cache independent of any command."""
    inv = _mock_inverter()
    mod = _module(inv)
    # Bitmask 33 → GridCharge regardless of currents.
    inv.get_storage_control_word = MagicMock(return_value=33)
    assert mod.observed_mode is StorageMode.GridCharge
    # Unreadable register → UNKNOWN.
    inv.get_storage_control_word = MagicMock(return_value=None)
    assert mod.observed_mode is StorageMode.UNKNOWN


# ---------------------------------------------------------------------------
# Unreadable ≠ drifted (capability seam, phase 2)
#
# An entity that stops publishing must not read as register drift. The live
# 2026-08-03 failure: solis_modbus stopped publishing 43117/43118, every
# targeted current row went observed=None, and because ``match=False`` covered
# both "wrong" and "unreadable" the module treated a comms gap as a control
# fault — pinning ``_registers_drifted()`` True, starving the stale-verdict
# self-heal, and burning two force-writes plus an ERROR per slot boundary.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreadable_registers_do_not_count_as_drift(call_later):
    """Currents going unavailable must not trigger reconcile re-writes."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)
    assert inv.apply_mode.await_count == 1

    # The two current registers stop being published; everything else still reads.
    inv.get_charge_current_a = MagicMock(return_value=None)
    inv.get_discharge_current_a = MagicMock(return_value=None)

    # Well past the drift debounce: still holding, never re-commanded.
    for i in range(1, _DRIFT_RECONCILE_CYCLES + 3):
        await _hold_tick(mod, StorageMode.Discharge, minutes=5 * i)
        assert mod.last_dispatch_outcome == "holding"
    assert inv.apply_mode.await_count == 1
    assert mod.verify_state == "ok"


@pytest.mark.asyncio
async def test_unreadable_rows_report_unknown_status(call_later):
    """The panel payload distinguishes unreadable from wrong."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)
    inv.get_charge_current_a = MagicMock(return_value=None)
    await _hold_tick(mod, StorageMode.Discharge, minutes=5)

    rows = {r["name"]: r for r in mod.register_status}
    assert rows["charge_a"]["status"] == "unknown"
    assert rows["reg_43110"]["status"] == "match"
    # Legacy boolean stays available and falsy for the unreadable row.
    assert rows["charge_a"]["match"] is False


@pytest.mark.asyncio
async def test_verify_reports_unknown_not_mismatch_when_only_unreadable(call_later):
    """A comms gap during verify yields ``unknown`` and issues no retry.

    Re-writing a register we cannot read proves nothing, so the retry is skipped
    entirely — the old code force-wrote twice and then declared ``mismatch``.
    """
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    # reg_43110 matches; the currents this mode targets are unreadable.
    spec = mod._driver.spec_for(StorageMode.SelfUse)
    inv.get_storage_control_word = MagicMock(return_value=spec.reg_43110_value)
    inv.get_backflow_power_w = MagicMock(return_value=spec.export_limit_w)
    inv.get_rc_setpoint_w = MagicMock(return_value=spec.rc_setpoint_w)
    inv.get_rc_adjustment_value = MagicMock(return_value=0)

    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    assert inv.apply_mode.await_count == 1
    # Exhaust the verify window.
    await call_later.last_callback(NOW + timedelta(seconds=_VERIFY_WINDOW_S + 5))
    assert mod.verify_state == "unknown"
    assert inv.apply_mode.await_count == 1  # no retry issued


@pytest.mark.asyncio
async def test_unknown_verify_clears_to_ok_only_on_positive_confirmation(call_later):
    """``unknown`` needs every row read back and matching before it clears.

    "Not drifted" is insufficient — an unreadable row is also not drifted, so
    accepting that would flip the badge green with no evidence.
    """
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    spec = mod._driver.spec_for(StorageMode.SelfUse)
    inv.get_storage_control_word = MagicMock(return_value=spec.reg_43110_value)
    inv.get_backflow_power_w = MagicMock(return_value=spec.export_limit_w)
    inv.get_rc_setpoint_w = MagicMock(return_value=spec.rc_setpoint_w)
    inv.get_rc_adjustment_value = MagicMock(return_value=0)
    await mod.tick(
        now=NOW, schedule=_schedule_with(StorageMode.SelfUse),
        reading=_reading(StorageMode.SelfUse, reg=1),
        history=InverterModeHistory(samples=()), automation_enabled=True,
    )
    await call_later.last_callback(NOW + timedelta(seconds=_VERIFY_WINDOW_S + 5))
    assert mod.verify_state == "unknown"

    # Still unreadable → stays unknown, no false green.
    await _hold_tick(mod, StorageMode.SelfUse, minutes=5)
    assert mod.verify_state == "unknown"

    # Registers come back and agree → now it may clear.
    _wire_inverter_to_spec(inv, mod, StorageMode.SelfUse)
    await _hold_tick(mod, StorageMode.SelfUse, minutes=10)
    assert mod.verify_state == "ok"


@pytest.mark.asyncio
async def test_genuine_drift_still_reconciles_after_seam_change(call_later):
    """Guard: making unreadable benign must not disarm real drift detection."""
    inv = _mock_inverter()
    mod = _module_with_hass(inv)
    await _engage_ok(mod, inv, call_later)
    # A readable-but-wrong current is drift, and must still be re-commanded.
    inv.get_charge_current_a = MagicMock(return_value=1.0)
    for i in range(1, _DRIFT_RECONCILE_CYCLES):
        await _hold_tick(mod, StorageMode.Discharge, minutes=5 * i)
        assert mod.last_dispatch_outcome == "holding"
    await _hold_tick(mod, StorageMode.Discharge, minutes=5 * _DRIFT_RECONCILE_CYCLES)
    assert mod.last_dispatch_outcome == "reconcile"
    assert inv.apply_mode.await_count == 2
