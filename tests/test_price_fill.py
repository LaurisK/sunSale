"""Tests for the forecast fill and its settlement — pure Python, no HA needed.

Two things must hold at once here, and they pull against each other: a slot the
market has not priced must become *usable* (the 2026-09-23 outage left the
inverter with an empty plan for a whole day), and it must stay *unbillable* (a
predicted price is not money). The tests below pin both sides of that line, and
the override that retires a fill and banks its error.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    PredictedDay,
    PriceCurveHistory,
    PriceErrorPoint,
    PriceSeries,
    PriceSlot,
)
from custom_components.sun_sale.pipeline import price_fill
from custom_components.sun_sale.pipeline import price_forecast as pf
from tests.conftest import flat_tariff

_TODAY = date(2026, 7, 28)
_TOMORROW = _TODAY + timedelta(days=1)
_RESOLUTION = timedelta(minutes=15)


def _series(priced_days: tuple[date, ...], days: tuple[date, ...]) -> PriceSeries:
    """Build a quarter-hourly series; days outside ``priced_days`` are placeholders."""
    slots: list[PriceSlot] = []
    for day in days:
        midnight = datetime.combine(day, datetime.min.time(), UTC)
        for i in range(96):
            start = midnight + _RESOLUTION * i
            priced = day in priced_days
            spot = 0.10 if priced else 0.0
            slots.append(PriceSlot(
                start=start, end=start + _RESOLUTION,
                buy_eur_kwh=spot + 0.05 if priced else 0.0,
                sell_eur_kwh=spot - 0.02 if priced else 0.0,
                spot_eur_kwh=spot,
                sources=("test", "tariff") if priced else ("test", "tariff", "unpriced"),
                priced=priced,
            ))
    return PriceSeries(
        slots=tuple(slots), resolution=_RESOLUTION,
        computed_at=datetime.combine(_TODAY, datetime.min.time(), UTC),
    )


def _curve(value: float) -> tuple[float, ...]:
    """Return a flat 24-hour predicted curve."""
    return tuple([value] * 24)


# ---------------------------------------------------------------------------
# Filling
# ---------------------------------------------------------------------------


def test_the_fill_replaces_the_placeholder_zero_with_a_tariffed_prediction():
    """An unpriced slot takes the predicted spot through the same tariff a real
    one does, so the two lines are directly comparable."""
    series = _series(priced_days=(_TODAY,), days=(_TODAY, _TOMORROW))
    tariff = flat_tariff()
    filled = price_fill.fill_from_forecast(
        series, {_TOMORROW: _curve(0.08)}, tariff, local_tz=UTC,
    )

    tomorrow = [s for s in filled.slots if s.start.date() == _TOMORROW]
    assert len(tomorrow) == 96
    assert all(s.forecast for s in tomorrow)
    assert all(not s.priced for s in tomorrow)
    assert all(s.spot_eur_kwh == pytest.approx(0.08) for s in tomorrow)
    # buy = (0.08 + markup 0.01 + fee 0.03) * 1.21
    assert tomorrow[0].buy_eur_kwh == pytest.approx((0.08 + 0.01 + 0.03) * 1.21)
    assert price_fill.SOURCE_FORECAST in tomorrow[0].sources


def test_the_fill_never_touches_a_real_price():
    """The market is right. A curve covering a priced day changes nothing."""
    series = _series(priced_days=(_TODAY,), days=(_TODAY,))
    filled = price_fill.fill_from_forecast(
        series, {_TODAY: _curve(0.99)}, flat_tariff(), local_tz=UTC,
    )
    assert filled is series
    assert all(s.spot_eur_kwh == pytest.approx(0.10) for s in filled.slots)


def test_a_day_with_no_prediction_keeps_its_bare_placeholders():
    """An honest zero nothing may read beats a guess nobody can attribute."""
    series = _series(priced_days=(_TODAY,), days=(_TODAY, _TOMORROW))
    filled = price_fill.fill_from_forecast(series, {}, flat_tariff(), local_tz=UTC)
    tomorrow = [s for s in filled.slots if s.start.date() == _TOMORROW]
    assert not any(s.forecast for s in tomorrow)
    assert not any(s.priced for s in tomorrow)


def test_a_short_curve_is_ignored():
    """A partial day is not a day; filling half of one would be worse than none."""
    series = _series(priced_days=(_TODAY,), days=(_TODAY, _TOMORROW))
    filled = price_fill.fill_from_forecast(
        series, {_TOMORROW: (0.08, 0.09)}, flat_tariff(), local_tz=UTC,
    )
    assert not any(s.forecast for s in filled.slots)


# ---------------------------------------------------------------------------
# Who may act on a filled slot
# ---------------------------------------------------------------------------


def test_a_filled_slot_is_plannable_but_never_priced():
    """The whole contract in one assertion pair: the planner sees it, the
    billing path does not."""
    series = _series(priced_days=(_TODAY,), days=(_TODAY, _TOMORROW))
    filled = price_fill.fill_from_forecast(
        series, {_TOMORROW: _curve(0.08)}, flat_tariff(), local_tz=UTC,
    )
    assert len(filled.priced_slots) == 96
    assert len(filled.plannable_slots) == 192
    assert all(s.priced for s in filled.priced_slots)


def test_the_planner_gets_slot_decisions_past_the_auction_edge():
    """The live symptom this fixes: through the 2026-09-23 feed outage the
    schedule was empty because calculation had nothing to hand it."""
    from custom_components.sun_sale.contract.models import GenerationSeries
    from custom_components.sun_sale.pipeline.calculation import calculate
    from tests.conftest import default_battery_state

    series = _series(priced_days=(), days=(_TODAY,))
    bare = calculate(
        series, GenerationSeries(slots=()), default_battery_state(),
        datetime.combine(_TODAY, datetime.min.time(), UTC),
    )
    assert bare.slots == ()

    filled = price_fill.fill_from_forecast(
        series, {_TODAY: _curve(0.08)}, flat_tariff(), local_tz=UTC,
    )
    planned = calculate(
        filled, GenerationSeries(slots=()), default_battery_state(),
        datetime.combine(_TODAY, datetime.min.time(), UTC),
    )
    assert len(planned.slots) == 96


# ---------------------------------------------------------------------------
# Override and banking
# ---------------------------------------------------------------------------


def test_the_error_is_banked_the_moment_the_real_prices_arrive():
    """Tomorrow's auction publishes in the afternoon; the error must not wait
    for the day to settle at midnight."""
    history = PriceCurveHistory(
        predictions=(PredictedDay(day=_TOMORROW, lead_days=1, curve=_curve(0.08)),),
    )
    priced = _series(priced_days=(_TODAY, _TOMORROW), days=(_TODAY, _TOMORROW))
    updated = price_fill.bank_fill_errors(history, priced, _TODAY, UTC, 14)

    assert updated is not None
    banked = [e for e in updated.errors if e.start.date() == _TOMORROW]
    assert len(banked) == 24
    assert all(e.forecast_eur_kwh == pytest.approx(0.08) for e in banked)
    assert all(e.actual_eur_kwh == pytest.approx(0.10) for e in banked)
    assert all(e.error_eur_kwh == pytest.approx(0.02) for e in banked)
    # The prediction stays — settle_days still folds it into the day's record.
    assert updated.predictions == history.predictions


def test_banking_is_idempotent():
    """Every cycle re-runs this; a day must be banked exactly once."""
    history = PriceCurveHistory(
        predictions=(PredictedDay(day=_TOMORROW, lead_days=1, curve=_curve(0.08)),),
    )
    priced = _series(priced_days=(_TODAY, _TOMORROW), days=(_TODAY, _TOMORROW))
    once = price_fill.bank_fill_errors(history, priced, _TODAY, UTC, 14)
    assert once is not None
    assert price_fill.bank_fill_errors(once, priced, _TODAY, UTC, 14) is None


def test_an_unpriced_day_banks_nothing():
    """Before the auction there is no 'actual' to measure the forecast against."""
    history = PriceCurveHistory(
        predictions=(PredictedDay(day=_TOMORROW, lead_days=1, curve=_curve(0.08)),),
    )
    series = _series(priced_days=(_TODAY,), days=(_TODAY, _TOMORROW))
    assert price_fill.bank_fill_errors(history, series, _TODAY, UTC, 14) is None


def test_banked_error_is_pruned_to_the_retention_window():
    """The store is written every cycle; this list must not grow without bound."""
    old = datetime(2026, 1, 1, 0, tzinfo=UTC)
    history = PriceCurveHistory(
        errors=(PriceErrorPoint(start=old, forecast_eur_kwh=0.1,
                                actual_eur_kwh=0.1, error_eur_kwh=0.0),),
    )
    series = _series(priced_days=(_TODAY,), days=(_TODAY,))
    updated = price_fill.bank_fill_errors(history, series, _TODAY, UTC, 14)
    assert updated is not None
    assert updated.errors == ()


# ---------------------------------------------------------------------------
# The feedback loop
# ---------------------------------------------------------------------------


def test_an_unproven_engine_keeps_the_plain_horizon_decay():
    """No banked error means no measurement, not a bad measurement."""
    assert pf.measured_skill(()) == pytest.approx(1.0)


def test_a_perfect_forecast_scores_one_and_a_useless_one_hits_the_floor():
    """Skill is the error as a share of the price level it was predicting."""
    perfect = tuple(
        PriceErrorPoint(start=datetime(2026, 7, 27, h, tzinfo=UTC),
                        forecast_eur_kwh=0.10, actual_eur_kwh=0.10, error_eur_kwh=0.0)
        for h in range(24)
    )
    assert pf.measured_skill(perfect) == pytest.approx(1.0)

    useless = tuple(
        PriceErrorPoint(start=datetime(2026, 7, 27, h, tzinfo=UTC),
                        forecast_eur_kwh=0.0, actual_eur_kwh=0.10, error_eur_kwh=0.10)
        for h in range(24)
    )
    assert pf.measured_skill(useless) == pytest.approx(0.4)


def test_measured_skill_discounts_a_modelled_days_confidence():
    """The banked error has to reach the published number, or it is decoration."""
    from tests.test_online_shape import _trained_history
    from tests.test_price_forecast import _generation, _price_series

    trained = _trained_history(days=50)
    day = _price_series(days=2).slots[0].start.date()
    bad = tuple(
        PriceErrorPoint(start=datetime.combine(day, datetime.min.time(), UTC)
                        + timedelta(hours=h),
                        forecast_eur_kwh=0.0, actual_eur_kwh=0.10, error_eur_kwh=0.10)
        for h in range(24)
    )

    def modelled_confidence(history: PriceCurveHistory) -> float:
        forecast = pf.compute_price_forecast(
            price_series=_price_series(days=2),
            history=history,
            weather=None,
            generation=_generation(),
            negative_threshold_eur_kwh=0.02,
            now=datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=10),
            local_tz=UTC,
            latitude=54.9, longitude=23.9,
        )
        modelled = [d for d in forecast.days if d.source == "model"]
        assert modelled
        return modelled[0].confidence

    assert modelled_confidence(replace(trained, errors=bad)) < modelled_confidence(trained)


def test_a_banked_day_is_published_before_it_settles():
    """The panel draws the error from the same place, so it appears in the
    afternoon rather than a day late."""
    day = _TODAY - timedelta(days=1)
    banked = tuple(
        PriceErrorPoint(start=datetime.combine(day, datetime.min.time(), UTC)
                        + timedelta(hours=h),
                        forecast_eur_kwh=0.08, actual_eur_kwh=0.10, error_eur_kwh=0.02)
        for h in range(24)
    )
    points = pf._error_slots((), UTC, _TODAY, banked=banked)
    assert len(points) == 24
    assert points[0].error_eur_kwh == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_banked_error_survives_the_store_round_trip():
    """A restart mid-afternoon must not lose the day's measured error — the
    prediction it came from is dropped once the day passes."""
    from custom_components.sun_sale.orchestration.store_codecs import (
        _deserialize_price_curve_history,
        _serialize_price_curve_history,
    )

    history = PriceCurveHistory(
        errors=(PriceErrorPoint(
            start=datetime(2026, 7, 27, 9, tzinfo=UTC),
            forecast_eur_kwh=0.08, actual_eur_kwh=0.10, error_eur_kwh=0.02,
        ),),
    )
    restored = _deserialize_price_curve_history(
        _serialize_price_curve_history(history)
    )
    assert len(restored.errors) == 1
    point = restored.errors[0]
    assert point.forecast_eur_kwh == pytest.approx(0.08)
    assert point.actual_eur_kwh == pytest.approx(0.10)
    assert point.error_eur_kwh == pytest.approx(0.02)


def test_todays_prediction_outlives_midnight():
    """During a feed outage it is the only thing that can fill today's slots."""
    history = PriceCurveHistory(
        predictions=(
            PredictedDay(day=_TODAY, lead_days=1, curve=_curve(0.08)),
            PredictedDay(day=_TODAY - timedelta(days=1), lead_days=1, curve=_curve(0.07)),
        ),
    )
    updated = pf.settle_days(
        history, _series(priced_days=(_TODAY,), days=(_TODAY,)), None, None,
        _TODAY, UTC, 0.02, 400,
    )
    assert updated is not None
    assert [p.day for p in updated.predictions] == [_TODAY]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_pricing_node_fills_from_the_stored_prediction():
    """The fill has to reach the series the rest of the DAG sees, or none of
    the contract above is live."""
    import asyncio

    from custom_components.sun_sale.contract.models import (
        PriceEntry,
        PriceFeedData,
        SunSaleConfig,
        YesterdayPrices,
    )
    from custom_components.sun_sale.pipeline.dag_engine import NodeContext
    from custom_components.sun_sale.pipeline.nodes.tier1 import PricingNode
    from tests.conftest import default_battery_config

    midnight = datetime.combine(_TODAY, datetime.min.time(), UTC)
    feed = PriceFeedData(
        entries=tuple(
            PriceEntry(start=midnight + timedelta(hours=h),
                       end=midnight + timedelta(hours=h + 1),
                       price_eur_kwh=0.10)
            for h in range(24)
        ),
        resolution=timedelta(hours=1),
    )
    ctx = NodeContext(
        primary={
            PriceFeedData: feed,
            YesterdayPrices: YesterdayPrices(entries=()),
            PriceCurveHistory: PriceCurveHistory(
                predictions=(
                    PredictedDay(day=_TOMORROW, lead_days=1, curve=_curve(0.08)),
                ),
            ),
        },
        secondary={},
        config=SunSaleConfig(tariff=flat_tariff(), battery=default_battery_config()),
        now=midnight + timedelta(hours=10),
    )
    series: PriceSeries = asyncio.run(PricingNode()._compute(ctx))

    tomorrow = [s for s in series.slots if s.start.date() == _TOMORROW]
    assert tomorrow and all(s.forecast for s in tomorrow)
    assert all(s.spot_eur_kwh == pytest.approx(0.08) for s in tomorrow)
    # Today came from the feed and is untouched.
    today = [s for s in series.slots if s.start.date() == _TODAY]
    assert all(s.priced and not s.forecast for s in today)


def test_the_current_price_sensor_says_when_its_number_is_a_forecast():
    """At 00:05 tomorrow is unpriced and during an outage today is too; an
    automation templating on this must be able to tell."""
    from custom_components.sun_sale.contract.models import PriceSeries as _Series
    from custom_components.sun_sale.sensor import CurrentBuyPriceSensor
    from tests.test_sensor import make_coord, make_entry

    now = datetime.now(UTC)
    step = timedelta(minutes=15)

    def series(forecast: bool) -> _Series:
        slot = PriceSlot(
            start=now - step, end=now + step,
            buy_eur_kwh=0.30, sell_eur_kwh=0.10, spot_eur_kwh=0.15,
            sources=("test", "tariff"), priced=not forecast, forecast=forecast,
        )
        return _Series(slots=(slot,), resolution=step, computed_at=now)

    real = CurrentBuyPriceSensor(make_coord({"pricing": series(False)}), make_entry())
    assert real.native_value == pytest.approx(0.30)
    assert real.extra_state_attributes["forecast"] is False

    filled = CurrentBuyPriceSensor(make_coord({"pricing": series(True)}), make_entry())
    assert filled.native_value == pytest.approx(0.30)
    assert filled.extra_state_attributes["forecast"] is True
