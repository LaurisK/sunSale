"""Tests for the discharge gate: running sell average and the single boosted-sun DP.

Pure Python, no HA required. The unit tests pin ``fold_running_average``; the
scenario tests drive ``optimize_schedule`` over a two-day hourly horizon (today
2024-01-15, tomorrow 2024-01-16, UTC) where the default battery is 10 kWh, 5 kW,
10-95 % SoC and a slot sells for ``spot - 0.025`` (the default flat tariff). The
battery cycle cost is therefore ~0.0926 EUR/kWh, i.e. a spot of about 0.118 is
the least that may discharge.
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
    DerivedPowerHistory,
    GenerationSeries,
    GenerationSlot,
    PriceSeries,
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
    make_trading_history,
)

NOW = BASE_DT                       # 2024-01-15 00:00 UTC
TODAY = date(2024, 1, 15)
DAY1 = BASE_DT
DAY2 = BASE_DT + timedelta(days=1)
D = StorageMode.Discharge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
         enabled: bool = True, boost: float = 0.2, now: datetime = NOW,
         **kwargs) -> Schedule:
    """Run ``optimize_schedule`` over the two-day horizon with the gate settings."""
    bc = default_battery_config()
    state = default_battery_state(soc)
    deg = degradation_cost_per_kwh(bc, state)
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    calc = calculate(series, _gen(solar or {}), state, now)
    return optimize_schedule(
        series, calc, bc, state, deg, now, local_tz=UTC,
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


def test_cold_start_seeds_with_the_realized_mean():
    got = discharge_gate.fold_running_average(None, 0.20, TODAY)
    assert got == RunningSellAverage(value_eur_kwh=pytest.approx(0.20), day=TODAY)


def test_new_day_keeps_ninety_percent_of_previous():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 14))
    got = discharge_gate.fold_running_average(stored, 0.20, TODAY)
    assert got.value_eur_kwh == pytest.approx(0.9 * 0.50 + 0.1 * 0.20)
    assert got.day == TODAY


def test_same_day_returns_the_stored_object_untouched():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=TODAY)
    got = discharge_gate.fold_running_average(stored, 9.0, TODAY)
    assert got is stored            # identity is how the coordinator skips the save


def test_a_stored_figure_from_the_future_is_not_rewound():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 16))
    assert discharge_gate.fold_running_average(stored, 0.1, TODAY) is stored


def test_a_day_without_a_sale_keeps_the_value_and_moves_the_day():
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 14))
    got = discharge_gate.fold_running_average(stored, None, TODAY)
    assert got == RunningSellAverage(value_eur_kwh=0.50, day=TODAY)


def test_nothing_ever_sold_and_no_price_leaves_the_average_unset():
    assert discharge_gate.fold_running_average(None, None, TODAY) is None


def test_a_fresh_start_with_no_trading_seeds_from_the_seed_price():
    got = discharge_gate.fold_running_average(None, None, TODAY, seed=0.45)
    assert got == RunningSellAverage(value_eur_kwh=0.45, day=TODAY)


def test_the_seed_is_ignored_once_there_is_history():
    realized = discharge_gate.fold_running_average(None, 0.20, TODAY, seed=0.45)
    assert realized.value_eur_kwh == pytest.approx(0.20)
    stored = RunningSellAverage(value_eur_kwh=0.50, day=date(2024, 1, 14))
    kept = discharge_gate.fold_running_average(stored, None, TODAY, seed=0.99)
    assert kept.value_eur_kwh == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# optimize_schedule: the price bar
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
# optimize_schedule: planning for a stronger sun
# ---------------------------------------------------------------------------

# 23:00 on day 1, a battery that still holds most of its charge. Tonight sells
# 0.095 (spot 0.12) - barely above the cycle cost - while the grid charge at
# 00:00-05:00 costs 0.097 to buy, so any sale tonight is a loss once the energy
# is bought back. Tomorrow has a morning price that peaks at 09:00 and a dear
# evening. (The live incident of 2026-10-11/12: the second pass sold at 0.080
# at 23:05, grid-charged at 0.129 from 23:50, then shed in the cheaper 06:45-07:30
# slots while 08:15-09:00 sold higher.)
_LATE = DAY1.replace(hour=23)
_NIGHT_AND_PEAKS = (
    {23: 0.12},
    {**{h: 0.04 for h in range(0, 6)}, 7: 0.16, 8: 0.19, 9: 0.22, 10: 0.17, 11: 0.15,
     19: 0.45, 20: 0.45},
)


def _sun(kwh_per_hour: float) -> dict[int, float]:
    """Tomorrow's sun: ``kwh_per_hour`` from 08:00 to 15:59."""
    return {h: kwh_per_hour for h in range(8, 16)}


def test_gate_does_not_sell_tonight_only_to_buy_the_energy_back():
    prices = _prices(*_NIGHT_AND_PEAKS, base1=0.10, base2=0.10)
    plain = plan(prices, avg=0.02, soc=0.90, solar=_sun(1.2), enabled=False, now=_LATE)
    gated = plan(prices, avg=0.02, soc=0.90, solar=_sun(1.2), now=_LATE)
    # Nothing is sold before the morning's best price: not tonight's 0.095, not
    # the cheaper early-morning slots a "before the sun" rule would pick.
    assert not any(s.mode is D and s.start < DAY2.replace(hour=9) for s in gated.slots)
    # Leaving room for a stronger sun costs a hedge, not the €0.8 the forced
    # sales cost.
    assert gated.total_expected_profit_eur > plain.total_expected_profit_eur - 0.1


def test_gate_sheds_stored_energy_in_the_best_priced_morning_slot():
    # The room for the sun is made at 09:00, the day's best morning price - a slot
    # after the first sunny one (08:00), which a "before the sun arrives" rule
    # never considered.
    prices = _prices(*_NIGHT_AND_PEAKS, base1=0.10, base2=0.10)
    gated = plan(prices, avg=0.02, soc=0.90, solar=_sun(1.2), now=_LATE)
    morning = [s for s in gated.slots
               if s.mode is D and DAY2.replace(hour=6) <= s.start < DAY2.replace(hour=12)]
    assert [s.start.hour for s in morning] == [9]


def test_gate_never_forces_a_sale_below_the_average():
    # Tomorrow's sun overflows a full battery, but every slot sells under the 0.30
    # bar: nothing may be sold to make room, and nothing is.
    prices = _prices({19: 0.22, 20: 0.25}, base1=0.15, base2=0.15)
    got = plan(prices, avg=0.30, soc=0.95, solar=_sun(6.0))
    assert got.gate.active is True
    assert not any(s.mode is D for s in got.slots)


def test_gate_never_sells_below_the_battery_cycle_cost():
    # Spot 0.05 -> sell 0.025, under the ~0.0926 cycle cost: nothing may discharge.
    prices = _prices({}, base1=0.05, base2=0.05)
    got = plan(prices, avg=0.0, soc=0.95, solar=_sun(6.0))
    assert not any(s.mode is D for s in got.slots)


def test_boost_changes_the_plan_only_where_there_is_sun():
    prices = _prices(*_NIGHT_AND_PEAKS, base1=0.10, base2=0.10)
    boosted = plan(prices, avg=0.02, soc=0.12, boost=0.5, now=_LATE)
    plain = plan(prices, avg=0.02, soc=0.12, boost=0.0, now=_LATE)
    assert [s.mode for s in boosted.slots] == [s.mode for s in plain.slots]


def test_gate_off_ignores_the_boost():
    prices = _prices(*_NIGHT_AND_PEAKS, base1=0.10, base2=0.10)
    a = plan(prices, avg=0.02, soc=0.12, solar=_sun(1.2), enabled=False, boost=0.0, now=_LATE)
    b = plan(prices, avg=0.02, soc=0.12, solar=_sun(1.2), enabled=False, boost=0.5, now=_LATE)
    assert [s.mode for s in a.slots] == [s.mode for s in b.slots]


def test_the_reported_trajectory_follows_the_real_forecast_not_the_boosted_one():
    # The policy is built for 20 % more sun, but the plan the user sees charges
    # the battery only from the sun that is actually forecast: 8 h x 0.4 kWh.
    prices = _prices(*_NIGHT_AND_PEAKS, base1=0.10, base2=0.10)
    got = plan(prices, avg=0.02, soc=0.12, solar=_sun(0.4), now=_LATE)
    before = next(s for s in got.slots if s.start == DAY2.replace(hour=7)).expected_soc_after
    after = next(s for s in got.slots if s.start == DAY2.replace(hour=15)).expected_soc_after
    assert after - before <= 8 * 0.4 / 10.0 + 1e-6


# ---------------------------------------------------------------------------
# ScheduleNode: the average is folded every cycle and handed back for saving
# ---------------------------------------------------------------------------


YESTERDAY = DAY1 - timedelta(days=1)
# The hours of yesterday the battery charged from / discharged to the grid, and
# the spot price in each (the rest of yesterday sits at 0.10).
_BOUGHT_HOUR, _BOUGHT_SPOT = 3, 0.05
_SOLD_HOUR, _SOLD_SPOT = 18, 0.30


def _yesterday_prices():
    """Return 24 hourly PriceEntry rows for yesterday with a cheap and a dear hour."""
    spots = {_BOUGHT_HOUR: _BOUGHT_SPOT, _SOLD_HOUR: _SOLD_SPOT}
    return [make_price(h, spots.get(h, 0.10), YESTERDAY) for h in range(24)]


def _trading_history() -> DerivedPowerHistory:
    """Return yesterday's samples: grid charge in the cheap hour, grid sale in the dear one."""
    return make_trading_history(YESTERDAY, bought_hour=_BOUGHT_HOUR, sold_hour=_SOLD_HOUR)


def _ctx(
    stored: RunningSellAverage | None, *, enabled: bool,
    history: DerivedPowerHistory | None = None,
) -> NodeContext:
    """A NodeContext holding exactly what ScheduleNode reads."""
    bc = default_battery_config()
    state = default_battery_state(0.9)
    series = build_price_series(
        _yesterday_prices() + _prices(_TWO_PEAKS), default_tariff_config(), now=NOW,
    )
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
        DerivedPowerHistory: history,
    }
    config = SimpleNamespace(local_tz=UTC, battery=bc, forecast_reserve_enabled=False)
    return NodeContext(primary=primary, secondary={}, config=config, now=NOW)


def _yesterday_sell(at_hour: int) -> float:
    """Return the sell price the default tariff gives yesterday's ``at_hour`` slot."""
    series = build_price_series(
        _yesterday_prices() + _prices(_TWO_PEAKS), default_tariff_config(), now=NOW,
    )
    return next(s.sell_eur_kwh for s in series.slots if s.start == YESTERDAY.replace(hour=at_hour))


async def test_node_seeds_a_fresh_start_with_the_highest_known_sell_price():
    schedule = await ScheduleNode()._compute(_ctx(None, enabled=False))
    series = build_price_series(
        _yesterday_prices() + _prices(_TWO_PEAKS), default_tariff_config(), now=NOW,
    )
    avg = schedule.gate.average
    assert avg is not None and avg.day == TODAY
    assert avg.value_eur_kwh == pytest.approx(max(s.sell_eur_kwh for s in series.slots))


async def test_node_prefers_yesterdays_realized_sale_over_the_market_high():
    schedule = await ScheduleNode()._compute(_ctx(None, enabled=False, history=_trading_history()))
    assert schedule.gate.average.value_eur_kwh == pytest.approx(_yesterday_sell(_SOLD_HOUR))
    assert schedule.gate.average.value_eur_kwh < 0.5     # the two-peak day sells at 0.575


async def test_node_returns_the_stored_object_when_the_day_has_not_changed():
    stored = RunningSellAverage(value_eur_kwh=0.30, day=TODAY)
    schedule = await ScheduleNode()._compute(_ctx(stored, enabled=True))
    assert schedule.gate.average is stored
    assert schedule.gate.active is True


async def test_node_folds_when_the_day_moves_on():
    stored = RunningSellAverage(value_eur_kwh=0.30, day=date(2024, 1, 14))
    schedule = await ScheduleNode()._compute(
        _ctx(stored, enabled=True, history=_trading_history()))
    assert schedule.gate.average is not stored
    assert schedule.gate.average.day == TODAY
    assert schedule.gate.average.value_eur_kwh == pytest.approx(
        0.9 * 0.30 + 0.1 * _yesterday_sell(_SOLD_HOUR))


async def test_node_moves_only_the_day_when_nothing_was_sold():
    stored = RunningSellAverage(value_eur_kwh=0.30, day=date(2024, 1, 14))
    schedule = await ScheduleNode()._compute(_ctx(stored, enabled=True))
    assert schedule.gate.average == RunningSellAverage(value_eur_kwh=0.30, day=TODAY)
