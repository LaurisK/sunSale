"""Tests for the week-ahead price forecast — pure Python, no HA mocking needed.

The behaviour that matters most here is not accuracy but *safety*: the module
ships to installs in markets where its weather model is known to be worthless
(hydro- and nuclear-set price formation), so the tests below pin the guard that
keeps it from making those installs worse, alongside the arithmetic.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    PRICE_STAT_SOURCE_ACTUAL,
    PRICE_STAT_SOURCE_CLIMATOLOGY,
    PRICE_STAT_SOURCE_MODEL,
    BaseLoadProfile,
    BaseLoadSlot,
    DayClass,
    GenerationSeries,
    GenerationSlot,
    PriceCurveHistory,
    PriceDayRecord,
    PriceSeries,
    PriceSlot,
    WeatherDaily,
    WeatherForecastData,
)
from custom_components.sun_sale.pipeline import price_forecast as pf
from custom_components.sun_sale.pipeline.profitability import classify_day

_NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
_TODAY = _NOW.date()
_RESOLUTION = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _price_series(days: int = 2, peak: float = 0.25, base: float = 0.05,
                  cheap_hours: tuple[int, ...] = ()) -> PriceSeries:
    """Build a settled series with an evening peak and optional cheap window."""
    slots = []
    midnight = datetime.combine(_TODAY, datetime.min.time(), UTC)
    for i in range(days * 96):
        start = midnight + _RESOLUTION * i
        hour = start.hour
        spot = peak if 17 <= hour < 20 else base
        if hour in cheap_hours:
            spot = 0.001
        slots.append(PriceSlot(
            start=start, end=start + _RESOLUTION,
            buy_eur_kwh=spot + 0.1, sell_eur_kwh=spot - 0.025,
            spot_eur_kwh=spot, sources=("test",),
        ))
    return PriceSeries(slots=tuple(slots), resolution=_RESOLUTION, computed_at=_NOW)


def _generation(kwh_per_slot: float = 1.0, daylight: range = range(9, 17)) -> GenerationSeries:
    """Build a generation series with a flat daylight block."""
    slots = []
    midnight = datetime.combine(_TODAY, datetime.min.time(), UTC)
    for i in range(2 * 96):
        start = midnight + _RESOLUTION * i
        expected = kwh_per_slot if start.hour in daylight else 0.0
        slots.append(GenerationSlot(start=start, end=start + _RESOLUTION, expected_kwh=expected))
    return GenerationSeries(
        slots=tuple(slots), total_today_kwh=40.0, total_tomorrow_kwh=42.0,
        total_d2_kwh=35.0, total_d3_kwh=30.0, total_d4_kwh=44.0,
        total_d5_kwh=38.0, total_d6_kwh=41.0,
    )


def _history(n: int = 300, wind_effect: float = -0.004, seed: int = 7) -> PriceCurveHistory:
    """Build settled history in which wind genuinely depresses prices."""
    rng = random.Random(seed)
    records = []
    start = _TODAY - timedelta(days=n)
    for i in range(n):
        day = start + timedelta(days=i)
        wind = 10 + 20 * rng.random()
        cls = classify_day(day)
        base = 0.20 if cls is DayClass.WEEKDAY else 0.16
        peak3 = max(0.01, base + wind_effect * (wind - 20) + rng.gauss(0, 0.015))
        trough3 = max(0.0, 0.04 + wind_effect * 0.5 * (wind - 20) + rng.gauss(0, 0.006))
        neg = max(0.0, 2.0 - wind_effect * 60 * (wind - 20) + rng.gauss(0, 0.5))
        records.append(PriceDayRecord(
            day=day, day_class=cls,
            peak_1h_eur_kwh=peak3 * 1.1, peak_3h_eur_kwh=peak3,
            trough_1h_eur_kwh=trough3 * 0.8, trough_3h_eur_kwh=trough3,
            negative_hours=neg, mean_eur_kwh=(peak3 + trough3) / 2,
            wind_speed_kmh=wind, temperature_c=15.0, solar_kwh=30.0,
            negative_generation_kwh=30.0 * min(1.0, neg * 0.05),
        ))
    return PriceCurveHistory(records=tuple(records))


def _weather(wind: float = 20.0, days: int = 7) -> WeatherForecastData:
    """Build a flat daily weather forecast covering the horizon."""
    return WeatherForecastData(
        days=tuple(
            WeatherDaily(day=_TODAY + timedelta(days=n), wind_speed_kmh=wind, temperature_c=15.0)
            for n in range(days)
        ),
        source_entity="weather.test",
    )


# ---------------------------------------------------------------------------
# Day statistics
# ---------------------------------------------------------------------------


def test_export_break_even_is_the_tariff_wedge_not_zero():
    """Export stops paying at fee+markup — the whole point of the threshold."""
    assert pf.export_break_even(0.02, 0.005) == pytest.approx(0.025)


def test_day_price_stats_bands_and_ordering():
    """Peak/trough bands are means over the right slot counts, correctly ordered."""
    series = _price_series()
    day_slots = pf.slots_for_local_day(series.slots, _TODAY, UTC)
    stats = pf.day_price_stats(day_slots, 0.25, 0.025)

    assert stats["peak_1h_eur_kwh"] == pytest.approx(0.25)
    assert stats["peak_3h_eur_kwh"] == pytest.approx(0.25)   # exactly 3 h at peak
    assert stats["trough_3h_eur_kwh"] == pytest.approx(0.05)
    assert stats["negative_hours"] == 0.0


def test_day_price_stats_counts_negative_hours_against_the_wedge():
    """Hours below the wedge count even though every price is above zero."""
    series = _price_series(cheap_hours=(11, 12, 13))
    day_slots = pf.slots_for_local_day(series.slots, _TODAY, UTC)
    stats = pf.day_price_stats(day_slots, 0.25, 0.025)

    assert stats["negative_hours"] == pytest.approx(3.0)
    assert min(s.spot_eur_kwh for s in day_slots) > 0.0   # nothing is actually negative


def test_negative_generation_sums_only_loss_making_slots():
    """PV in cheap midday hours is counted; PV outside them is not."""
    series = _price_series(cheap_hours=(11, 12))
    gen = _generation(kwh_per_slot=1.0)
    day_slots = pf.slots_for_local_day(series.slots, _TODAY, UTC)

    # 2 cheap hours × 4 slots × 1.0 kWh, all inside the 09–17 daylight block.
    assert pf.negative_generation_for_day(day_slots, gen, 0.025) == pytest.approx(8.0)


def test_partial_day_is_not_recorded():
    """A day covered by only a few slots cannot define a daily statistic."""
    series = _price_series()
    partial = PriceSeries(slots=series.slots[:8], resolution=_RESOLUTION, computed_at=_NOW)
    assert pf.build_day_record(partial, None, None, _TODAY, UTC, 0.025) is None


# ---------------------------------------------------------------------------
# Forecast assembly
# ---------------------------------------------------------------------------


def test_settled_days_are_reported_as_actual():
    """Days the auction already covers are passed through, not modelled."""
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    by_day = forecast.by_day()
    assert by_day[_TODAY].source == PRICE_STAT_SOURCE_ACTUAL
    assert by_day[_TODAY].peak_3h_eur_kwh == pytest.approx(0.25)
    assert by_day[_TODAY].confidence == 1.0


def test_horizon_covers_a_full_week_with_decaying_confidence():
    """Seven days are published and confidence falls with horizon."""
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    assert len(forecast.days) == 7
    assert [d.horizon_days for d in forecast.days] == list(range(7))
    confidences = [d.confidence for d in forecast.days[2:]]
    assert confidences == sorted(confidences, reverse=True)


def test_band_ordering_holds_on_modelled_days():
    """trough_1h ≤ trough_3h ≤ peak_3h ≤ peak_1h, even when targets are fitted apart."""
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    for day in forecast.days:
        assert day.trough_1h_eur_kwh <= day.trough_3h_eur_kwh + 1e-9
        assert day.trough_3h_eur_kwh <= day.peak_3h_eur_kwh + 1e-9
        assert day.peak_3h_eur_kwh <= day.peak_1h_eur_kwh + 1e-9


def test_wind_moves_the_forecast_in_the_physical_direction():
    """More wind → lower peak and more loss-making hours."""
    args = (_price_series(), _history(), None, _generation(), 0.025)
    calm = pf.compute_price_forecast(*args[:2], _weather(wind=12.0), *args[3:],
                                     now=_NOW, local_tz=UTC)
    windy = pf.compute_price_forecast(*args[:2], _weather(wind=32.0), *args[3:],
                                      now=_NOW, local_tz=UTC)
    target = _TODAY + timedelta(days=3)
    assert windy.by_day()[target].peak_3h_eur_kwh < calm.by_day()[target].peak_3h_eur_kwh
    assert windy.by_day()[target].negative_hours > calm.by_day()[target].negative_hours


def test_weekend_is_cheaper_than_the_surrounding_weekdays():
    """The day-class baseline carries the weekday premium the history contains."""
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    modelled = [d for d in forecast.days if d.source != PRICE_STAT_SOURCE_ACTUAL]
    weekend = [d.peak_3h_eur_kwh for d in modelled if d.day_class is DayClass.WEEKEND]
    weekday = [d.peak_3h_eur_kwh for d in modelled if d.day_class is DayClass.WEEKDAY]
    assert weekend and weekday
    assert max(weekend) < max(weekday)


# ---------------------------------------------------------------------------
# The skill guard — the property that makes this safe to ship everywhere
# ---------------------------------------------------------------------------


def test_model_earns_weight_when_weather_genuinely_explains_price():
    """In a wind-driven market the model scores positive skill and is trusted."""
    pf._skill_cache.clear()
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    assert forecast.model_skill is not None and forecast.model_skill > 0
    assert forecast.model_weight > 0
    assert any(d.source == PRICE_STAT_SOURCE_MODEL for d in forecast.days)


@pytest.mark.parametrize("seed", range(12))
def test_model_is_ignored_when_weather_explains_nothing(seed):
    """A hydro/nuclear-style market — price uncorrelated with weather — gets zero weight.

    This is the guard from the multi-region study: in NO2 and FR the weather
    model measured *worse* than climatology, so it must not be allowed to
    contribute. Weight collapsing to zero is what makes shipping this to a
    region-agnostic user base defensible.

    Swept across seeds deliberately. A single seed would not prove anything:
    on histories with no signal at all, roughly 40 % still score a *positive*
    skill by chance (observed max ≈ +1.9 %). Testing one seed would pass on a
    build that trusts any positive number — the dead zone is what actually
    holds, and only a sweep demonstrates it.
    """
    pf._skill_cache.clear()
    history = _history(wind_effect=0.0, seed=seed)   # wind carries no signal
    forecast = pf.compute_price_forecast(
        _price_series(), history, _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    assert forecast.model_weight == 0.0
    assert all(d.source != PRICE_STAT_SOURCE_MODEL for d in forecast.days)


def test_no_history_publishes_only_settled_days():
    """A fresh install degrades to the auction window rather than guessing."""
    pf._skill_cache.clear()
    forecast = pf.compute_price_forecast(
        _price_series(), None, _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    assert {d.source for d in forecast.days} == {PRICE_STAT_SOURCE_ACTUAL}
    assert forecast.model_weight == 0.0


def test_no_weather_still_publishes_the_climatology_baseline():
    """Weather is optional; without it the baseline is published and labelled honestly."""
    pf._skill_cache.clear()
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), None, _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    assert len(forecast.days) == 7
    modelled = [d for d in forecast.days if d.source != PRICE_STAT_SOURCE_ACTUAL]
    assert modelled and all(d.source == PRICE_STAT_SOURCE_CLIMATOLOGY for d in modelled)


def test_ridge_solver_rejects_a_singular_system():
    """A rank-deficient design returns None rather than exploding."""
    rows = [[1.0, 2.0], [2.0, 4.0], [3.0, 6.0]]
    assert pf._solve_ridge(rows, [1.0, 2.0, 3.0], 0.0) is None


# ---------------------------------------------------------------------------
# History maintenance
# ---------------------------------------------------------------------------


def test_vintage_is_captured_once_and_never_revised():
    """Features freeze at the lead time so training matches inference."""
    history = PriceCurveHistory()
    first = pf.capture_feature_vintage(history, _weather(wind=20.0), _generation(), _TODAY)
    assert first is not None
    target = _TODAY + timedelta(days=pf.PRICE_FORECAST_VINTAGE_LEAD_DAYS)
    assert first.vintage_by_day()[target].wind_speed_kmh == 20.0

    # A later cycle with different weather must not overwrite the frozen row.
    again = pf.capture_feature_vintage(first, _weather(wind=99.0), _generation(), _TODAY)
    assert again is None


def test_settle_days_records_settled_days_and_uses_the_vintage():
    """A settled day is recorded with the features frozen for it, not today's."""
    history = PriceCurveHistory()
    captured = pf.capture_feature_vintage(
        history, _weather(wind=7.0), _generation(), _TODAY - timedelta(days=2),
    )
    assert captured is not None

    settled = pf.settle_days(
        captured, _price_series(), _generation(), _weather(wind=42.0),
        _TODAY, UTC, 0.025, retention_days=400,
    )
    assert settled is not None
    record = settled.by_day()[_TODAY]
    assert record.wind_speed_kmh == 7.0          # the frozen vintage, not 42.0
    assert record.peak_3h_eur_kwh == pytest.approx(0.25)
    assert settled.vintages == ()                # consumed once its day settled


def test_settle_days_is_idempotent():
    """Re-running the same cycle records nothing new."""
    settled = pf.settle_days(
        PriceCurveHistory(), _price_series(), _generation(), _weather(),
        _TODAY, UTC, 0.025, retention_days=400,
    )
    assert settled is not None
    assert pf.settle_days(
        settled, _price_series(), _generation(), _weather(),
        _TODAY, UTC, 0.025, retention_days=400,
    ) is None


def test_settle_days_prunes_beyond_retention():
    """Records older than the retention window are dropped."""
    old = PriceDayRecord(
        day=_TODAY - timedelta(days=500), day_class=DayClass.WEEKDAY,
        peak_1h_eur_kwh=0.2, peak_3h_eur_kwh=0.2,
        trough_1h_eur_kwh=0.0, trough_3h_eur_kwh=0.0,
        negative_hours=0.0, mean_eur_kwh=0.1,
    )
    settled = pf.settle_days(
        PriceCurveHistory(records=(old,)), _price_series(), _generation(), _weather(),
        _TODAY, UTC, 0.025, retention_days=400,
    )
    assert settled is not None
    assert old.day not in settled.by_day()


def test_daily_price_stats_labels_match_the_generation_day_labels():
    """The week-ahead price view keys align with the week-ahead generation view."""
    forecast = pf.compute_price_forecast(
        _price_series(), _history(), _weather(), _generation(), 0.025,
        now=_NOW, local_tz=UTC,
    )
    stats = forecast.daily_price_stats()
    assert set(stats) == {
        "price_today", "price_tomorrow", "price_d2", "price_d3",
        "price_d4", "price_d5", "price_d6",
    }
    assert stats["price_today"]["source"] == PRICE_STAT_SOURCE_ACTUAL


def test_negative_generation_never_exceeds_the_day_total():
    """The PV exposed to negative prices is bounded by that day's forecast."""
    forecast = pf.compute_price_forecast(
        _price_series(cheap_hours=tuple(range(9, 17))), _history(), _weather(),
        _generation(), 0.025, now=_NOW, local_tz=UTC,
    )
    totals = {0: 40.0, 1: 42.0, 2: 35.0, 3: 30.0, 4: 44.0, 5: 38.0, 6: 41.0}
    for day in forecast.days:
        assert day.negative_generation_kwh <= totals[day.horizon_days] + 1e-6
        assert day.negative_generation_kwh >= 0.0


# ---------------------------------------------------------------------------
# Absorbable vs surplus — the actionable half of the negative-price figure
# ---------------------------------------------------------------------------


def _base_load(kw: float = 0.5) -> BaseLoadProfile:
    """Build a flat 24-hour baseload profile."""
    return BaseLoadProfile(
        slots=tuple(BaseLoadSlot(hour=h, baseload_kw=kw, sample_count=10, is_fallback=False) for h in range(24)),
        fallback_kw=kw, overall_p10_kw=kw, overall_median_kw=kw,
        confidence=1.0, sample_count=240, distinct_days=10, computed_at=_NOW,
    )


def _forecast_with(capacity, base_load=None, cheap_hours=(9, 10, 11, 12, 13, 14, 15, 16)):
    """Run a forecast with a given battery size and baseload."""
    pf._skill_cache.clear()
    return pf.compute_price_forecast(
        _price_series(cheap_hours=cheap_hours), _history(), _weather(),
        _generation(kwh_per_slot=2.0), 0.025, now=_NOW, local_tz=UTC,
        battery_capacity_kwh=capacity, base_load=base_load,
    )


def test_split_is_unreported_without_a_battery_size():
    """Unknown capacity leaves the split absent rather than guessing a size."""
    today = _forecast_with(None).by_day()[_TODAY]
    assert today.negative_generation_kwh > 0
    assert today.absorbable_kwh is None
    assert today.surplus_kwh is None


def test_split_partitions_the_negative_generation():
    """absorbable + surplus always reconstructs the figure they describe."""
    forecast = _forecast_with(10.0, _base_load())
    for day in forecast.days:
        assert day.absorbable_kwh is not None and day.surplus_kwh is not None
        assert day.absorbable_kwh + day.surplus_kwh == pytest.approx(
            day.negative_generation_kwh
        )
        assert day.absorbable_kwh >= 0.0 and day.surplus_kwh >= 0.0


def test_small_battery_leaves_surplus_a_large_one_does_not():
    """The surplus is what no schedule can rescue — it shrinks as capacity grows.

    This is the signal the week-ahead view exists to raise: a day whose PV
    output under loss-making prices exceeds everything the site could possibly
    take says the battery must be empty before it, which is a reason to sell
    earlier even at a mediocre price.
    """
    # 8 cheap hours x 4 slots x 2.0 kWh = 64 kWh generated into negative prices.
    small = _forecast_with(5.0).by_day()[_TODAY]
    large = _forecast_with(100.0).by_day()[_TODAY]

    assert small.negative_generation_kwh == pytest.approx(64.0)
    assert small.absorbable_kwh == pytest.approx(5.0)
    assert small.surplus_kwh == pytest.approx(59.0)      # unavoidable spill

    assert large.surplus_kwh == pytest.approx(0.0)       # all of it absorbable


def test_household_load_counts_towards_headroom():
    """Draw through the loss-making hours absorbs PV the battery would not."""
    without = _forecast_with(5.0).by_day()[_TODAY]
    with_load = _forecast_with(5.0, _base_load(kw=2.0)).by_day()[_TODAY]

    # 8 negative hours x 2 kW = 16 kWh of extra headroom.
    assert with_load.absorbable_kwh == pytest.approx(without.absorbable_kwh + 16.0)
    assert with_load.surplus_kwh == pytest.approx(without.surplus_kwh - 16.0)


def test_surplus_is_zero_when_no_hours_go_negative():
    """No loss-making hours means nothing to spill."""
    day = _forecast_with(10.0, cheap_hours=()).by_day()[_TODAY]
    assert day.negative_generation_kwh == 0.0
    assert day.surplus_kwh == pytest.approx(0.0)


def test_split_is_published_on_modelled_days_too():
    """The split is what makes a *future* day actionable, so it must be there."""
    forecast = _forecast_with(10.0, _base_load())
    modelled = [d for d in forecast.days if d.source != PRICE_STAT_SOURCE_ACTUAL]
    assert modelled
    assert all(d.surplus_kwh is not None for d in modelled)
