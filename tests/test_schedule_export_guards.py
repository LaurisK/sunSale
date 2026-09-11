"""Tests for the planner's export guards: cycle-cost gate and below-min_soc refill."""
from __future__ import annotations

import pytest

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.pipeline.schedule import optimize_schedule
from tests.conftest import (
    default_battery_config,
    default_battery_state,
    default_tariff_config,
    make_price,
    make_solar,
)
from tests.test_schedule import NOW, _make_gen_series
from tests.test_slot_physics import _sim

_EXPORT = {StorageMode.Discharge, StorageMode.FeedIn}


def _plan(spots, solar=None, soc=0.90, terminal_value_discount=0.0):
    """Run the DP on hourly ``spots`` and return (schedule, price_series, cycle_cost).

    Terminal valuation is off by default so a Discharge is judged on its own
    slot reward — the case the cycle-cost gate exists for.
    """
    bc = default_battery_config()
    state = default_battery_state(soc)
    deg = degradation_cost_per_kwh(bc, state)
    prices = build_price_series(
        [make_price(h, p) for h, p in enumerate(spots)], default_tariff_config(), now=NOW,
    )
    calc = calculate(prices, _make_gen_series(solar or []), state, NOW)
    schedule = optimize_schedule(
        prices, calc, bc, state, deg, NOW,
        terminal_value_discount=terminal_value_discount,
    )
    return schedule, prices, 2.0 * deg / bc.round_trip_efficiency


def test_discharge_is_not_scheduled_below_the_cycle_cost():
    # One slot selling just under the cycle cost, then worthless slots: without
    # the gate the DP would dump the battery into the first slot (its slot
    # reward is still positive after one leg of wear).
    schedule, prices, cycle_cost = _plan([0.10, -0.05, -0.05, -0.05])
    assert 0 < prices.slots[0].sell_eur_kwh < cycle_cost
    assert all(s.mode is not StorageMode.Discharge for s in schedule.slots)


def test_discharge_is_still_scheduled_above_the_cycle_cost():
    schedule, prices, cycle_cost = _plan([0.30, -0.05, -0.05, -0.05])
    assert prices.slots[0].sell_eur_kwh > cycle_cost
    assert schedule.slots[0].mode is StorageMode.Discharge


def test_feed_in_is_not_affected_by_the_cycle_cost_gate():
    # Solar surplus at a sell price below the cycle cost may still be fed in —
    # the gate is about battery energy, not solar.
    schedule, prices, cycle_cost = _plan(
        [0.10, 0.10], solar=[make_solar(0, 4.0), make_solar(1, 4.0)], soc=0.95,
    )
    assert prices.slots[0].sell_eur_kwh < cycle_cost
    assert all(s.mode is not StorageMode.Discharge for s in schedule.slots)


def test_below_min_soc_the_schedule_starts_from_the_measured_soc():
    bc = default_battery_config()
    schedule, _, _ = _plan([0.50, 0.50, 0.50], soc=0.03)
    first = schedule.slots[0]
    assert first.mode not in _EXPORT
    # No solar, no baseload: nothing charges, so the projection stays at the
    # measured 3 % instead of jumping to the 10 % floor.
    assert first.expected_soc_after == pytest.approx(0.03)
    assert first.expected_soc_after < bc.min_soc


def test_below_min_soc_solar_refills_instead_of_being_exported():
    bc = default_battery_config()
    schedule, _, _ = _plan(
        [0.60, 0.60, 0.60], solar=[make_solar(0, 0.3)], soc=0.03,
        terminal_value_discount=0.5,
    )
    first = schedule.slots[0]
    assert first.mode not in _EXPORT
    # 0.3 kWh into a 10 kWh pack: 3 % → 6 %, still honestly below the floor.
    assert first.expected_soc_after == pytest.approx(0.06, abs=1e-6)
    assert first.expected_soc_after < bc.min_soc
    later_below_floor = [
        s for s in schedule.slots if s.expected_soc_after < bc.min_soc - 1e-9
    ]
    assert all(s.mode not in _EXPORT for s in later_below_floor)


def test_slot_physics_does_not_lift_a_below_floor_soc_to_min_soc():
    out = _sim(mode=StorageMode.SelfUse, soc_in=0.03, solar_kwh=0.3)
    assert out.soc_out == pytest.approx(0.06)


def test_slot_physics_discharge_below_floor_moves_no_battery_energy():
    out = _sim(mode=StorageMode.Discharge, soc_in=0.03)
    assert out.batt_discharge_kwh == 0.0
    assert out.soc_out == pytest.approx(0.03)
