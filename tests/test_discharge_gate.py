"""Tests for the discharge gate: running sell average, overfill plan, two-pass schedule.

Pure Python, no HA required. The unit tests pin ``fold_running_average`` and
``plan_overfill`` on hand-built inputs; the scenario tests drive
``optimize_schedule`` over a two-day hourly horizon (today 2024-01-15, tomorrow
2024-01-16, UTC) where the default battery is 10 kWh, 5 kW, 10-95 % SoC and a
slot sells for ``spot - 0.025`` (the default flat tariff). The battery cycle
cost is therefore ~0.0926 EUR/kWh, i.e. a spot of about 0.118 is the least that
may discharge.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.sun_sale.contract.models import (
    BaseLoadProfile,
    BaseLoadSlot,
    BatteryState,
    CalculationResult,
    DegradationCost,
    GenerationSeries,
    GenerationSlot,
    PriceSeries,
    PriceSlot,
    RunningSellAverage,
    Schedule,
    SchedulePolicy,
    StorageMode,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline import discharge_gate
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.pipeline.dag_engine import NodeContext
from custom_components.sun_sale.pipeline.nodes.tier4 import ScheduleNode
from custom_components.sun_sale.pipeline.schedule import optimize_schedule
from tests.conftest import (
    BASE_DT,
    default_battery_config,
    default_battery_state,
    default_tariff_config,
    make_price,
)

NOW = BASE_DT                       # 2024-01-15 00:00 UTC
TODAY = date(2024, 1, 15)
DAY1 = BASE_DT
DAY2 = BASE_DT + timedelta(days=1)
D = StorageMode.Discharge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot(start: datetime, sell: float, *, priced: bool = True) -> PriceSlot:
    """Build an hourly PriceSlot with the given sell price."""
    return PriceSlot(
        start=start, end=start + timedelta(hours=1), spot_eur_kwh=sell,
        buy_eur_kwh=sell + 0.1, sell_eur_kwh=sell, sources=(), priced=priced,
    )


def _series(slots: list[PriceSlot]) -> PriceSeries:
    """Wrap slots into a PriceSeries."""
    return PriceSeries(slots=tuple(slots), resolution=timedelta(hours=1), computed_at=NOW)


def _prices(day1: dict[int, float], day2: dict[int, float] | None = None,
            base1: float = 0.10, base2: float = 0.10):
    """Return 48 hourly PriceEntry rows; ``dayN`` overrides spot per hour."""
    rows = [make_price(h, day1.get(h, base1), DAY1) for h in range(24)]
    rows += [make_price(h, (day2 or {}).get(h, base2), DAY2) for h in range(24)]
    return rows


def _gen(day2_kwh: dict[int, float]) -> GenerationSeries:
    """Return a generation series with energy only on tomorrow's listed hours."""
    slots = tuple(
        GenerationSlot(
            start=DAY2.replace(hour=h), end=DAY2.replace(hour=h) + timedelta(hours=1),
            expected_kwh=kwh,
        )
        for h, kwh in sorted(day2_kwh.items())
    )
    return GenerationSeries(slots=slots)


def plan(prices, *, avg: float | None, soc: float = 0.90, solar: dict[int, float] | None = None,
         enabled: bool = True, boost: float = 0.2, **kwargs) -> Schedule:
    """Run ``optimize_schedule`` over the two-day horizon with the gate settings."""
    bc = default_battery_config()
    state = default_battery_state(soc)
    deg = degradation_cost_per_kwh(bc, state)
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    calc = calculate(series, _gen(solar or {}), state, NOW)
    return optimize_schedule(
        series, calc, bc, state, deg, NOW, local_tz=UTC,
        running_average=None if avg is None else RunningSellAverage(avg, TODAY),
        discharge_gate_enabled=enabled, overfill_boost=boost, **kwargs,
    )


def _sell_by_start(schedule: Schedule, prices) -> dict[datetime, float]:
    """Map slot start to sell price using the same tariff the plan was built with."""
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    return {s.start: s.sell_eur_kwh for s in series.slots}


# ---------------------------------------------------------------------------
# fold_running_average
# ---------------------------------------------------------------------------


def _day_series(today_sells: list[float], *, priced: bool = True) -> PriceSeries:
    """Series whose only slots are today's, one per hour from 00:00 UTC."""
    return _series([_slot(DAY1 + timedelta(hours=i), s, priced=priced)
                    for i, s in enumerate(today_sells)])


def test_cold_start_seeds_with_todays_mean():
    got = discharge_gate.fold_running_average(None, _day_series([0.10, 0.30]), TODAY, UTC)
    assert got == RunningSellAverage(value_eur_kwh=pytest.approx(0.20), day=TODAY)


def test_new_day_keeps_ninety_percent_of_previous():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 14))
    got = discharge_gate.fold_running_average(stored, _day_series([0.10, 0.30]), TODAY, UTC)
    assert got.value_eur_kwh == pytest.approx(0.9 * 0.50 + 0.1 * 0.20)
    assert got.day == TODAY


def test_same_day_returns_the_stored_object_untouched():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=TODAY)
    got = discharge_gate.fold_running_average(stored, _day_series([9.0]), TODAY, UTC)
    assert got is stored            # identity is how the coordinator skips the save


def test_a_stored_figure_from_the_future_is_not_rewound():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 16))
    assert discharge_gate.fold_running_average(
        stored, _day_series([0.1]), TODAY, UTC) is stored


def test_no_priced_slot_today_keeps_what_was_stored():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 14))
    # Only forecast-filled placeholders: they must not move the average.
    series = _day_series([0.9, 0.9], priced=False)
    assert discharge_gate.fold_running_average(stored, series, TODAY, UTC) is stored
    assert discharge_gate.fold_running_average(None, series, TODAY, UTC) is None


def test_only_todays_local_day_counts():
    other_day = _slot(DAY2, 5.0)        # tomorrow's price must not leak in
    series = _series([_slot(DAY1, 0.10), other_day])
    got = discharge_gate.fold_running_average(None, series, TODAY, UTC)
    assert got.value_eur_kwh == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# plan_overfill (hand-built inputs)
# ---------------------------------------------------------------------------


def _plan_inputs(*, tomorrow_solar: float = 6.0, soc: float = 0.95):
    """48 hourly slots; tomorrow's 08:00-15:59 carry ``tomorrow_solar`` kWh each."""
    slots = [_slot(DAY1 + timedelta(hours=i), 0.10) for i in range(48)]
    solar = [tomorrow_solar if 32 <= i < 40 else 0.0 for i in range(48)]
    return dict(
        slots=slots, solar_kwh=solar, baseload_kwh=[0.0] * 48,
        soc_before=[soc] * 48, taken=[False] * 48, eligible=[True] * 48,
        drain_ac_kwh=lambda i: 5.0, now=NOW, local_tz=UTC, boost=0.2,
        max_charge_kwh=5.0, cap_kwh=10.0, max_soc=0.95, floor_soc=0.10, eff=0.9,
    )


def test_overfill_is_surplus_beyond_the_room_and_sheds_in_the_priciest_slots():
    inputs = _plan_inputs()
    inputs["slots"][20] = _slot(inputs["slots"][20].start, 0.40)    # best
    inputs["slots"][19] = _slot(inputs["slots"][19].start, 0.30)    # second
    got = discharge_gate.plan_overfill(**inputs)
    # 8 slots x min(7.2, 5 charge cap) = 40 kWh with zero room.
    assert got.overfill_kwh == pytest.approx(40.0)
    # (0.95-0.10) x 10 kWh x 0.9 = 7.65 AC kWh to shed -> two 5 kWh slots.
    assert got.slot_indexes == (19, 20)


def test_no_overfill_when_the_battery_has_room_for_boosted_solar():
    inputs = _plan_inputs(tomorrow_solar=1.0, soc=0.10)             # 8 x 1.2 = 9.6 vs 8.5 room
    got = discharge_gate.plan_overfill(**inputs)
    assert got.overfill_kwh == pytest.approx(1.1)
    inputs = _plan_inputs(tomorrow_solar=0.5, soc=0.10)             # 4.8 fits in 8.5
    assert discharge_gate.plan_overfill(**inputs) == discharge_gate.OverfillPlan()


def test_boost_decides_whether_a_surplus_overflows():
    inputs = _plan_inputs(tomorrow_solar=1.0, soc=0.50)             # room 4.5 kWh, solar 8 kWh
    inputs["solar_kwh"] = [0.5625 if 32 <= i < 40 else 0.0 for i in range(48)]   # 4.5 raw
    assert discharge_gate.plan_overfill(**{**inputs, "boost": 0.0}).overfill_kwh == 0.0
    assert discharge_gate.plan_overfill(**{**inputs, "boost": 0.2}).overfill_kwh == pytest.approx(0.9)


def test_taken_and_ineligible_slots_are_left_alone():
    inputs = _plan_inputs()
    inputs["slots"][20] = _slot(inputs["slots"][20].start, 0.40)
    inputs["slots"][19] = _slot(inputs["slots"][19].start, 0.30)
    inputs["taken"][20] = True                  # the first pass already sells here
    inputs["eligible"][19] = False              # below the battery cycle cost
    got = discharge_gate.plan_overfill(**inputs)
    assert 19 not in got.slot_indexes and 20 not in got.slot_indexes
    assert len(got.slot_indexes) == 2


def test_only_slots_before_the_sun_arrives_are_candidates():
    inputs = _plan_inputs()
    inputs["slots"][36] = _slot(inputs["slots"][36].start, 9.0)     # midday tomorrow
    got = discharge_gate.plan_overfill(**inputs)
    assert all(i < 32 for i in got.slot_indexes)


def test_shed_is_limited_to_what_the_battery_holds_above_the_floor():
    inputs = _plan_inputs(soc=0.20)
    inputs["max_soc"] = 0.95
    got = discharge_gate.plan_overfill(**inputs)
    # (0.20-0.10) x 10 x 0.9 = 0.9 AC kWh -> one slot is enough; overfill itself is large.
    assert got.overfill_kwh > 30.0
    assert len(got.slot_indexes) == 1


def test_a_sliver_above_the_floor_is_not_worth_a_forced_discharge():
    got = discharge_gate.plan_overfill(**_plan_inputs(soc=0.12))   # 0.18 AC kWh to shed
    assert got.overfill_kwh > 30.0 and got.slot_indexes == ()


def test_battery_at_the_floor_has_nothing_to_shed():
    got = discharge_gate.plan_overfill(**_plan_inputs(soc=0.10))
    assert got.overfill_kwh > 0 and got.slot_indexes == ()


def test_load_eats_into_the_surplus():
    inputs = _plan_inputs(tomorrow_solar=2.0, soc=0.10)             # 2.4 per slot boosted
    inputs["baseload_kwh"] = [2.4 if 32 <= i < 40 else 0.0 for i in range(48)]
    assert discharge_gate.plan_overfill(**inputs) == discharge_gate.OverfillPlan()


def test_no_local_timezone_or_capacity_disables_the_plan():
    assert discharge_gate.plan_overfill(**{**_plan_inputs(), "local_tz": None}) \
        == discharge_gate.OverfillPlan()
    assert discharge_gate.plan_overfill(**{**_plan_inputs(), "cap_kwh": 0.0}) \
        == discharge_gate.OverfillPlan()


def test_no_solar_tomorrow_means_no_overfill():
    assert discharge_gate.plan_overfill(**_plan_inputs(tomorrow_solar=0.0)) \
        == discharge_gate.OverfillPlan()


# ---------------------------------------------------------------------------
# optimize_schedule: first pass (price gate)
# ---------------------------------------------------------------------------

# A moderate morning peak (sell 0.275) that the plain DP trades and then refills
# from a cheap valley, and a high evening peak (sell 0.575). A running average of
# 0.50 sits between the two.
_TWO_PEAKS = {
    **{h: 0.30 for h in range(8, 11)},
    **{h: 0.02 for h in range(11, 16)},
    **{h: 0.60 for h in range(18, 21)},
}
_BAR = 0.50


def test_gate_off_is_the_plain_dp_and_still_carries_the_average():
    prices = _prices(_TWO_PEAKS)
    plain = plan(prices, avg=None, enabled=False)
    with_avg = plan(prices, avg=_BAR, enabled=False)
    assert [s.mode for s in with_avg.slots] == [s.mode for s in plain.slots]
    assert with_avg.gate.active is False
    assert with_avg.gate.average == RunningSellAverage(_BAR, TODAY)
    assert with_avg.gate.overfill_slots == ()


def test_plain_dp_sells_below_the_average_so_the_gate_has_something_to_do():
    prices = _prices(_TWO_PEAKS)
    sells = _sell_by_start(plan(prices, avg=None, enabled=False), prices)
    plain = plan(prices, avg=None, enabled=False)
    assert any(s.mode is D and sells[s.start] < _BAR for s in plain.slots)


def test_gate_discharges_only_at_or_above_the_running_average():
    prices = _prices(_TWO_PEAKS)
    gated = plan(prices, avg=_BAR)
    sells = _sell_by_start(gated, prices)
    discharge = [s for s in gated.slots if s.mode is D]
    assert gated.gate.active is True
    assert discharge, "the evening peak must still be sold"
    assert all(sells[s.start] >= _BAR - 1e-9 for s in discharge)


def test_average_above_every_price_stops_all_grid_discharge():
    prices = _prices(_TWO_PEAKS)
    gated = plan(prices, avg=5.0)
    assert not any(s.mode is D for s in gated.slots)


def test_fixed_sell_price_is_never_gated_out():
    prices = _prices({}, base1=0.20, base2=0.20)                   # one price everywhere
    sells = list(_sell_by_start(plan(prices, avg=None), prices).values())
    avg = sum(sells) / len(sells)
    gated = plan(prices, avg=avg)
    plain = plan(prices, avg=None, enabled=False)
    assert [s.mode for s in gated.slots] == [s.mode for s in plain.slots]


def test_gate_without_an_average_is_inactive():
    prices = _prices(_TWO_PEAKS)
    got = plan(prices, avg=None)
    assert got.gate.active is False
    assert [s.mode for s in got.slots] == [s.mode for s in plan(prices, avg=None, enabled=False).slots]


def test_gate_is_moot_when_discharge_to_grid_is_not_allowed():
    prices = _prices(_TWO_PEAKS)
    got = plan(prices, avg=_BAR, allow_discharge_to_grid=False)
    assert got.gate.active is False
    assert not any(s.mode is D for s in got.slots)


# ---------------------------------------------------------------------------
# optimize_schedule: second pass (overfill)
# ---------------------------------------------------------------------------

# Tomorrow's sun: 8 hours of 6 kWh against a battery that is already full.
_BIG_SUN = {h: 6.0 for h in range(8, 16)}
# Flat, discharge-eligible prices with 19:00 and 20:00 the best of the night.
_EVENING = {19: 0.22, 20: 0.25}


def test_overfill_turns_the_best_unused_slots_into_discharge_slots():
    prices = _prices(_EVENING, base1=0.15, base2=0.15)
    got = plan(prices, avg=0.30, soc=0.95, solar=_BIG_SUN)
    modes = {s.start.hour + 24 * (s.start.day - 15): s for s in got.slots}
    forced = {DAY1.replace(hour=19), DAY1.replace(hour=20)}
    assert got.gate.overfill_kwh > 30.0
    assert set(got.gate.overfill_slots) == forced
    assert modes[19].mode is D and modes[20].mode is D
    assert "makes room for tomorrow's solar" in modes[20].reason
    # Every other slot is still gated: nothing else sells below the 0.30 bar.
    assert [s for s in got.slots if s.mode is D and s.start not in forced] == []
    # The room was actually made: the battery is at its floor when the sun comes up.
    sunrise = next(s for s in got.slots if s.start == DAY2.replace(hour=7))
    assert sunrise.expected_soc_after == pytest.approx(0.10, abs=0.02)


def test_no_overfill_pass_when_tomorrow_fits_the_battery():
    prices = _prices(_EVENING, base1=0.15, base2=0.15)
    got = plan(prices, avg=0.30, soc=0.30, solar={8: 1.0})
    assert got.gate.overfill_kwh == 0.0
    assert got.gate.overfill_slots == ()
    assert not any(s.mode is D for s in got.slots)


def test_boost_sizes_the_overfill():
    # Room at 08:00 is (0.95-0.60) x 10 = 3.5 kWh; tomorrow's 08:00 sun is 3.45 kWh,
    # which fits as forecast but not once raised by 20 % (4.14 kWh).
    prices = _prices(_EVENING, base1=0.15, base2=0.15)
    plain = plan(prices, avg=0.30, soc=0.60, solar={8: 3.45}, boost=0.0)
    boosted = plan(prices, avg=0.30, soc=0.60, solar={8: 3.45}, boost=0.2)
    assert plain.gate.overfill_kwh == 0.0 and plain.gate.overfill_slots == ()
    assert boosted.gate.overfill_kwh == pytest.approx(0.64)
    assert len(boosted.gate.overfill_slots) == 1
    assert DAY1.replace(hour=20) in boosted.gate.overfill_slots


def test_an_overfill_too_small_to_shed_forces_nothing():
    # 3.0 kWh raised by 20 % overflows the 3.5 kWh room by 0.1 kWh - not worth a slot.
    prices = _prices(_EVENING, base1=0.15, base2=0.15)
    got = plan(prices, avg=0.30, soc=0.60, solar={8: 3.0}, boost=0.2)
    assert got.gate.overfill_kwh == pytest.approx(0.1)
    assert got.gate.overfill_slots == ()


def test_overfill_never_sells_below_the_battery_cycle_cost():
    # Spot 0.05 -> sell 0.025, under the ~0.0926 cycle cost: nothing is eligible.
    prices = _prices({}, base1=0.05, base2=0.05)
    got = plan(prices, avg=0.30, soc=0.95, solar=_BIG_SUN)
    assert got.gate.overfill_kwh > 0
    assert got.gate.overfill_slots == ()
    assert not any(s.mode is D for s in got.slots)


def test_gate_off_never_runs_the_overfill_pass():
    prices = _prices(_EVENING, base1=0.15, base2=0.15)
    got = plan(prices, avg=0.30, soc=0.95, solar=_BIG_SUN, enabled=False)
    assert got.gate.overfill_kwh == 0.0 and got.gate.overfill_slots == ()


# ---------------------------------------------------------------------------
# ScheduleNode: the average is folded every cycle and handed back for saving
# ---------------------------------------------------------------------------


def _ctx(stored: RunningSellAverage | None, *, enabled: bool) -> NodeContext:
    """A NodeContext holding exactly what ScheduleNode reads."""
    bc = default_battery_config()
    state = default_battery_state(0.9)
    series = build_price_series(_prices(_TWO_PEAKS), default_tariff_config(), now=NOW)
    primary = {
        PriceSeries: series,
        CalculationResult: calculate(series, _gen({}), state, NOW),
        BatteryState: state,
        DegradationCost: DegradationCost(value_kwh=degradation_cost_per_kwh(bc, state)),
        BaseLoadProfile: BaseLoadProfile(
            slots=tuple(BaseLoadSlot(hour=h, baseload_kw=0.0, sample_count=0, is_fallback=True) for h in range(24)),
            fallback_kw=0.0, overall_p10_kw=0.0, overall_median_kw=0.0,
            confidence=None, sample_count=0, distinct_days=0, computed_at=NOW,
        ),
        GenerationSeries: _gen({}),
        SchedulePolicy: SchedulePolicy(discharge_gate_enabled=enabled),
        RunningSellAverage: stored,
    }
    config = SimpleNamespace(local_tz=UTC, battery=bc, forecast_reserve_enabled=False)
    return NodeContext(primary=primary, secondary={}, config=config, now=NOW)


async def test_node_seeds_the_average_on_a_cold_start():
    schedule = await ScheduleNode()._compute(_ctx(None, enabled=False))
    avg = schedule.gate.average
    assert avg is not None and avg.day == TODAY
    todays = [s.sell_eur_kwh for s in
              build_price_series(_prices(_TWO_PEAKS), default_tariff_config(), now=NOW).slots
              if s.start.date() == TODAY]
    assert avg.value_eur_kwh == pytest.approx(sum(todays) / len(todays))


async def test_node_returns_the_stored_object_when_the_day_has_not_changed():
    stored = RunningSellAverage(value_eur_kwh=0.30, day=TODAY)
    schedule = await ScheduleNode()._compute(_ctx(stored, enabled=True))
    assert schedule.gate.average is stored
    assert schedule.gate.active is True


async def test_node_folds_when_the_day_moves_on():
    stored = RunningSellAverage(value_eur_kwh=0.30, day=date(2024, 1, 14))
    schedule = await ScheduleNode()._compute(_ctx(stored, enabled=True))
    assert schedule.gate.average is not stored
    assert schedule.gate.average.day == TODAY
    assert schedule.gate.average.value_eur_kwh != pytest.approx(0.30)
