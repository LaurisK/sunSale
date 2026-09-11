"""Tests for the dispatch SoC floor, grid-charge permission and dispatch decoding."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.outbound.driver_factory import soc_percent_ceil
from custom_components.sun_sale.outbound.solis_dispatch_driver import SolisDispatchDriver
from custom_components.sun_sale.outbound.solis_driver import SolisDriver
from tests.conftest import default_battery_config
from tests.test_solis_dispatch_driver import (
    _ROLES,
    _capable,
    _dispatch_calls,
    _hass,
    _inverter,
    _State,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _driver(states=None, soc_min_pct=6, permitted=None, inv=None):
    """Return (dispatch driver, inverter mock, hass stub) with the new options wired."""
    inv = inv or _inverter()
    hass = _hass(_capable(states))
    base = SolisDriver(inv, default_battery_config(), 10_000, 10_000)
    drv = SolisDispatchDriver(
        base, hass, dict(_ROLES),
        soc_min_pct=soc_min_pct, grid_charge_permitted=permitted,
    )
    return drv, inv, hass


async def _command(drv: SolisDispatchDriver, mode: StorageMode) -> None:
    """Apply ``mode`` as a routine (non-force) write, so no re-confirm is armed."""
    await drv.apply_mode(mode, drv.spec_for(mode), force=False)


@pytest.mark.parametrize("permitted", [False, True])
async def test_discharge_carries_soc_floor_and_mirrors_the_grid_charge_switch(permitted):
    drv, _, hass = _driver(permitted=lambda: permitted)
    await _command(drv, StorageMode.Discharge)
    _, payload = _dispatch_calls(hass)[0]
    assert payload["soc_min"] == 6
    assert payload["allow_grid_charge"] is permitted


async def test_grid_charge_always_permits_grid_charging():
    drv, _, hass = _driver(permitted=lambda: False)
    await _command(drv, StorageMode.GridCharge)
    _, payload = _dispatch_calls(hass)[0]
    assert payload["allow_grid_charge"] is True
    assert payload["soc_min"] == 6


async def test_unwired_options_leave_the_payload_as_before():
    drv, _, hass = _driver(soc_min_pct=None, permitted=None)
    await _command(drv, StorageMode.Discharge)
    _, payload = _dispatch_calls(hass)[0]
    assert "soc_min" not in payload
    assert "allow_grid_charge" not in payload


async def test_switch_is_read_at_write_time():
    state = {"allowed": False}
    drv, _, hass = _driver(permitted=lambda: state["allowed"])
    await _command(drv, StorageMode.Discharge)
    state["allowed"] = True
    await drv.hold(StorageMode.Discharge, drv.spec_for(StorageMode.Discharge))
    payloads = [p for s, p in _dispatch_calls(hass) if s == "solis_dispatch"]
    assert [p["allow_grid_charge"] for p in payloads] == [False, True]


@pytest.mark.parametrize(
    ("soc", "pct"), [(0.06, 6), (0.061, 7), (0.0, 0), (0.1, 10), (1.2, 100)],
)
def test_soc_percent_ceil(soc, pct):
    assert soc_percent_ceil(soc) == pct


def _dispatch_states(active: str, mode: str, target: str) -> dict[str, _State]:
    """Return dispatch readback states for the decode tests."""
    return {
        _ROLES["dispatch_active"]: _State(active),
        _ROLES["dispatch_control_mode"]: _State(mode),
        _ROLES["dispatch_power_target"]: _State(target),
    }


@pytest.mark.parametrize(
    ("target", "expected"),
    [("10000", StorageMode.Discharge), ("-5000", StorageMode.GridCharge)],
)
def test_running_dispatch_decodes_as_its_forced_mode(target, expected):
    inv = _inverter()
    inv.get_storage_control_word = MagicMock(return_value=64)
    drv, _, _ = _driver(_dispatch_states("1", "3", target), inv=inv)
    assert drv.decode_observed() is expected
    assert drv.observe(NOW).mode is expected


@pytest.mark.parametrize(
    ("active", "mode", "target"),
    [("0", "1", "0"), ("1", "3", "0"), ("1", "5", "10000")],
)
def test_released_or_foreign_dispatch_defers_to_the_register_decoder(active, mode, target):
    inv = _inverter()
    inv.get_storage_control_word = MagicMock(return_value=64)
    drv, _, _ = _driver(_dispatch_states(active, mode, target), inv=inv)
    assert drv.decode_observed() is StorageMode.FeedIn
    assert drv.observe(NOW).mode is StorageMode.FeedIn


def test_unreadable_raw_state_is_not_relabelled():
    drv, _, _ = _driver(_dispatch_states("1", "3", "10000"))
    assert drv.observe(NOW).mode is StorageMode.UNKNOWN


def test_diagnostic_snapshot_merges_register_and_dispatch_readbacks():
    inv = _inverter()
    inv.raw_states = MagicMock(return_value={"operating_mode": "4096"})
    drv, _, _ = _driver(_dispatch_states("0", "1", "0"), inv=inv)
    snap = drv.diagnostic_snapshot()
    assert snap["operating_mode"] == "4096"
    assert snap["dispatch_active"] == "0"
    assert snap["dispatch_control_mode"] == "1"
