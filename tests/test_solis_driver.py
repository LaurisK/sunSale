"""Tests for SolisDriver — the extracted Solis control surface / decoder."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import UNKNOWN_LIMIT, StorageMode
from custom_components.sun_sale.outbound.driver import (
    InverterControlDriver,
    InverterDriver,
)
from custom_components.sun_sale.outbound.driver_factory import make_inverter_driver
from custom_components.sun_sale.outbound.entity_control import InverterContext
from custom_components.sun_sale.outbound.inverter import (
    RC_ADJUSTMENT_AC_PORT_VALUE,
    InverterPlatform,
)
from custom_components.sun_sale.outbound.solis_driver import (
    _RC_KEEPALIVE_INTERVAL,
    SolisDriver,
)
from tests.conftest import default_battery_config


def _inverter() -> MagicMock:
    """Return a mock inverter whose readbacks all default to None."""
    inv = MagicMock()
    inv.apply_mode = AsyncMock()
    inv.refresh_rc = AsyncMock()
    for getter in (
        "get_storage_control_word", "get_charge_current_a",
        "get_discharge_current_a", "get_backflow_power_w",
        "get_rc_setpoint_w", "get_rc_adjustment_value",
    ):
        setattr(inv, getter, MagicMock(return_value=None))
    inv.is_rc_engaged = MagicMock(return_value=None)
    # No advertised bounds: ``effective_spec`` must leave the composed spec
    # untouched. A bare MagicMock here would turn every reduced target into a
    # stray mock — including ``rc_setpoint_w``, making even a passive mode
    # look RC-backed.
    inv.limit_for = MagicMock(return_value=UNKNOWN_LIMIT)
    return inv


def _driver(inv: MagicMock | None = None) -> SolisDriver:
    return SolisDriver(inv or _inverter(), default_battery_config(), 10_000, 10_000)


def test_conforms_to_control_driver_protocol():
    assert isinstance(_driver(), InverterControlDriver)


def test_base_driver_interface_is_platform_neutral():
    # A new platform must satisfy InverterDriver with ONLY the neutral surface —
    # no Solis register readbacks. Guards against re-leaking the protocol.
    class _MinimalNeutralDriver:
        async def apply_mode(self, mode, spec, force=False): ...
        async def hold(self, mode, spec): ...
        def shutdown(self) -> None: ...
        def get_grid_power(self) -> float: return 0.0

    assert isinstance(_MinimalNeutralDriver(), InverterDriver)
    for solis_only in (
        "get_charge_current_a", "get_discharge_current_a", "get_rc_setpoint_w",
        "get_backflow_power_w", "get_rc_adjustment_value", "is_rc_engaged",
        "get_storage_control_word",
    ):
        assert not hasattr(InverterDriver, solis_only), (
            f"{solis_only} leaked back onto the platform-neutral InverterDriver"
        )


def test_factory_returns_control_driver_for_solis():
    inv = _inverter()
    context = InverterContext(
        hass=MagicMock(), entity_ids={}, get_grid_power=inv.get_grid_power,
    )
    d = make_inverter_driver(
        inv, context, InverterPlatform.SOLIS, default_battery_config(), 10_000, 10_000,
    )
    assert isinstance(d, InverterControlDriver)


def test_spec_table_built_for_dispatchable_modes():
    d = _driver()
    for mode in (StorageMode.SelfUse, StorageMode.GridCharge, StorageMode.Discharge):
        assert d.spec_for(mode) is not None
    assert d.spec_for(StorageMode.UNKNOWN) is None


def test_control_surface_no_command_yields_no_target_rows():
    rows = {r.name: r for r in _driver().control_surface(None)}
    assert rows["reg_43110"].desired is None
    assert rows["reg_43110"].match is None          # no target → neutral
    assert "rc_enable" not in rows                   # RC row only for RC modes


def test_control_surface_pairs_target_with_observed_on_match():
    inv = _inverter()
    d = _driver(inv)
    spec = d.spec_for(StorageMode.GridCharge)
    inv.get_storage_control_word = MagicMock(return_value=spec.reg_43110_value)
    inv.get_charge_current_a = MagicMock(return_value=spec.charge_a)
    inv.get_discharge_current_a = MagicMock(return_value=spec.discharge_a)
    inv.get_backflow_power_w = MagicMock(return_value=spec.export_limit_w)
    inv.get_rc_setpoint_w = MagicMock(return_value=spec.rc_setpoint_w)
    inv.get_rc_adjustment_value = MagicMock(return_value=RC_ADJUSTMENT_AC_PORT_VALUE)
    rows = {r.name: r for r in d.control_surface(StorageMode.GridCharge)}
    assert rows["reg_43110"].desired == spec.reg_43110_value
    assert rows["reg_43110"].match is True
    assert rows["rc_enable"].match is True


def test_decode_observed_delegates_to_readbacks():
    inv = _inverter()
    d = _driver(inv)
    spec = d.spec_for(StorageMode.GridCharge)
    inv.get_storage_control_word = MagicMock(return_value=spec.reg_43110_value)
    inv.get_charge_current_a = MagicMock(return_value=spec.charge_a)
    inv.get_discharge_current_a = MagicMock(return_value=spec.discharge_a)
    inv.get_backflow_power_w = MagicMock(return_value=spec.export_limit_w)
    inv.get_rc_setpoint_w = MagicMock(return_value=spec.rc_setpoint_w)
    inv.is_rc_engaged = MagicMock(return_value=True)
    assert d.decode_observed() is StorageMode.GridCharge
    # Unreadable bitmask decodes to UNKNOWN.
    inv.get_storage_control_word = MagicMock(return_value=None)
    assert d.decode_observed() is StorageMode.UNKNOWN


# ---------------------------------------------------------------------------
# Driver-owned RC liveness
#
# The keep-alive lives here, not in the control module: the ~5-min RC expiry is
# a Solis property, and the neutral driver protocol says only "still holding".
# ---------------------------------------------------------------------------


class _TrackedInterval:
    """Captures ``async_track_time_interval`` so a beat can be fired by hand."""

    def __init__(self) -> None:
        self.calls: list[tuple[timedelta, object]] = []
        self.cancels: int = 0

    def __call__(self, _hass, action, interval, *args, **kwargs):
        self.calls.append((interval, action))

        def _cancel() -> None:
            self.cancels += 1

        return _cancel

    @property
    def interval(self) -> timedelta:
        return self.calls[-1][0]

    @property
    def action(self):
        return self.calls[-1][1]


@pytest.fixture
def track_interval(monkeypatch):
    """Patch ``async_track_time_interval`` where the Heartbeat imports it."""
    tracked = _TrackedInterval()
    monkeypatch.setattr(
        "custom_components.sun_sale.outbound.heartbeat.async_track_time_interval",
        tracked,
    )
    return tracked


def _driver_with_hass(inv: MagicMock) -> SolisDriver:
    """Build a driver with a hass handle so its heartbeat actually arms."""
    return SolisDriver(
        inv, default_battery_config(), 10_000, 10_000, hass=MagicMock(),
    )


@pytest.mark.asyncio
async def test_rc_mode_arms_heartbeat_and_beat_re_arms(track_interval) -> None:
    inv = _inverter()
    drv = _driver_with_hass(inv)
    spec = drv.spec_for(StorageMode.Discharge)
    await drv.apply_mode(StorageMode.Discharge, spec)
    assert track_interval.interval == _RC_KEEPALIVE_INTERVAL
    assert inv.refresh_rc.await_count == 0  # the apply wrote a full RC window
    await track_interval.action(None)
    assert inv.refresh_rc.await_count == 1
    assert inv.refresh_rc.await_args.args[0] == spec


@pytest.mark.asyncio
async def test_passive_mode_stops_the_heartbeat(track_interval) -> None:
    """Applying a non-RC mode releases the function, so no beat may re-arm it."""
    inv = _inverter()
    drv = _driver_with_hass(inv)
    await drv.apply_mode(StorageMode.Discharge, drv.spec_for(StorageMode.Discharge))
    stale_beat = track_interval.action
    await drv.apply_mode(StorageMode.SelfUse, drv.spec_for(StorageMode.SelfUse))
    assert track_interval.cancels == 1
    # Even a beat queued behind the mode change re-arms nothing: the driver
    # re-reads what it is actually holding.
    await stale_beat(None)
    assert inv.refresh_rc.await_count == 0


@pytest.mark.asyncio
async def test_shutdown_stops_the_heartbeat(track_interval) -> None:
    inv = _inverter()
    drv = _driver_with_hass(inv)
    await drv.apply_mode(StorageMode.Discharge, drv.spec_for(StorageMode.Discharge))
    drv.shutdown()
    assert track_interval.cancels == 1


@pytest.mark.asyncio
async def test_hold_re_asserts_rc_regardless_of_anything(track_interval) -> None:
    """``hold`` is unconditional — the module states intent, the driver acts.

    Nothing about the control module's verify verdict reaches here, which is the
    point: gating the deadman on a confirmation verdict about unrelated
    registers is what let a commanded Discharge expire while every register
    still read "engaged".
    """
    inv = _inverter()
    drv = _driver_with_hass(inv)
    spec = drv.spec_for(StorageMode.Discharge)
    await drv.hold(StorageMode.Discharge, spec)
    assert inv.refresh_rc.await_count == 1


@pytest.mark.asyncio
async def test_beat_survives_a_failing_re_arm(track_interval) -> None:
    """One failed beat must not kill the timer — the next one still fires."""
    inv = _inverter()
    inv.refresh_rc = AsyncMock(side_effect=RuntimeError("modbus down"))
    drv = _driver_with_hass(inv)
    await drv.apply_mode(StorageMode.Discharge, drv.spec_for(StorageMode.Discharge))
    await track_interval.action(None)  # must not raise
    assert inv.refresh_rc.await_count == 1
    await track_interval.action(None)
    assert inv.refresh_rc.await_count == 2


@pytest.mark.asyncio
async def test_no_hass_degrades_to_hold_only() -> None:
    """Without a hass handle the timer never arms, but ``hold`` still works."""
    inv = _inverter()
    drv = _driver(inv)  # hass=None
    spec = drv.spec_for(StorageMode.Discharge)
    await drv.apply_mode(StorageMode.Discharge, spec)
    await drv.hold(StorageMode.Discharge, spec)
    assert inv.refresh_rc.await_count == 1
