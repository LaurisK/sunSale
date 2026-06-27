"""Tests for SolisDriver — the extracted Solis control surface / decoder."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.outbound.driver import (
    InverterControlDriver,
    InverterDriver,
)
from custom_components.sun_sale.outbound.inverter import (
    RC_ADJUSTMENT_AC_PORT_VALUE,
    InverterPlatform,
)
from custom_components.sun_sale.outbound.entity_control import InverterContext
from custom_components.sun_sale.outbound.driver_factory import make_inverter_driver
from custom_components.sun_sale.outbound.solis_driver import SolisDriver
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
        async def refresh_rc(self, spec): ...
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
