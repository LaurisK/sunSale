"""Tests for the precharge: running buy average, sun-only walk, trigger and DP tier.

Pure Python, no HA required. The unit tests pin ``fold_running_buy_average``,
``sun_only_peak_soc``, ``plan_precharge`` and the terminal valuation; the
scenario tests drive ``optimize_schedule`` over a two-day hourly horizon (today
2024-01-15, tomorrow 2024-01-16, UTC) on the default 10 kWh, 5 kW, 10-95 %
battery, where a slot buys for ``1.21 × spot + 0.0484`` and sells for
``spot - 0.025`` (the default flat tariff).
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
    Precharge,
    PriceSeries,
    PriceSlot,
    RunningBuyAverage,
    Schedule,
    SchedulePolicy,
    StorageMode,
    TerminalValuation,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline import precharge as pc
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.pipeline.dag_engine import NodeContext
from custom_components.sun_sale.pipeline.nodes.tier4 import ScheduleNode
from custom_components.sun_sale.pipeline.schedule import (
    _terminal_value,
    optimize_schedule,
    sun_only_peak_soc,
)
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
GRID_CHARGE = StorageMode.GridCharge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot(start: datetime, buy: float, *, priced: bool = True, forecast: bool = False) -> PriceSlot:
    """Build an hourly PriceSlot with the given buy price."""
    return PriceSlot(
        start=start, end=start + timedelta(hours=1), spot_eur_kwh=buy,
        buy_eur_kwh=buy, sell_eur_kwh=buy - 0.1, sources=(),
        priced=priced, forecast=forecast,
    )


def _series(slots: list[PriceSlot]) -> PriceSeries:
    """Wrap slots into a PriceSeries."""
    return PriceSeries(slots=tuple(slots), resolution=timedelta(hours=1), computed_at=NOW)


def _flat_load(kw: float) -> BaseLoadProfile:
    """A baseload profile drawing ``kw`` in every local hour."""
    return BaseLoadProfile(
        slots=tuple(
            BaseLoadSlot(hour=h, baseload_kw=kw, sample_count=0, is_fallback=True)
            for h in range(24)
        ),
        fallback_kw=kw, overall_p10_kw=kw, overall_median_kw=kw,
        confidence=None, sample_count=0, distinct_days=0, computed_at=NOW,
    )


def _evening_load(kw: float) -> BaseLoadProfile:
    """A baseload profile drawing ``kw`` only from 17:00 to 22:00 local."""
    return BaseLoadProfile(
        slots=tuple(
            BaseLoadSlot(
                hour=h, baseload_kw=kw if 17 <= h < 22 else 0.0, sample_count=0,
                is_fallback=True,
            )
            for h in range(24)
        ),
        fallback_kw=0.0, overall_p10_kw=0.0, overall_median_kw=0.0,
        confidence=None, sample_count=0, distinct_days=0, computed_at=NOW,
    )


def _sun(today: float = 0.0, tomorrow: float = 0.0, d2: float = 0.0, d3: float = 0.0) -> GenerationSeries:
    """Generation whose daily totals are given, spread over 08-16 local on both days."""
    slots = []
    for day, total in ((DAY1, today), (DAY2, tomorrow)):
        for h in range(8, 16):
            start = day.replace(hour=h)
            slots.append(GenerationSlot(
                start=start, end=start + timedelta(hours=1), expected_kwh=total / 8,
            ))
    return GenerationSeries(
        slots=tuple(slots), total_today_kwh=today, total_tomorrow_kwh=tomorrow,
        total_d2_kwh=d2, total_d3_kwh=d3,
    )


def _avg(value: float, day: date = TODAY) -> RunningBuyAverage:
    """A running buy average stamped ``day``."""
    return RunningBuyAverage(value_eur_kwh=value, day=day)


# ---------------------------------------------------------------------------
# fold_running_buy_average
# ---------------------------------------------------------------------------


def _day_series(buys: list[float], **kw) -> PriceSeries:
    """Series whose only slots are today's, one per hour from 00:00 UTC."""
    return _series([_slot(DAY1 + timedelta(hours=i), b, **kw) for i, b in enumerate(buys)])


def test_cold_start_seeds_with_todays_mean_buy_price():
    got = pc.fold_running_buy_average(None, _day_series([0.10, 0.20]), TODAY, UTC)
    assert got.value_eur_kwh == pytest.approx(0.15)
    assert got.day == TODAY


def test_new_day_keeps_ninety_percent_of_previous():
    stored = _avg(0.10, TODAY - timedelta(days=1))
    got = pc.fold_running_buy_average(stored, _day_series([0.20, 0.20]), TODAY, UTC)
    assert got.value_eur_kwh == pytest.approx(0.9 * 0.10 + 0.1 * 0.20)
    assert got.day == TODAY


def test_same_day_returns_the_stored_object_untouched():
    stored = _avg(0.10)
    assert pc.fold_running_buy_average(stored, _day_series([0.50]), TODAY, UTC) is stored


def test_placeholder_prices_never_move_the_average():
    series = _day_series([0.50, 0.50], priced=False)
    assert pc.fold_running_buy_average(None, series, TODAY, UTC) is None
    stored = _avg(0.10, TODAY - timedelta(days=1))
    assert pc.fold_running_buy_average(stored, series, TODAY, UTC) is stored


def test_the_buy_average_reads_the_buy_price_not_the_sell_price():
    # _slot sells 0.1 below its buy price, so a sell-side fold would give 0.05.
    got = pc.fold_running_buy_average(None, _day_series([0.15, 0.15]), TODAY, UTC)
    assert got.value_eur_kwh == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# sun_only_peak_soc
# ---------------------------------------------------------------------------


def _peak(gen: GenerationSeries, *, load_kw: float = 0.0, soc: float = 0.30, days: int = 3,
          now: datetime = NOW) -> float | None:
    """Run the sun-only walk on the default battery."""
    return sun_only_peak_soc(
        gen, _flat_load(load_kw), default_battery_config(), default_battery_state(soc),
        now, UTC, days,
    )


def test_no_forecast_total_gives_no_basis():
    assert _peak(_sun()) is None


def test_a_sunny_day_lifts_the_battery_to_its_ceiling():
    assert _peak(_sun(today=30.0)) == pytest.approx(0.95)


def test_a_dark_spell_never_gets_above_the_current_soc():
    # 2 kWh of sun a day against a 0.4 kW house: the battery only drains.
    assert _peak(_sun(today=2.0, tomorrow=2.0, d2=2.0), load_kw=0.4) == pytest.approx(0.30)


def test_a_small_surplus_lifts_it_by_the_stored_share_of_the_surplus():
    # 8 h of 0.5 kWh sun, no load: 4 kWh surplus x sqrt(0.9) over 10 kWh.
    got = _peak(_sun(today=4.0))
    assert got == pytest.approx(0.30 + 4.0 * 0.9 ** 0.5 / 10.0)


def test_the_walk_stops_at_the_first_day_without_a_total():
    # Today is dark, tomorrow is sunny, but d2 has no total: with 3 days asked
    # the walk still sees tomorrow, and never invents a sunny or dark d2.
    gen = _sun(today=2.0, tomorrow=30.0)
    assert _peak(gen, load_kw=0.4, days=3) == pytest.approx(0.95)
    # One day only never reaches tomorrow's sun.
    assert _peak(gen, load_kw=0.4, days=1) == pytest.approx(0.30)


def test_a_battery_already_at_the_target_counts_as_reached():
    assert _peak(_sun(today=2.0), load_kw=0.4, soc=0.90) == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# plan_precharge
# ---------------------------------------------------------------------------


def _decide(prices: PriceSeries, *, gen: GenerationSeries | None = None, avg: float | None = 0.15,
            soc: float = 0.30, target: float = 0.8, days: int = 3) -> Precharge:
    """Run ``plan_precharge`` on a dark default setup unless told otherwise."""
    return pc.plan_precharge(
        None if avg is None else _avg(avg), prices,
        gen or _sun(today=2.0, tomorrow=2.0), _flat_load(0.4),
        default_battery_config(), default_battery_state(soc), NOW, UTC, days, target,
    )


def _two_days(cheap_hours: set[int], *, cheap: float = 0.10, dear: float = 0.30, **kw) -> PriceSeries:
    """48 hourly slots, ``cheap`` on the given hours of both days, ``dear`` elsewhere."""
    return _series([
        _slot(DAY1 + timedelta(hours=i), cheap if i % 24 in cheap_hours else dear, **kw)
        for i in range(48)
    ])


def test_dark_sun_and_a_confirmed_cheap_slot_activates():
    got = _decide(_two_days({2, 3}))
    assert got.active
    assert got.target_soc == pytest.approx(0.8)
    assert got.peak_soc == pytest.approx(0.30)
    assert got.shortfall_kwh == pytest.approx(5.0)
    assert got.average == _avg(0.15)


def test_a_sun_that_reaches_the_target_stays_inactive():
    got = _decide(_two_days({2, 3}), gen=_sun(today=40.0))
    assert not got.active
    assert got.shortfall_kwh == 0.0
    assert got.peak_soc == pytest.approx(0.95)


def test_no_forecast_total_stays_inactive():
    got = _decide(_two_days({2, 3}), gen=_sun())
    assert not got.active and got.peak_soc is None


def test_no_slot_under_the_average_stays_inactive():
    got = _decide(_two_days(set()))
    assert not got.active
    assert got.shortfall_kwh == pytest.approx(5.0)


def test_a_cheap_estimate_is_not_a_trigger():
    # The only slots under the average are forecast-filled, not confirmed.
    series = _series(
        [_slot(DAY1 + timedelta(hours=i), 0.30) for i in range(24)]
        + [_slot(DAY2 + timedelta(hours=i), 0.10, priced=False, forecast=True) for i in range(24)]
    )
    assert not _decide(series).active


def test_a_cheap_slot_already_over_is_not_a_trigger():
    later = NOW + timedelta(hours=5)
    got = pc.plan_precharge(
        _avg(0.15), _two_days({2, 3}), _sun(today=2.0, tomorrow=2.0), _flat_load(0.4),
        default_battery_config(), default_battery_state(0.30), later, UTC, 3, 0.8,
    )
    # Hours 2-3 of today ended before 05:00; the cheap pair tomorrow remains.
    assert got.active
    got = pc.plan_precharge(
        _avg(0.15), _series([
            _slot(DAY1 + timedelta(hours=i), 0.10 if i in (2, 3) else 0.30) for i in range(24)
        ]), _sun(today=2.0), _flat_load(0.4),
        default_battery_config(), default_battery_state(0.30), later, UTC, 3, 0.8,
    )
    assert not got.active


def test_no_average_yet_stays_inactive_but_reports_the_walk():
    got = _decide(_two_days({2, 3}), avg=None)
    assert not got.active and got.average is None
    assert got.peak_soc == pytest.approx(0.30)


def test_a_fixed_price_never_triggers_itself():
    # Every slot buys at the average to within float noise: nothing is "below".
    series = _two_days(set(), dear=0.15)
    assert not _decide(series, avg=0.15 + 1e-12).active


def test_the_target_is_capped_at_the_batterys_ceiling():
    assert _decide(_two_days({2}), target=1.0).target_soc == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# _terminal_value tiers
# ---------------------------------------------------------------------------

# floor 0.10, 10 kWh: soc 0.60 holds 5 kWh above the floor.


def _term(**kw) -> float:
    """Value of 5 kWh above the floor under the given terminal valuation."""
    base = dict(reserve_kwh=0.0, reserve_eur_kwh=0.0, flat_eur_kwh=0.05)
    return _terminal_value(0.60, 0.10, 10.0, TerminalValuation(**{**base, **kw}))


def test_flat_only_is_the_flat_price_on_every_kwh():
    assert _term() == pytest.approx(0.25)


def test_load_reserve_alone_keeps_the_original_two_tier_value():
    assert _term(reserve_kwh=2.0, reserve_eur_kwh=0.30) == pytest.approx(2 * 0.30 + 3 * 0.05)


def test_precharge_alone_is_a_second_tier_of_its_own():
    assert _term(precharge_kwh=4.0, precharge_eur_kwh=0.20) == pytest.approx(4 * 0.20 + 1 * 0.05)


def test_where_the_reserves_overlap_the_higher_price_applies():
    # [0,2] max(0.30, 0.20); (2,4] precharge 0.20; (4,5] flat.
    got = _term(reserve_kwh=2.0, reserve_eur_kwh=0.30, precharge_kwh=4.0, precharge_eur_kwh=0.20)
    assert got == pytest.approx(2 * 0.30 + 2 * 0.20 + 1 * 0.05)
    # A dearer, larger precharge covers the whole of the smaller load reserve.
    got = _term(reserve_kwh=2.0, reserve_eur_kwh=0.10, precharge_kwh=4.0, precharge_eur_kwh=0.20)
    assert got == pytest.approx(4 * 0.20 + 1 * 0.05)


def test_the_value_never_decreases_and_never_convexifies_with_soc():
    term = TerminalValuation(
        reserve_kwh=2.0, reserve_eur_kwh=0.10, flat_eur_kwh=0.05,
        precharge_kwh=4.0, precharge_eur_kwh=0.20,
    )
    values = [_terminal_value(0.10 + i / 100, 0.10, 10.0, term) for i in range(0, 86)]
    steps = [b - a for a, b in zip(values, values[1:], strict=False)]
    assert all(s >= -1e-12 for s in steps)
    assert all(b <= a + 1e-12 for a, b in zip(steps, steps[1:], strict=False))


# ---------------------------------------------------------------------------
# optimize_schedule with a precharge
# ---------------------------------------------------------------------------

_CHEAP_NIGHTS = set(range(0, 6))


def _prices(cheap_hours=frozenset(_CHEAP_NIGHTS), *, cheap: float = 0.04, dear: float = 0.15):
    """48 hourly PriceEntry rows; ``cheap`` spot on the given hours of both days."""
    rows = [make_price(h, cheap if h in cheap_hours else dear, DAY1) for h in range(24)]
    rows += [make_price(h, cheap if h in cheap_hours else dear, DAY2) for h in range(24)]
    return rows


def plan(prices, *, precharge: Precharge | None, soc: float = 0.30,
         load: BaseLoadProfile | None = None, **kwargs) -> Schedule:
    """Run ``optimize_schedule`` over the two-day horizon with the given precharge."""
    bc = default_battery_config()
    state = default_battery_state(soc)
    deg = degradation_cost_per_kwh(bc, state)
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    calc = calculate(series, GenerationSeries(slots=()), state, NOW)
    return optimize_schedule(
        series, calc, bc, state, deg, NOW, local_tz=UTC, precharge=precharge,
        base_load_profile=load, **kwargs,
    )


def _active(avg: float = 0.15, target: float = 0.8) -> Precharge:
    """An active precharge decision, as ``plan_precharge`` would hand over."""
    return Precharge(
        average=_avg(avg), active=True, target_soc=target, peak_soc=0.30, shortfall_kwh=5.0,
    )


def _buy_by_start(prices) -> dict[datetime, float]:
    """Map slot start to buy price using the tariff the plan was built with."""
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    return {s.start: s.buy_eur_kwh for s in series.slots}


def test_without_a_precharge_the_plan_does_not_buy_into_a_dead_end():
    got = plan(_prices(), precharge=None)
    assert got.slots[-1].expected_soc_after < 0.5
    assert got.precharge is None


def test_an_active_precharge_buys_the_target_in_the_cheap_slots():
    got = plan(_prices(), precharge=_active())
    assert got.slots[-1].expected_soc_after == pytest.approx(0.8, abs=0.03)
    assert got.precharge is not None and got.precharge.active
    assert any(s.mode is GRID_CHARGE for s in got.slots)


def test_it_never_buys_in_a_slot_dearer_than_the_average():
    prices = _prices()
    buy = _buy_by_start(prices)
    got = plan(prices, precharge=_active(avg=0.15))
    bought = [s for s in got.slots if s.mode is GRID_CHARGE]
    assert bought
    assert all(buy[s.start] < 0.15 for s in bought)


def test_a_lower_average_buys_in_fewer_slots():
    prices = _prices()
    buy = _buy_by_start(prices)
    # Day-2 nights are no cheaper than day 1's; an average just under the cheap
    # buy price leaves nothing worth buying into.
    cheap_buy = min(buy.values())
    got = plan(prices, precharge=_active(avg=cheap_buy - 0.01))
    assert got.slots[-1].expected_soc_after < 0.5


def _plan_peak(schedule: Schedule) -> float:
    """Highest SoC any slot of the plan ends on."""
    return max(s.expected_soc_after for s in schedule.slots)


def test_leftover_cheap_slots_are_filled_towards_the_target():
    # An evening load of 2 kWh from the floor: the plain plan charges just over
    # 50 % at the first cheap slot and never again. With a 90 % target there are
    # cheap slots left over, so the plan goes on charging in them.
    prices = _prices()
    buy = _buy_by_start(prices)
    plain = plan(prices, precharge=None, soc=0.10, load=_evening_load(0.4))
    held = plan(prices, precharge=_active(target=0.9), soc=0.10, load=_evening_load(0.4))
    assert _plan_peak(plain) < 0.7
    assert _plan_peak(held) >= 0.9
    assert held.slots[-1].expected_soc_after > plain.slots[-1].expected_soc_after + 0.2
    assert all(buy[s.start] < 0.15 for s in held.slots if s.mode is GRID_CHARGE)
    assert held.precharge.active
    assert held.precharge.peak_soc == pytest.approx(_plan_peak(plain))
    assert held.precharge.shortfall_kwh == pytest.approx((0.9 - _plan_peak(plain)) * 10.0)


def test_a_plan_that_already_peaks_above_the_target_is_left_alone():
    # Cheap nights and a 0.4 kW house: the plain plan already fills to the
    # ceiling, so the sun-only trigger is armed but nothing is missing.
    prices = _prices()
    plain = plan(prices, precharge=None, load=_flat_load(0.4))
    held = plan(prices, precharge=_active(), load=_flat_load(0.4))
    assert _plan_peak(plain) >= 0.8
    assert [s.mode for s in held.slots] == [s.mode for s in plain.slots]
    assert not held.precharge.active
    assert held.precharge.shortfall_kwh == 0.0
    assert held.terminal.precharge_kwh == 0.0


def test_no_slot_left_under_the_average_changes_nothing():
    prices = _prices()
    cheap_buy = min(_buy_by_start(prices).values())
    plain = plan(prices, precharge=None, soc=0.10, load=_evening_load(0.4))
    held = plan(prices, precharge=_active(avg=cheap_buy - 0.05), soc=0.10, load=_evening_load(0.4))
    assert [s.mode for s in held.slots] == [s.mode for s in plain.slots]


def test_the_tier_is_reported_on_the_terminal_valuation():
    got = plan(_prices(), precharge=_active())
    # floor 0.10, target 0.8, 10 kWh, plain plan stays at 0.30 -> 7 kWh; priced at the
    # average plus the wear of cycling a kWh in (above the flat value).
    assert got.terminal.precharge_kwh == pytest.approx(7.0)
    assert got.terminal.precharge_eur_kwh == pytest.approx(
        max(0.15 + got.degradation_cost_per_kwh, got.terminal.flat_eur_kwh),
    )


def test_an_inactive_precharge_is_the_plain_dp_and_still_carries_the_average():
    prices = _prices()
    off = Precharge(average=_avg(0.15))
    got = plan(prices, precharge=off)
    base = plan(prices, precharge=None)
    assert [s.mode for s in got.slots] == [s.mode for s in base.slots]
    assert got.precharge == off
    assert got.terminal.precharge_kwh == 0.0


def test_without_grid_charging_the_precharge_cannot_activate():
    got = plan(_prices(), precharge=_active(), allow_grid_charging=False)
    assert got.precharge is not None and not got.precharge.active
    assert got.terminal.precharge_kwh == 0.0


def test_a_target_below_the_planning_floor_holds_nothing():
    got = plan(_prices(), precharge=_active(target=0.05))
    assert got.precharge is not None and not got.precharge.active


def test_the_precharge_stacks_with_the_load_reserve_without_losing_either():
    got = plan(_prices(), precharge=_active(), load_reserve_kwh=3.0)
    assert got.terminal.reserve_kwh == pytest.approx(3.0)
    assert got.terminal.precharge_kwh == pytest.approx(7.0)
    assert got.slots[-1].expected_soc_after == pytest.approx(0.8, abs=0.03)


# ---------------------------------------------------------------------------
# ScheduleNode: the buy average is folded every cycle and the decision is made
# ---------------------------------------------------------------------------


def _ctx(stored: RunningBuyAverage | None, *, enabled: bool, policy_kwargs=None,
         gen: GenerationSeries | None = None, soc: float = 0.10) -> NodeContext:
    """A NodeContext holding exactly what ScheduleNode reads: an evening-load house."""
    bc = default_battery_config()
    state = default_battery_state(soc)
    series = build_price_series(_prices(), default_tariff_config(), now=NOW)
    gen = gen or _sun(today=2.0, tomorrow=2.0)
    primary = {
        PriceSeries: series,
        CalculationResult: calculate(series, gen, state, NOW),
        BatteryState: state,
        DegradationCost: DegradationCost(value_kwh=degradation_cost_per_kwh(bc, state)),
        BaseLoadProfile: _evening_load(0.4),
        GenerationSeries: gen,
        SchedulePolicy: SchedulePolicy(
            precharge_enabled=enabled, precharge_days=3, precharge_target_soc=0.9,
            **(policy_kwargs or {}),
        ),
        RunningBuyAverage: stored,
    }
    config = SimpleNamespace(local_tz=UTC, battery=bc, forecast_reserve_enabled=False)
    return NodeContext(primary=primary, secondary={}, config=config, now=NOW)


async def test_node_seeds_the_buy_average_on_a_cold_start_even_when_off():
    schedule = await ScheduleNode()._compute(_ctx(None, enabled=False))
    assert schedule.precharge.average is not None
    assert schedule.precharge.average.day == TODAY
    assert not schedule.precharge.active


async def test_node_returns_the_same_stored_object_within_the_day():
    stored = _avg(0.12)
    schedule = await ScheduleNode()._compute(_ctx(stored, enabled=False))
    assert schedule.precharge.average is stored


async def test_node_activates_on_a_dark_horizon_with_cheap_confirmed_nights():
    held = await ScheduleNode()._compute(_ctx(_avg(0.15), enabled=True))
    plain = await ScheduleNode()._compute(_ctx(_avg(0.15), enabled=False))
    assert held.precharge.active
    assert _plan_peak(held) >= 0.9 > _plan_peak(plain)


async def test_node_stays_off_when_the_switch_is_off():
    schedule = await ScheduleNode()._compute(_ctx(_avg(0.15), enabled=False))
    assert not schedule.precharge.active
    assert _plan_peak(schedule) < 0.7


async def test_node_skips_the_decision_when_grid_charging_is_not_allowed():
    ctx = _ctx(_avg(0.15), enabled=True, policy_kwargs={"allow_grid_charging": False})
    schedule = await ScheduleNode()._compute(ctx)
    assert not schedule.precharge.active
    assert schedule.precharge.peak_soc is None


async def test_node_does_not_precharge_when_the_sun_will_do_it():
    ctx = _ctx(_avg(0.15), enabled=True, gen=_sun(today=40.0, tomorrow=40.0))
    schedule = await ScheduleNode()._compute(ctx)
    assert not schedule.precharge.active
