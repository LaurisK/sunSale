"""Tests for the feed-in floor: FeedIn only above what a stored kWh would fetch.

Pure Python, no HA required. Same two-day hourly horizon as the discharge-gate
tests (today 2024-01-15, tomorrow 2024-01-16, UTC; 10 kWh / 5 kW battery, 10–95 %
SoC; a slot sells for ``spot - 0.025``). With the default wear the floor is
``0.9 × average − 2 × wear``, i.e. about 0.367 for a running average of 0.50.
"""
from __future__ import annotations

from datetime import UTC, date, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    FeedInFloor,
    GenerationSeries,
    GenerationSlot,
    Precharge,
    RunningBuyAverage,
    RunningSellAverage,
    Schedule,
    StorageMode,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.pipeline.schedule import (
    _feed_in_floor_per_kwh,
    optimize_schedule,
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
DAY2 = BASE_DT + timedelta(days=1)
F = StorageMode.FeedIn
AVG = 0.50
EFF = 0.90


def _prices(day2: dict[int, float], base: float = 0.10):
    """Return 48 hourly PriceEntry rows: ``base`` everywhere, ``day2`` overrides tomorrow."""
    rows = [make_price(h, base, BASE_DT) for h in range(24)]
    rows += [make_price(h, day2.get(h, base), DAY2) for h in range(24)]
    return rows


def _gen(day2_kwh: dict[int, float]) -> GenerationSeries:
    """Return a generation series with energy only on tomorrow's listed hours."""
    return GenerationSeries(slots=tuple(
        GenerationSlot(
            start=DAY2.replace(hour=h), end=DAY2.replace(hour=h) + timedelta(hours=1),
            expected_kwh=kwh,
        )
        for h, kwh in sorted(day2_kwh.items())
    ))


def plan(prices, solar, *, enabled: bool = True, avg: float | None = AVG,
         soc: float = 0.30, **kwargs) -> Schedule:
    """Run ``optimize_schedule`` over the two-day horizon with the floor settings."""
    bc = default_battery_config()
    state = default_battery_state(soc)
    deg = degradation_cost_per_kwh(bc, state)
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    calc = calculate(series, _gen(solar), state, NOW)
    return optimize_schedule(
        series, calc, bc, state, deg, NOW, local_tz=UTC,
        running_average=None if avg is None else RunningSellAverage(avg, TODAY),
        feed_in_floor_enabled=enabled, **kwargs,
    )


def _floor() -> float:
    """Return the floor the default battery and ``AVG`` imply."""
    bc = default_battery_config()
    deg = degradation_cost_per_kwh(bc, default_battery_state())
    return _feed_in_floor_per_kwh(AVG, EFF, deg)


def _feed_in_sells(schedule: Schedule, prices) -> list[float]:
    """Return the sell price of every FeedIn slot in ``schedule``."""
    series = build_price_series(prices, default_tariff_config(), now=NOW)
    sell = {s.start: s.sell_eur_kwh for s in series.slots}
    return [sell[s.start] for s in schedule.slots if s.mode is F]


# Five sunny midday hours on a flat 0.075 sell — well under the 0.367 floor and
# under the battery cycle cost, so nothing is worth discharging — with no priced
# outlet for stored energy anywhere in the horizon: the plain DP exports the
# surplus (the live 2026-10-15 shape). 5 kWh fits the 6.5 kWh of headroom.
_SUN = {h: 1.0 for h in range(10, 15)}
_FLAT = _prices({})


def test_floor_formula_is_average_times_efficiency_less_both_legs_of_wear():
    assert _feed_in_floor_per_kwh(0.50, 0.9, 0.04) == pytest.approx(0.9 * 0.50 - 0.08)


def test_plain_dp_feeds_in_below_the_floor_so_the_floor_has_something_to_do():
    plain = plan(_FLAT, _SUN, enabled=False)
    sells = _feed_in_sells(plain, _FLAT)
    assert sells and all(s < _floor() for s in sells)


def test_floor_off_is_the_plain_dp_and_reports_nothing():
    plain = plan(_FLAT, _SUN, enabled=False, avg=None)
    off = plan(_FLAT, _SUN, enabled=False)
    assert [s.mode for s in off.slots] == [s.mode for s in plain.slots]
    assert off.feed_in_floor is None


def test_floor_stores_the_surplus_instead_of_feeding_it_in_cheaply():
    plain = plan(_FLAT, _SUN, enabled=False)
    held = plan(_FLAT, _SUN)
    assert not any(s.mode is F for s in held.slots)
    assert held.slots[-1].expected_soc_after > plain.slots[-1].expected_soc_after
    assert held.feed_in_floor == FeedInFloor(
        floor_eur_kwh=pytest.approx(_floor()), active=True, overfill=False,
    )


def test_a_slot_selling_above_the_floor_may_still_feed_in():
    prices = _prices({12: 0.80})
    held = plan(prices, _SUN, allow_discharge_to_grid=False)
    sells = _feed_in_sells(held, prices)
    assert held.feed_in_floor.active is True
    assert sells and all(s >= _floor() - 1e-9 for s in sells)


def test_a_plan_that_would_fill_the_battery_keeps_the_plain_plan():
    full = {h: 3.0 for h in range(10, 15)}          # 15 kWh into 6.5 kWh of headroom
    plain = plan(_FLAT, full, enabled=False)
    held = plan(_FLAT, full)
    assert [s.mode for s in held.slots] == [s.mode for s in plain.slots]
    assert held.feed_in_floor == FeedInFloor(
        floor_eur_kwh=pytest.approx(_floor()), active=False, overfill=True,
    )


def test_no_running_average_means_no_floor():
    plain = plan(_FLAT, _SUN, enabled=False, avg=None)
    held = plan(_FLAT, _SUN, avg=None)
    assert [s.mode for s in held.slots] == [s.mode for s in plain.slots]
    assert held.feed_in_floor == FeedInFloor(floor_eur_kwh=None, active=False)


def test_floor_is_not_applied_when_feed_in_is_not_allowed():
    held = plan(_FLAT, _SUN, allow_feed_in=False)
    assert held.feed_in_floor == FeedInFloor(floor_eur_kwh=None, active=False)


def test_a_battery_already_full_with_the_sun_to_come_is_left_to_the_plain_plan():
    held = plan(_FLAT, _SUN, soc=0.95)
    assert held.feed_in_floor.overfill is True
    assert held.feed_in_floor.active is False


def test_floor_survives_a_precharge_replan():
    """The mask is carried into the second DP the precharge runs."""
    pre = Precharge(
        average=RunningBuyAverage(1.0, TODAY), active=True, target_soc=0.9,
        peak_soc=0.3, shortfall_kwh=6.0,
    )
    held = plan(_FLAT, _SUN, precharge=pre)
    assert held.feed_in_floor.active is True
    assert all(s >= _floor() - 1e-9 for s in _feed_in_sells(held, _FLAT))

