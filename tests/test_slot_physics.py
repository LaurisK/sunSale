"""Tests for pipeline/slot_physics.py — per-mode energy flow physics.

Pure Python, no HA required. Each mode gets dedicated tests that probe:
  - the basic flow split (load covered, surplus disposed, battery moved)
  - boundary conditions (empty/full battery, no solar, no load)
  - negative sell price (price-aware modes curtail; price-blind modes don't)
  - mass balance (every kWh in is accounted for)
  - SoC and reward arithmetic.
"""
from __future__ import annotations

import pytest

from custom_components.sun_sale.contract.models import BatteryConfig, StorageMode
from custom_components.sun_sale.pipeline.slot_physics import (
    SlotOutcome,
    simulate_slot,
)
from tests.conftest import default_battery_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sim(
    *,
    mode: StorageMode,
    soc_in: float = 0.50,
    solar_kwh: float = 0.0,
    baseload_kwh: float = 0.0,
    buy_eur_kwh: float = 0.20,
    sell_eur_kwh: float = 0.10,
    slot_hours: float = 1.0,
    battery_cfg: BatteryConfig | None = None,
    est_capacity_kwh: float = 10.0,
    deg_cost_eur_kwh: float = 0.04,
    export_limit_kw: float | None = None,
) -> SlotOutcome:
    """Convenience wrapper for simulate_slot with sensible defaults."""
    return simulate_slot(
        soc_in=soc_in,
        mode=mode,
        solar_kwh=solar_kwh,
        baseload_kwh=baseload_kwh,
        buy_eur_kwh=buy_eur_kwh,
        sell_eur_kwh=sell_eur_kwh,
        slot_hours=slot_hours,
        battery_cfg=battery_cfg or default_battery_config(),
        est_capacity_kwh=est_capacity_kwh,
        deg_cost_eur_kwh=deg_cost_eur_kwh,
        export_limit_kw=export_limit_kw,
    )


def _assert_mass_balance(
    outcome: SlotOutcome,
    *,
    solar_kwh: float,
    baseload_kwh: float,
    eff: float,
) -> None:
    """Energy balance: AC inflow = AC outflow.

    AC sources:  solar + grid_in + batt_discharge
    AC sinks:    baseload + grid_out + batt_charge + curtailed

    Storage-side: charge is loss-free (1 AC in → 1 storage); discharge
    bakes the round-trip loss into the AC delivered (1 storage → eff AC).
    Curtailment counts on the sink side because the surplus existed and
    was dissipated.
    """
    sources = solar_kwh + outcome.grid_in_kwh + outcome.batt_discharge_kwh
    sinks = (
        baseload_kwh
        + outcome.grid_out_kwh
        + outcome.batt_charge_kwh
        + outcome.curtailed_kwh
    )
    assert sources == pytest.approx(sinks, abs=1e-9), (
        f"mass balance fail: sources={sources}, sinks={sinks}, outcome={outcome}"
    )


# ---------------------------------------------------------------------------
# StandBy
# ---------------------------------------------------------------------------


def test_stby_idle_battery_solar_covers_load():
    """StandBy: solar covers load entirely; battery untouched, no grid flow."""
    cfg = default_battery_config()
    out = _sim(mode=StorageMode.StandBy, solar_kwh=1.0, baseload_kwh=1.0)
    assert out.batt_charge_kwh == 0.0
    assert out.batt_discharge_kwh == 0.0
    assert out.grid_in_kwh == 0.0
    assert out.grid_out_kwh == 0.0
    assert out.curtailed_kwh == 0.0
    assert out.soc_out == pytest.approx(0.50)
    _assert_mass_balance(out, solar_kwh=1.0, baseload_kwh=1.0, eff=cfg.round_trip_efficiency)


def test_stby_charges_surplus_but_never_discharges():
    """StandBy holds (no drain) but still soaks surplus solar; export stays off."""
    cfg = default_battery_config()
    out = _sim(mode=StorageMode.StandBy, solar_kwh=3.0, baseload_kwh=1.0, sell_eur_kwh=0.15)
    # surplus = 2.0 kWh → charges (headroom 4.5, max 5.0 both allow); export off.
    assert out.batt_charge_kwh == pytest.approx(2.0)
    assert out.batt_discharge_kwh == 0.0
    assert out.grid_out_kwh == 0.0
    assert out.curtailed_kwh == 0.0
    assert out.soc_out == pytest.approx(0.70)
    _assert_mass_balance(out, solar_kwh=3.0, baseload_kwh=1.0, eff=cfg.round_trip_efficiency)


def test_stby_load_only_imports_from_grid():
    """StandBy: no solar, only load → battery untouched, grid imports load."""
    out = _sim(mode=StorageMode.StandBy, solar_kwh=0.0, baseload_kwh=0.5)
    assert out.grid_in_kwh == pytest.approx(0.5)
    assert out.batt_discharge_kwh == 0.0


# ---------------------------------------------------------------------------
# SelfUse  /  NoExport  (self-use family)
# ---------------------------------------------------------------------------


def test_self_use_battery_covers_load_deficit():
    """SelfUse: solar < load → battery discharges to cover the deficit."""
    cfg = default_battery_config()
    out = _sim(mode=StorageMode.SelfUse, solar_kwh=0.5, baseload_kwh=2.0, soc_in=0.50)
    # deficit = 1.5 kWh AC; drawdown allows it (4 kWh storage × 0.9 eff = 3.6 AC).
    assert out.batt_discharge_kwh == pytest.approx(1.5)
    assert out.grid_in_kwh == 0.0
    assert out.batt_charge_kwh == 0.0
    # soc lost = 1.5 / eff / cap = 1.5 / 0.9 / 10 ≈ 0.1667
    assert out.soc_out == pytest.approx(0.50 - 1.5 / 0.9 / 10.0)
    _assert_mass_balance(out, solar_kwh=0.5, baseload_kwh=2.0, eff=cfg.round_trip_efficiency)


def test_self_use_surplus_charges_battery_then_exports():
    """SelfUse: solar surplus first charges battery, remainder exports unlimited."""
    cfg = default_battery_config()
    # headroom at soc=0.5 = (0.95-0.5)*10 = 4.5; max_charge_kwh = 5.0; → batt soaks up to 4.5
    out = _sim(
        mode=StorageMode.SelfUse,
        solar_kwh=8.0,
        baseload_kwh=1.0,
        soc_in=0.50,
        sell_eur_kwh=0.12,
    )
    assert out.batt_charge_kwh == pytest.approx(4.5)
    assert out.grid_out_kwh == pytest.approx(8.0 - 1.0 - 4.5)
    assert out.curtailed_kwh == 0.0
    assert out.soc_out == pytest.approx(0.95)
    _assert_mass_balance(out, solar_kwh=8.0, baseload_kwh=1.0, eff=cfg.round_trip_efficiency)


def test_self_use_exports_even_when_sell_negative():
    """SelfUse has no price awareness — exports surplus even when paying to."""
    # SelfUse (uncapped) has no price awareness; surplus that exceeds headroom goes out.
    out = _sim(
        mode=StorageMode.SelfUse,
        solar_kwh=10.0,
        baseload_kwh=0.0,
        soc_in=0.50,
        sell_eur_kwh=-0.05,
    )
    # headroom soaks 4.5 kWh; remaining 5.5 kWh exports anyway (no curtailment in SelfUse).
    assert out.batt_charge_kwh == pytest.approx(4.5)
    assert out.grid_out_kwh == pytest.approx(5.5)
    assert out.curtailed_kwh == 0.0
    # reward includes the loss on the 5.5 kWh export
    assert out.reward_eur < 0


def test_store_caps_export_above_limit():
    """SelfUse: export cap clips export; above-cap surplus curtails."""
    out = _sim(
        mode=StorageMode.SelfUse,
        solar_kwh=10.0,
        baseload_kwh=0.0,
        soc_in=0.50,
        sell_eur_kwh=0.12,
        export_limit_kw=3.0,    # 3 kWh cap
    )
    assert out.batt_charge_kwh == pytest.approx(4.5)  # headroom
    leftover = 10.0 - 4.5
    assert out.grid_out_kwh == pytest.approx(3.0)
    assert out.curtailed_kwh == pytest.approx(leftover - 3.0)


def test_hoard_curtails_all_surplus():
    """NoExport: no export at all — surplus that exceeds headroom curtails."""
    out = _sim(
        mode=StorageMode.NoExport,
        solar_kwh=8.0,
        baseload_kwh=1.0,
        soc_in=0.50,
        sell_eur_kwh=0.20,    # even positive — NoExport never exports
    )
    surplus_after_load = 8.0 - 1.0
    assert out.batt_charge_kwh == pytest.approx(4.5)
    assert out.grid_out_kwh == 0.0
    assert out.curtailed_kwh == pytest.approx(surplus_after_load - 4.5)


def test_hoard_still_discharges_for_baseload():
    """NoExport: no export, but battery still covers deficit (it's self-use)."""
    out = _sim(mode=StorageMode.NoExport, solar_kwh=0.0, baseload_kwh=2.0, soc_in=0.50)
    assert out.batt_discharge_kwh == pytest.approx(2.0)
    assert out.grid_in_kwh == 0.0


def test_self_use_at_min_soc_falls_back_to_grid():
    """At min_soc the battery cannot discharge — load comes from grid."""
    cfg = default_battery_config()
    out = _sim(
        mode=StorageMode.SelfUse,
        solar_kwh=0.0,
        baseload_kwh=1.0,
        soc_in=cfg.min_soc,
    )
    assert out.batt_discharge_kwh == 0.0
    assert out.grid_in_kwh == pytest.approx(1.0)


def test_self_use_at_max_soc_cannot_charge():
    """At max_soc the battery cannot charge — surplus skips storage."""
    cfg = default_battery_config()
    out = _sim(
        mode=StorageMode.SelfUse,
        solar_kwh=4.0,
        baseload_kwh=0.0,
        soc_in=cfg.max_soc,
        sell_eur_kwh=0.10,
    )
    assert out.batt_charge_kwh == 0.0
    assert out.grid_out_kwh == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# GridCharge
# ---------------------------------------------------------------------------


def test_gulp_charges_battery_at_max_rate():
    """GridCharge: battery charges at min(max_charge, headroom); load also from grid."""
    out = _sim(mode=StorageMode.GridCharge, solar_kwh=0.0, baseload_kwh=0.5, soc_in=0.50)
    # max_charge_kwh = 5.0, headroom = 4.5 → batt_charge = 4.5
    assert out.batt_charge_kwh == pytest.approx(4.5)
    assert out.grid_in_kwh == pytest.approx(4.5 + 0.5)
    assert out.batt_discharge_kwh == 0.0


def test_gulp_full_battery_no_charge_grid_still_serves_load():
    """GridCharge at max_soc — no charging, but baseload still imports."""
    cfg = default_battery_config()
    out = _sim(mode=StorageMode.GridCharge, solar_kwh=0.0, baseload_kwh=0.3, soc_in=cfg.max_soc)
    assert out.batt_charge_kwh == 0.0
    assert out.grid_in_kwh == pytest.approx(0.3)


def test_gulp_curtails_solar_surplus():
    """GridCharge locks export to 0 by spec; solar above baseload curtails."""
    out = _sim(
        mode=StorageMode.GridCharge,
        solar_kwh=2.0,
        baseload_kwh=0.5,
        soc_in=0.50,
        sell_eur_kwh=0.20,
    )
    # solar surplus over baseload = 1.5 kWh → curtailed
    assert out.curtailed_kwh == pytest.approx(1.5)
    assert out.grid_out_kwh == 0.0


def test_gulp_solar_offsets_baseload_import():
    """GridCharge: solar absorbs into baseload first, reducing grid import."""
    out = _sim(mode=StorageMode.GridCharge, solar_kwh=0.5, baseload_kwh=2.0, soc_in=0.50)
    # batt_charge = 4.5; baseload_from_solar = 0.5; baseload_from_grid = 1.5
    assert out.batt_charge_kwh == pytest.approx(4.5)
    assert out.grid_in_kwh == pytest.approx(4.5 + 1.5)


# ---------------------------------------------------------------------------
# Discharge
# ---------------------------------------------------------------------------


def test_dump_discharges_battery_and_exports():
    """Discharge: battery drains at max rate; AC after baseload exports."""
    cfg = default_battery_config()
    out = _sim(mode=StorageMode.Discharge, solar_kwh=0.0, baseload_kwh=0.5, soc_in=0.50)
    # storage drawdown = 4 kWh; max_discharge_storage = 5/0.9 ≈ 5.56 → storage drained = 4
    # batt_discharge_ac = 4 * 0.9 = 3.6
    assert out.batt_discharge_kwh == pytest.approx(3.6)
    # baseload covered from battery's AC output; rest exports
    assert out.grid_out_kwh == pytest.approx(3.6 - 0.5)
    assert out.grid_in_kwh == 0.0
    assert out.curtailed_kwh == 0.0


def test_dump_at_min_soc_exports_solar_only():
    """Discharge at min_soc: battery empty, solar still exports, deficit imports."""
    cfg = default_battery_config()
    out = _sim(
        mode=StorageMode.Discharge,
        solar_kwh=2.0,
        baseload_kwh=1.5,
        soc_in=cfg.min_soc,
    )
    assert out.batt_discharge_kwh == 0.0
    # solar 2.0 - baseload 1.5 = 0.5 net AC → exports
    assert out.grid_out_kwh == pytest.approx(0.5)
    assert out.grid_in_kwh == 0.0


def test_dump_exports_even_at_negative_sell_price():
    """Discharge is uncapped — runs even at negative prices. Reward is negative."""
    out = _sim(mode=StorageMode.Discharge, solar_kwh=0.0, baseload_kwh=0.0, sell_eur_kwh=-0.10)
    assert out.grid_out_kwh == pytest.approx(3.6)
    assert out.curtailed_kwh == 0.0
    assert out.reward_eur < 0    # paying to export


# ---------------------------------------------------------------------------
# FeedIn
# ---------------------------------------------------------------------------


def test_sell_exports_solar_up_to_cap_then_charges():
    """FeedIn: solar exports first; over-cap surplus charges battery."""
    out = _sim(
        mode=StorageMode.FeedIn,
        solar_kwh=8.0,
        baseload_kwh=1.0,
        soc_in=0.50,
        sell_eur_kwh=0.15,
        export_limit_kw=3.0,    # 3 kWh cap
    )
    # baseload from solar = 1.0; remaining solar = 7.0
    # export = min(7.0, 3.0) = 3.0
    # over_cap = 4.0; batt_charge = min(4.0, 4.5 headroom, 5.0 max) = 4.0
    assert out.grid_out_kwh == pytest.approx(3.0)
    assert out.batt_charge_kwh == pytest.approx(4.0)
    assert out.curtailed_kwh == 0.0


def test_sell_discharges_battery_for_load_deficit():
    """FeedIn: native feed-in priority covers load from the battery, not the grid."""
    out = _sim(mode=StorageMode.FeedIn, solar_kwh=0.0, baseload_kwh=1.0, soc_in=0.50)
    # deficit 1.0 kWh AC; drawdown 4.0 storage × 0.9 = 3.6 AC available → the
    # battery covers it fully, nothing imported.
    assert out.batt_discharge_kwh == pytest.approx(1.0)
    assert out.grid_in_kwh == pytest.approx(0.0)


def test_sell_curtails_when_battery_full_and_export_capped():
    """FeedIn with full battery and tight cap → over-cap surplus curtails."""
    cfg = default_battery_config()
    out = _sim(
        mode=StorageMode.FeedIn,
        solar_kwh=8.0,
        baseload_kwh=0.0,
        soc_in=cfg.max_soc,
        sell_eur_kwh=0.15,
        export_limit_kw=3.0,
    )
    assert out.grid_out_kwh == pytest.approx(3.0)
    assert out.batt_charge_kwh == 0.0
    assert out.curtailed_kwh == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# SoC and reward arithmetic
# ---------------------------------------------------------------------------


def test_soc_clamped_within_bounds_on_overshoot():
    """End-of-slot SoC must be inside [min_soc, max_soc]."""
    cfg = default_battery_config()
    # Try to massively over-discharge: solar 0, huge baseload, low SoC
    out = _sim(
        mode=StorageMode.SelfUse,
        solar_kwh=0.0,
        baseload_kwh=20.0,
        soc_in=0.30,
    )
    assert cfg.min_soc - 1e-9 <= out.soc_out <= cfg.max_soc + 1e-9


def test_reward_matches_revenue_cost_degradation_formula():
    """reward = grid_out·sell − grid_in·buy − throughput·deg."""
    cfg = default_battery_config()
    out = _sim(
        mode=StorageMode.GridCharge,
        solar_kwh=0.0,
        baseload_kwh=0.0,
        soc_in=0.50,
        buy_eur_kwh=0.10,
        sell_eur_kwh=0.30,
        deg_cost_eur_kwh=0.05,
    )
    # batt_charge = 4.5 kWh; grid_in = 4.5; throughput_storage = 4.5
    expected = 0.0 * 0.30 - 4.5 * 0.10 - 4.5 * 0.05
    assert out.reward_eur == pytest.approx(expected)


def test_reward_credits_discharge_revenue():
    """Discharge reward includes sell × grid_out − deg × storage_drained."""
    out = _sim(
        mode=StorageMode.Discharge,
        solar_kwh=0.0,
        baseload_kwh=0.0,
        soc_in=0.50,
        buy_eur_kwh=0.10,
        sell_eur_kwh=0.30,
        deg_cost_eur_kwh=0.05,
    )
    # storage_drained = 4 kWh; batt_discharge_ac = 3.6; grid_out = 3.6; grid_in = 0
    expected = 3.6 * 0.30 - 0.0 * 0.10 - 4.0 * 0.05
    assert out.reward_eur == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 15-min slots
# ---------------------------------------------------------------------------


def test_quarter_hour_slot_scales_power_limits():
    """A 15-min slot should yield 1/4 of the per-hour energy throughput."""
    out_hour = _sim(mode=StorageMode.GridCharge, soc_in=0.10, slot_hours=1.0)
    out_quarter = _sim(mode=StorageMode.GridCharge, soc_in=0.10, slot_hours=0.25)
    # max_charge_kwh halves for a 15-min slot: 5*0.25 = 1.25
    assert out_quarter.batt_charge_kwh == pytest.approx(1.25)
    assert out_hour.batt_charge_kwh > out_quarter.batt_charge_kwh


# ---------------------------------------------------------------------------
# Mass-balance sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [
    StorageMode.StandBy,
    StorageMode.SelfUse,
    StorageMode.NoExport,
    StorageMode.GridCharge,
    StorageMode.Discharge,
    StorageMode.FeedIn,
])
@pytest.mark.parametrize("scenario", [
    {"solar": 0.0, "load": 0.0, "soc": 0.5, "sell": 0.10},
    {"solar": 2.0, "load": 1.0, "soc": 0.5, "sell": 0.10},
    {"solar": 6.0, "load": 0.5, "soc": 0.5, "sell": 0.10},
    {"solar": 6.0, "load": 0.5, "soc": 0.5, "sell": -0.05},
    {"solar": 0.5, "load": 3.0, "soc": 0.5, "sell": 0.10},
    {"solar": 0.0, "load": 0.0, "soc": 0.10, "sell": 0.10},
    {"solar": 0.0, "load": 0.0, "soc": 0.95, "sell": 0.10},
])
def test_mass_balance_holds_for_every_mode(mode, scenario):
    """Across modes × scenarios: every kWh in is accounted for."""
    cfg = default_battery_config()
    out = _sim(
        mode=mode,
        solar_kwh=scenario["solar"],
        baseload_kwh=scenario["load"],
        soc_in=scenario["soc"],
        sell_eur_kwh=scenario["sell"],
        export_limit_kw=5.0,
    )
    _assert_mass_balance(
        out, solar_kwh=scenario["solar"], baseload_kwh=scenario["load"],
        eff=cfg.round_trip_efficiency,
    )
    # SoC must be inside bounds.
    assert cfg.min_soc - 1e-9 <= out.soc_out <= cfg.max_soc + 1e-9
