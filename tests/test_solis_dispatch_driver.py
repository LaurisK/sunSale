"""Tests for SolisDispatchDriver — forced modes via the Remote Dispatch block.

Two concerns are pinned here:

  * the **payload** sunSale sends (`solis_dispatch` / `solis_dispatch_stop`),
    field by field, so an accidental rename or a dropped failsafe is a test
    failure rather than a battery that quietly stops discharging; and
  * the **delegation boundary** — everything that is not a forced mode must
    reach the register driver unchanged, because that path is the one already
    validated against hardware.

The upstream half of the contract (that ``solis_modbus`` still accepts these
service names and fields) is guarded separately in
``test_solis_modbus_contract.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import UNKNOWN_LIMIT, StorageMode
from custom_components.sun_sale.outbound.driver import InverterControlDriver
from custom_components.sun_sale.outbound.solis_dispatch_driver import (
    DISPATCH_CAPABLE_MAGIC,
    SERVICE_DISPATCH,
    SERVICE_DISPATCH_STOP,
    SolisDispatchDriver,
    dispatch_supported,
)
from custom_components.sun_sale.outbound.solis_driver import SolisDriver
from tests.conftest import default_battery_config

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

_ROLES = {
    "dispatch_capability": "sensor.solis_remote_dispatch_capability",
    "dispatch_active": "sensor.solis_dispatch_active",
    "dispatch_control_mode": "sensor.solis_dispatch_control_mode",
    "dispatch_power_target": "sensor.solis_dispatch_power_target",
    "dispatch_failsafe_interval": "sensor.solis_dispatch_failsafe_interval",
}


class _State:
    def __init__(self, value: str) -> None:
        self.state = value


def _hass(states: dict[str, _State] | None = None) -> MagicMock:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.services.has_service = MagicMock(return_value=True)
    hass.states.get = (states or {}).get
    return hass


def _inverter() -> MagicMock:
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
    inv.limit_for = MagicMock(return_value=UNKNOWN_LIMIT)
    return inv


def _driver(states: dict[str, _State] | None = None):
    """Return (dispatch driver, wrapped inverter mock, hass stub)."""
    inv = _inverter()
    hass = _hass(states)
    base = SolisDriver(inv, default_battery_config(), 10_000, 10_000)
    return SolisDispatchDriver(base, hass, dict(_ROLES)), inv, hass


def _dispatch_calls(hass: MagicMock) -> list[tuple[str, dict]]:
    """Return ``(service, payload)`` for every solis_modbus service call."""
    return [
        (c.args[1], c.args[2])
        for c in hass.services.async_call.await_args_list
        if c.args[0] == "solis_modbus"
    ]


def test_conforms_to_the_control_driver_protocol() -> None:
    drv, _, _ = _driver()
    assert isinstance(drv, InverterControlDriver)


# --- Forced modes ----------------------------------------------------------- #


@pytest.mark.asyncio
async def test_discharge_dispatches_grid_export_with_failsafe() -> None:
    drv, _, hass = _driver()
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    service, payload = _dispatch_calls(hass)[0]
    assert service == SERVICE_DISPATCH
    assert payload["mode"] == "grid_export"
    assert payload["power_watts"] == 10_000
    # The failsafe is the deadman — a dispatch without one would revert on the
    # inverter's own default, which is what the RC path already got wrong.
    assert payload["failsafe_minutes"] == 20


@pytest.mark.asyncio
async def test_grid_charge_dispatches_grid_import() -> None:
    drv, _, hass = _driver()
    await drv.apply_mode(
        StorageMode.GridCharge, drv.spec_for(StorageMode.GridCharge),
    )
    _, payload = _dispatch_calls(hass)[0]
    assert payload["mode"] == "grid_import"
    # Magnitude is positive; the block's own sign convention carries direction.
    assert payload["power_watts"] > 0


@pytest.mark.asyncio
async def test_forced_mode_still_applies_the_register_composition() -> None:
    """Dispatch steers power *within* a mode; it does not replace the mode."""
    drv, inv, _ = _driver()
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    inv.apply_mode.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_magnitude_ignores_the_rc_entity_ceiling() -> None:
    """The ±10 kW RC bound is an entity bound; dispatch does not go through it.

    Reducing the commanded magnitude by it would under-drive the inverter for
    no physical reason — the exact ceiling the dispatch path exists to escape.
    """
    from custom_components.sun_sale.contract.models import Limit

    inv = _inverter()
    inv.limit_for = MagicMock(return_value=Limit(low=-10_000, high=10_000, known=True))
    hass = _hass()
    base = SolisDriver(inv, default_battery_config(), 20_000, 15_000)
    drv = SolisDispatchDriver(base, hass, dict(_ROLES))
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    _, payload = _dispatch_calls(hass)[0]
    assert payload["power_watts"] == 15_000  # declared, not the 10 kW clamp


# --- Passive modes + release ------------------------------------------------ #


@pytest.mark.asyncio
async def test_passive_mode_releases_dispatch_before_delegating() -> None:
    """A stale power target under a passive mode would fight the register path."""
    drv, inv, hass = _driver()
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    await drv.apply_mode(StorageMode.SelfUse, drv.spec_for(StorageMode.SelfUse))
    services = [s for s, _ in _dispatch_calls(hass)]
    assert services == [SERVICE_DISPATCH, SERVICE_DISPATCH_STOP]
    assert inv.apply_mode.await_count == 2


@pytest.mark.asyncio
async def test_passive_mode_without_prior_dispatch_does_not_call_stop() -> None:
    """Never dispatched → nothing to release; SolisCloud's hold stays untouched."""
    drv, _, hass = _driver()
    await drv.apply_mode(StorageMode.SelfUse, drv.spec_for(StorageMode.SelfUse))
    assert _dispatch_calls(hass) == []


@pytest.mark.asyncio
async def test_hold_re_pushes_the_block_to_refresh_the_failsafe() -> None:
    drv, _, hass = _driver()
    spec = drv.spec_for(StorageMode.Discharge)
    await drv.apply_mode(StorageMode.Discharge, spec)
    await drv.hold(StorageMode.Discharge, spec)
    assert [s for s, _ in _dispatch_calls(hass)] == [SERVICE_DISPATCH] * 2


@pytest.mark.asyncio
async def test_hold_on_a_passive_mode_delegates_to_the_register_driver() -> None:
    drv, inv, hass = _driver()
    spec = drv.spec_for(StorageMode.SelfUse)
    await drv.hold(StorageMode.SelfUse, spec)
    assert _dispatch_calls(hass) == []
    inv.refresh_rc.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_service_call_is_logged_not_raised() -> None:
    """A rejected call must not abort the rest of the mode's actions."""
    drv, inv, hass = _driver()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("no controller"))
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    inv.apply_mode.assert_awaited_once()  # register composition still applied


# --- Control surface -------------------------------------------------------- #


@pytest.mark.asyncio
async def test_control_surface_matches_when_the_inverter_agrees() -> None:
    states = {
        _ROLES["dispatch_active"]: _State("1"),
        _ROLES["dispatch_control_mode"]: _State("3"),
        _ROLES["dispatch_power_target"]: _State("10000"),
        _ROLES["dispatch_failsafe_interval"]: _State("20"),
    }
    drv, _, _ = _driver(states)
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    rows = {r.name: r for r in drv.control_surface(StorageMode.Discharge)}
    for name in (
        "dispatch_active", "dispatch_control_mode",
        "dispatch_power_target", "dispatch_failsafe",
    ):
        assert rows[name].status == "match", name


@pytest.mark.asyncio
async def test_control_surface_flags_a_foreign_write_as_drift() -> None:
    """SolisCloud stealing the block must surface as drift, not silence."""
    states = {
        _ROLES["dispatch_active"]: _State("1"),
        _ROLES["dispatch_control_mode"]: _State("1"),  # battery_hold, not ours
        _ROLES["dispatch_power_target"]: _State("0"),
        _ROLES["dispatch_failsafe_interval"]: _State("1440"),
    }
    drv, _, _ = _driver(states)
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    rows = {r.name: r for r in drv.control_surface(StorageMode.Discharge)}
    assert rows["dispatch_control_mode"].status == "mismatch"
    assert rows["dispatch_power_target"].status == "mismatch"


def test_unreadable_dispatch_sensors_are_unknown_not_mismatch() -> None:
    """An unreadable readback is a comms gap, not a control fault."""
    drv, _, _ = _driver()
    rows = {r.name: r for r in drv.control_surface(StorageMode.Discharge)}
    assert rows["dispatch_active"].status == "unknown"


def test_passive_mode_surface_targets_dispatch_off() -> None:
    """A dispatch sunSale failed to release must show as drift."""
    states = {_ROLES["dispatch_active"]: _State("1")}
    drv, _, _ = _driver(states)
    rows = {r.name: r for r in drv.control_surface(StorageMode.SelfUse)}
    assert rows["dispatch_active"].desired == 0
    assert rows["dispatch_active"].status == "mismatch"


def test_no_commanded_mode_adds_no_dispatch_rows() -> None:
    drv, _, _ = _driver()
    names = {r.name for r in drv.control_surface(None)}
    assert not any(n.startswith("dispatch_") for n in names)


# --- Capability ------------------------------------------------------------- #


def test_capability_drops_the_rc_leg() -> None:
    """The RC entity's ±10 kW does not bound a path that bypasses the entity.

    With the RC leg the reference install's grid-discharge cap is pinned at
    10 kW by an entity declaration; without it the binding legs are the real
    ones — battery current (400 A x 48 V = 19.2 kW) and the export cap (20 kW).
    """
    from custom_components.sun_sale.contract.models import Limit

    limits = {
        "battery_max_charge_current": Limit(low=0, high=400, known=True),
        "battery_max_discharge_current": Limit(low=0, high=400, known=True),
        "backflow_power": Limit(low=0, high=20_000, known=True),
        "rc_setpoint": Limit(low=-10_000, high=10_000, known=True),
    }
    inv = _inverter()
    inv.limit_for = MagicMock(side_effect=lambda role: limits[role])
    hass = _hass()
    base = SolisDriver(inv, default_battery_config(), 20_000, 15_000)
    drv = SolisDispatchDriver(base, hass, dict(_ROLES))
    assert base.capability(NOW).max_grid_discharge_kw == 10.0
    assert drv.capability(NOW).max_grid_discharge_kw == pytest.approx(19.2)
    # The RC leg is not merely ignored — it is not reported as a degraded
    # (unresolvable) control point either, because it is not a leg at all here.
    assert "rc_setpoint" not in drv.capability(NOW).degraded


# --- Capability gate -------------------------------------------------------- #


def test_dispatch_supported_requires_service_and_magic() -> None:
    states = {_ROLES["dispatch_capability"]: _State(str(DISPATCH_CAPABLE_MAGIC))}
    hass = _hass(states)
    assert dispatch_supported(hass, dict(_ROLES)) is True


def test_dispatch_unsupported_without_the_service() -> None:
    states = {_ROLES["dispatch_capability"]: _State(str(DISPATCH_CAPABLE_MAGIC))}
    hass = _hass(states)
    hass.services.has_service = MagicMock(return_value=False)
    assert dispatch_supported(hass, dict(_ROLES)) is False


def test_dispatch_unsupported_when_the_inverter_says_so() -> None:
    hass = _hass({_ROLES["dispatch_capability"]: _State("0")})
    assert dispatch_supported(hass, dict(_ROLES)) is False


def test_dispatch_unsupported_without_the_capability_sensor() -> None:
    assert dispatch_supported(_hass(), {}) is False


# --- RC stand-down (two mechanisms must never drive at once) ---------------- #


@pytest.mark.asyncio
async def test_forced_mode_stands_the_rc_function_down() -> None:
    """Dispatch holds the power target, so RC must not also be engaged.

    Handing the base driver the mode's own spec would engage selector + timeout
    + setpoint *and* arm its 3-minute heartbeat, alongside the dispatch block —
    two controllers fighting, one of which expires after ~5 minutes.
    """
    drv, inv, _ = _driver()
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    applied = inv.apply_mode.await_args.args[1]
    assert applied.rc_setpoint_w == 0
    # The rest of the composition is untouched — dispatch steers power *within*
    # the mode, it does not replace it.
    declared = drv.spec_for(StorageMode.Discharge)
    assert applied.reg_43110_value == declared.reg_43110_value
    assert applied.export_limit_w == declared.export_limit_w
    assert applied.discharge_a == declared.discharge_a


@pytest.mark.asyncio
async def test_rc_rows_are_compared_against_what_was_written() -> None:
    """The stood-down RC rows must not report a mismatch nothing can resolve."""
    inv = _inverter()
    inv.get_rc_setpoint_w = MagicMock(return_value=0)
    inv.get_rc_adjustment_value = MagicMock(return_value=0)
    hass = _hass()
    base = SolisDriver(inv, default_battery_config(), 10_000, 10_000)
    drv = SolisDispatchDriver(base, hass, dict(_ROLES))
    await drv.apply_mode(
        StorageMode.Discharge, drv.spec_for(StorageMode.Discharge),
    )
    rows = {r.name: r for r in drv.control_surface(StorageMode.Discharge)}
    assert rows["rc_setpoint_w"].status == "match"
    # The selector row only exists for an RC-backed spec; standing RC down
    # removes it from the surface entirely.
    assert "rc_enable" not in rows
