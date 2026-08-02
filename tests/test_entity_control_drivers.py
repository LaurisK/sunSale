"""Tests for the entity-driven inverter drivers (Huawei/SolaX/Sungrow/GoodWe/Deye).

These platforms are untested against hardware, so the tests pin the *contract*:
protocol conformance, full dispatchable-mode coverage, idempotent + forced
writes through the shared actuator, the platform-neutral control surface the
verify loop relies on, and observed-mode decoding. The exact option strings /
role names are deliberately not asserted beyond what the drivers themselves
declare (hardware would be needed to validate those).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import (
    DISPATCHABLE_MODES,
    StorageMode,
)
from custom_components.sun_sale.outbound.deye_driver import make_deye_driver
from custom_components.sun_sale.outbound.driver import InverterControlDriver
from custom_components.sun_sale.outbound.entity_control import (
    EntityActuator,
    EntityControlDriver,
    EntityWrite,
    InverterContext,
)
from custom_components.sun_sale.outbound.goodwe_driver import make_goodwe_driver
from custom_components.sun_sale.outbound.huawei_driver import make_huawei_driver
from custom_components.sun_sale.outbound.solax_driver import make_solax_driver
from custom_components.sun_sale.outbound.sungrow_driver import make_sungrow_driver
from tests.conftest import default_battery_config

# --- Fakes ------------------------------------------------------------------ #


class _State:
    """Minimal HA state stub."""

    def __init__(self, value: str) -> None:
        self.state = value
        self.attributes: dict = {}


class _Hass:
    """Hass stub with per-entity state lookup + AsyncMock service tracking."""

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}
        self.services = MagicMock()
        self.services.async_call = AsyncMock()
        self.states = MagicMock()
        self.states.get = self._states.get

    def set_state(self, entity_id: str, value: str) -> None:
        self._states[entity_id] = _State(value)


def _fake_context(hass: _Hass, entity_ids: dict[str, str]) -> InverterContext:
    """Return an InverterContext over the stub hass + entity map (grid power = 1.5 kW)."""
    return InverterContext(
        hass=hass,
        entity_ids=entity_ids,
        get_grid_power=MagicMock(return_value=1.5),
    )


# Every driver's full role surface, mapped to dummy entity ids so the actuator
# resolves and writes rather than skipping. Roles are the union the plans touch;
# unused-per-driver roles are harmless.
_ROLES = {
    "work_mode_select": "select.x_work_mode",
    "use_mode_select": "select.x_use_mode",
    "manual_mode_select": "select.x_manual_mode",
    "ems_mode_select": "select.x_ems_mode",
    "forced_cmd_select": "select.x_forced_cmd",
    "operation_mode_select": "select.x_operation_mode",
    "charge_from_grid_switch": "switch.x_charge_from_grid",
    "grid_charge_switch": "switch.x_grid_charge",
    "max_feed_grid_power": "number.x_max_feed",
    "export_limit": "number.x_export_limit",
    "export_power": "number.x_export_power",
    "forced_power": "number.x_forced_power",
    "eco_power": "number.x_eco_power",
    "max_sell_power": "number.x_max_sell",
    "battery_device": "device-123",
}

_FACTORIES = {
    "huawei": make_huawei_driver,
    "solax": make_solax_driver,
    "sungrow": make_sungrow_driver,
    "goodwe": make_goodwe_driver,
    "deye": make_deye_driver,
}


def _driver(name: str) -> tuple[EntityControlDriver, _Hass]:
    hass = _Hass()
    ctx = _fake_context(hass, dict(_ROLES))
    drv = _FACTORIES[name](ctx, default_battery_config(), 6000, 10000)
    return drv, hass


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_conforms_to_control_driver_protocol(name: str) -> None:
    drv, _ = _driver(name)
    assert isinstance(drv, InverterControlDriver)


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_covers_every_dispatchable_mode(name: str) -> None:
    drv, _ = _driver(name)
    for mode in DISPATCHABLE_MODES:
        assert drv.spec_for(mode) is not None, f"{name} missing plan for {mode}"


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_grid_power_passthrough(name: str) -> None:
    drv, _ = _driver(name)
    assert drv.get_grid_power() == 1.5


_PERSISTENT_MODES = [
    m for m in DISPATCHABLE_MODES
    if m not in (StorageMode.GridCharge, StorageMode.Discharge)
]


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_persistent_modes_never_need_keepalive(name: str) -> None:
    # SelfUse / FeedIn / NoExport / StandBy hold in non-volatile config on every
    # platform — they must never request a periodic re-issue.
    drv, _ = _driver(name)
    for mode in _PERSISTENT_MODES:
        spec = drv.spec_for(mode)
        assert spec is not None
        assert drv.needs_keepalive(spec) is False


@pytest.mark.parametrize("mode", [StorageMode.GridCharge, StorageMode.Discharge])
def test_huawei_forced_modes_need_keepalive(mode: StorageMode) -> None:
    # Huawei's forcible_charge/discharge are timed commands that lapse — they
    # must be kept alive so a slot longer than the duration never stops forcing.
    drv, _ = _driver("huawei")
    spec = drv.spec_for(mode)
    assert spec is not None
    assert drv.needs_keepalive(spec) is True


@pytest.mark.asyncio
async def test_huawei_refresh_rc_reissues_forcible_service() -> None:
    # The keep-alive tick routes through refresh_rc; for a kept-alive plan it
    # must re-issue the forcible service (the volatile command being re-armed).
    drv, hass = _driver("huawei")
    spec = drv.spec_for(StorageMode.GridCharge)
    assert spec is not None
    await drv.refresh_rc(spec)
    calls = [c.args for c in hass.services.async_call.await_args_list]
    assert any(
        domain == "huawei_solar" and service == "forcible_charge"
        for domain, service, *_ in calls
    )


@pytest.mark.asyncio
async def test_entity_driver_persistent_refresh_rc_is_noop() -> None:
    # A persistent (non-keepalive) plan must not re-issue anything on refresh.
    drv, hass = _driver("solax")
    spec = drv.spec_for(StorageMode.SelfUse)
    assert spec is not None and drv.needs_keepalive(spec) is False
    await drv.refresh_rc(spec)
    assert hass.services.async_call.await_count == 0


@pytest.mark.parametrize("name", list(_FACTORIES))
@pytest.mark.asyncio
async def test_apply_mode_issues_writes(name: str) -> None:
    drv, hass = _driver(name)
    spec = drv.spec_for(StorageMode.GridCharge)
    assert spec is not None
    await drv.apply_mode(StorageMode.GridCharge, spec, force=True)
    # At least one service call was issued for a force-charge realisation.
    assert hass.services.async_call.await_count >= 1


@pytest.mark.parametrize("name", list(_FACTORIES))
@pytest.mark.asyncio
async def test_apply_mode_is_idempotent_when_readback_matches(name: str) -> None:
    drv, hass = _driver(name)
    spec = drv.spec_for(StorageMode.SelfUse)
    assert spec is not None
    # Pre-seed every targeted entity with its desired value so an unforced
    # apply should skip all writes.
    for write in spec.writes:
        if write.kind == "number":
            hass.set_state(_ROLES[write.role], str(float(write.value)))
        elif write.kind == "switch":
            hass.set_state(_ROLES[write.role], "on" if write.value else "off")
        else:
            hass.set_state(_ROLES[write.role], str(write.value))
    await drv.apply_mode(StorageMode.SelfUse, spec, force=False)
    assert hass.services.async_call.await_count == 0


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_control_surface_matches_when_readback_equals_target(name: str) -> None:
    drv, hass = _driver(name)
    spec = drv.spec_for(StorageMode.SelfUse)
    assert spec is not None
    for write in spec.writes:
        if write.kind == "number":
            hass.set_state(_ROLES[write.role], str(float(write.value)))
        elif write.kind == "switch":
            hass.set_state(_ROLES[write.role], "on" if write.value else "off")
        else:
            hass.set_state(_ROLES[write.role], str(write.value))
    rows = drv.control_surface(StorageMode.SelfUse)
    assert rows and all(r.match for r in rows)


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_control_surface_mismatch_when_unread(name: str) -> None:
    drv, _ = _driver(name)
    rows = drv.control_surface(StorageMode.SelfUse)
    # Nothing seeded → every number/select row is unread → not matching.
    assert rows and not all(r.match for r in rows)


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_decode_observed_roundtrips_self_use(name: str) -> None:
    drv, hass = _driver(name)
    spec = drv.spec_for(StorageMode.SelfUse)
    assert spec is not None
    mode_write = spec.writes[0]  # the mode select is always written first
    hass.set_state(_ROLES[mode_write.role], str(mode_write.value))
    assert drv.decode_observed() == StorageMode.SelfUse


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_observe_packs_decoded_mode(name: str) -> None:
    drv, hass = _driver(name)
    spec = drv.spec_for(StorageMode.SelfUse)
    assert spec is not None
    mode_write = spec.writes[0]
    hass.set_state(_ROLES[mode_write.role], str(mode_write.value))
    now = datetime(2026, 6, 27, 12, tzinfo=UTC)
    reading = drv.observe(now)
    assert reading.timestamp == now
    assert reading.mode == StorageMode.SelfUse
    assert reading.raw_state is None


@pytest.mark.parametrize("name", list(_FACTORIES))
def test_decode_unknown_when_unmapped(name: str) -> None:
    drv, _ = _driver(name)
    assert drv.decode_observed() == StorageMode.UNKNOWN


# Platforms whose forced modes live under an umbrella primary-mode value and are
# disambiguated by a secondary select (see each driver's _SUB_DECODE).
@pytest.mark.parametrize("name", ["solax", "sungrow"])
@pytest.mark.parametrize("mode", [StorageMode.GridCharge, StorageMode.Discharge])
def test_forced_sub_state_decode_roundtrips(name: str, mode: StorageMode) -> None:
    # Seed the live entities from the commanded plan's selects, then confirm the
    # secondary decode recovers the forced direction (not just UNKNOWN/base).
    drv, hass = _driver(name)
    spec = drv.spec_for(mode)
    assert spec is not None
    for write in spec.writes:
        if write.kind == "select":
            hass.set_state(_ROLES[write.role], str(write.value))
    assert drv.decode_observed() == mode


@pytest.mark.parametrize("name", ["solax", "sungrow"])
def test_sub_state_ignored_when_primary_mode_is_named(name: str) -> None:
    # A stale forced sub-select must NOT manufacture a forced mode while the
    # primary select reads a known (self-use) mode.
    drv, hass = _driver(name)
    self_use = drv.spec_for(StorageMode.SelfUse)
    grid_charge = drv.spec_for(StorageMode.GridCharge)
    assert self_use is not None and grid_charge is not None
    # Primary = self-use; sub-select left pointing at a forced-charge value.
    primary = self_use.writes[0]
    sub = next(w for w in grid_charge.writes if w.kind == "select" and w is not grid_charge.writes[0])
    hass.set_state(_ROLES[primary.role], str(primary.value))
    hass.set_state(_ROLES[sub.role], str(sub.value))
    assert drv.decode_observed() == StorageMode.SelfUse


# --- Actuator degradation --------------------------------------------------- #


@pytest.mark.asyncio
async def test_actuator_skips_unmapped_role() -> None:
    hass = _Hass()
    act = EntityActuator(hass, {})  # no roles mapped
    await act.apply_write(EntityWrite("missing", "number", 1.0, "x"), force=True)
    assert hass.services.async_call.await_count == 0


@pytest.mark.asyncio
async def test_apply_mode_degrades_on_empty_entity_map() -> None:
    hass = _Hass()
    ctx = _fake_context(hass, {})  # nothing resolved
    drv = make_huawei_driver(ctx, default_battery_config(), 6000, 10000)
    spec = drv.spec_for(StorageMode.Discharge)
    assert spec is not None
    await drv.apply_mode(StorageMode.Discharge, spec, force=True)
    # Every write + service is skipped (role unmapped) — no crash, no calls.
    assert hass.services.async_call.await_count == 0


def test_control_surface_observed_only_without_command() -> None:
    drv, hass = _driver("sungrow")
    rows = drv.control_surface(None)
    assert len(rows) == 1
    assert rows[0].desired is None
    assert rows[0].match is None
