"""Tests for pipeline/storage_mode_specs.py — pure Python, no HA required."""
from __future__ import annotations

import pytest

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.outbound.storage_mode_specs import (
    build_specs,
    decode_mode,
)
from tests.conftest import default_battery_config


# ---------------------------------------------------------------------------
# build_specs — concrete register targets
# ---------------------------------------------------------------------------


def _specs():
    """Return the canonical spec table for default test fixtures."""
    return build_specs(
        battery_config=default_battery_config(),
        export_max_w=10_000,
        inverter_max_power_w=10_000,
    )


def test_build_specs_covers_every_applied_mode():
    # UNKNOWN is observed-only and intentionally excluded.
    specs = _specs()
    applied = {m for m in StorageMode if m != StorageMode.UNKNOWN}
    assert set(specs.keys()) == applied


@pytest.mark.parametrize(
    "mode, expected_reg",
    [
        (StorageMode.FeedIn, 64),
        (StorageMode.SelfUse, 1),
        (StorageMode.NoExport, 1),
        (StorageMode.Discharge, 64),
        (StorageMode.GridCharge, 33),
        (StorageMode.StandBy, 1),
        (StorageMode.TRACK, 1),
    ],
)
def test_build_specs_register_bitmasks_match_doc(mode, expected_reg):
    # Bitmasks come straight from docs/solis_control.md §3.
    assert _specs()[mode].reg_43110_value == expected_reg


def test_grid_charge_pushes_grid_charge_setpoint_negative():
    # GridCharge forces grid → battery, so RC active-power setpoint is negative.
    bc = default_battery_config()
    spec = build_specs(bc, export_max_w=10_000, inverter_max_power_w=10_000)[StorageMode.GridCharge]
    assert spec.rc_setpoint_w == -int(bc.max_charge_power_kw * 1000)
    assert spec.discharge_a == 0.0
    assert spec.charge_a is not None and spec.charge_a > 0


def test_discharge_pushes_export_setpoint_positive_and_raises_export_cap():
    # The cap is written explicitly (not inherited) so a preceding
    # NoExport/GridCharge/StandBy slot's 0 W cap cannot clamp the dump.
    spec = _specs()[StorageMode.Discharge]
    assert spec.rc_setpoint_w == 10_000
    assert spec.export_limit_w == 10_000
    assert spec.charge_a == 0.0
    assert spec.discharge_a is not None and spec.discharge_a > 0


def test_no_export_zeros_export_limit():
    spec = _specs()[StorageMode.NoExport]
    assert spec.export_limit_w == 0


@pytest.mark.parametrize("mode", [StorageMode.SelfUse, StorageMode.NoExport])
def test_self_use_family_allows_battery_discharge(mode):
    # slot_physics._simulate_self_use covers baseload from the battery;
    # the spec must not write a 0 A discharge limit that blocks it.
    spec = _specs()[mode]
    assert spec.discharge_a is not None and spec.discharge_a > 0


def test_feed_in_allows_battery_discharge():
    # Native feed-in priority serves household load from the battery; the spec
    # must not write a 0 A discharge limit (the bug that forced grid imports
    # while the battery sat idle).
    spec = _specs()[StorageMode.FeedIn]
    assert spec.discharge_a is not None and spec.discharge_a > 0


def test_standby_bars_discharge_but_allows_charge():
    # StandBy = battery hold: discharge barred (preserve charge for later), but
    # charging from surplus solar stays enabled. Export stays disabled.
    spec = _specs()[StorageMode.StandBy]
    assert spec.discharge_a == 0.0
    assert spec.charge_a is not None and spec.charge_a > 0
    assert spec.export_limit_w == 0
    assert spec.rc_setpoint_w == 0


def test_amps_derived_from_battery_voltage():
    bc = default_battery_config()
    expected_a = (bc.max_charge_power_kw * 1000) / bc.nominal_voltage_v
    assert _specs()[StorageMode.FeedIn].charge_a == pytest.approx(expected_a)


def test_build_specs_safe_with_zero_voltage_fallback():
    bc = default_battery_config()
    # Use object.__setattr__ because BatteryConfig is frozen.
    object.__setattr__(bc, "nominal_voltage_v", 0.0)
    specs = build_specs(bc, export_max_w=10_000, inverter_max_power_w=10_000)
    # Falls back to 48 V — should not divide-by-zero.
    assert specs[StorageMode.FeedIn].charge_a == pytest.approx(
        (bc.max_charge_power_kw * 1000) / 48.0
    )


# ---------------------------------------------------------------------------
# decode_mode — observed register + currents → StorageMode
# ---------------------------------------------------------------------------


def test_decode_none_register_is_unknown():
    assert decode_mode(None, 10.0, 0.0, 0) == StorageMode.UNKNOWN


def test_decode_unrecognised_bitmask_is_unknown():
    # bit 2 (Off-Grid) on its own is not in the planner's vocabulary.
    assert decode_mode(4, 0.0, 0.0, 0) == StorageMode.UNKNOWN


def test_decode_self_use_no_discharge_is_standby():
    # discharge_a == 0 → StandBy; charge_a is irrelevant (a hold may still charge).
    assert decode_mode(1, 0.0, 0.0, 0) == StorageMode.StandBy
    assert decode_mode(1, 50.0, 0.0, 0) == StorageMode.StandBy


def test_decode_self_use_with_discharge_is_self_use():
    # Discharge enabled + backflow unknown → backwards-compat: assume SelfUse.
    assert decode_mode(1, 50.0, 50.0, 0) == StorageMode.SelfUse


def test_decode_self_use_with_export_allowed_is_self_use():
    # Discharge enabled, backflow > 0 → export permitted → SelfUse.
    assert decode_mode(1, 50.0, 50.0, 0, backflow_power_w=10_000) == StorageMode.SelfUse


def test_decode_self_use_with_zero_backflow_is_no_export():
    # Discharge enabled but Solis EMS zeroes backflow_power to suppress export
    # while leaving reg=1. The decoder must read NoExport to match.
    assert decode_mode(1, 50.0, 50.0, 0, backflow_power_w=0) == StorageMode.NoExport


def test_decode_standby_ignores_backflow():
    # discharge_a == 0 → StandBy regardless of backflow value.
    assert decode_mode(1, 0.0, 0.0, 0, backflow_power_w=0) == StorageMode.StandBy
    assert decode_mode(1, 0.0, 0.0, 0, backflow_power_w=10_000) == StorageMode.StandBy


def test_decode_grid_charge_bitmask_is_grid_charge():
    assert decode_mode(33, 50.0, 0.0, -3000) == StorageMode.GridCharge


def test_decode_feed_in_without_rc_is_feed_in():
    # RC released (setpoint 0) → FeedIn, even with discharge enabled (RC, not
    # discharge_a, is the discriminator from Discharge).
    assert decode_mode(64, 50.0, 50.0, 0) == StorageMode.FeedIn


def test_decode_feed_in_with_rc_export_is_discharge():
    # 43110=64 + positive RC setpoint, engagement unknown → trust the sign.
    assert decode_mode(64, 50.0, 50.0, 5000) == StorageMode.Discharge


def test_decode_discharge_requires_engaged_rc_selector():
    # Stale +setpoint but selector released → FeedIn (the function has expired).
    assert decode_mode(64, 50.0, 50.0, 5000, rc_engaged=False) == StorageMode.FeedIn
    # Selector engaged confirms a live forced discharge.
    assert decode_mode(64, 50.0, 50.0, 5000, rc_engaged=True) == StorageMode.Discharge
    # Selector unreadable → best-effort fall back to the setpoint sign.
    assert decode_mode(64, 50.0, 50.0, 5000, rc_engaged=None) == StorageMode.Discharge


def test_decode_handles_none_currents_as_zero():
    # Translator may pass None when the number entity is unavailable.
    assert decode_mode(1, None, None, None) == StorageMode.StandBy


def test_decode_round_trip_applied_modes_match_build_specs():
    """For every applied mode, applying its spec to the inverter and reading
    the register + ancillary fields back should reproduce the same mode."""
    specs = _specs()
    # TRACK collapses to StandBy/SelfUse in the decoder by design — it is not
    # part of the round-trip set (see decode_mode docstring).
    unambiguous = {
        StorageMode.FeedIn,
        StorageMode.SelfUse,
        StorageMode.NoExport,
        StorageMode.Discharge,
        StorageMode.GridCharge,
        StorageMode.StandBy,
    }
    for mode in unambiguous:
        spec = specs[mode]
        decoded = decode_mode(
            spec.reg_43110_value,
            spec.charge_a,
            spec.discharge_a,
            spec.rc_setpoint_w,
            spec.export_limit_w,
        )
        assert decoded == mode, f"{mode} round-trip failed → {decoded}"


