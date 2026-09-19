"""Tests for the panel's per-day price estimates (dashboard ``forecast_daily_price``)."""
from __future__ import annotations

from datetime import date, timedelta

from custom_components.sun_sale.contract.models import (
    PRICE_STAT_SOURCE_ACTUAL,
    PRICE_STAT_SOURCE_CLIMATOLOGY,
    PRICE_STAT_SOURCE_MODEL,
    DayClass,
    PriceDayStats,
    PriceForecast,
)
from custom_components.sun_sale.sensor import _serialize_price_estimates

TODAY = date(2026, 9, 15)


def _stat(horizon: int, source: str, with_4h: bool = True) -> PriceDayStats:
    """Build one day's statistics with distinguishable band values."""
    return PriceDayStats(
        day=TODAY + timedelta(days=horizon),
        day_class=DayClass.WEEKDAY,
        horizon_days=horizon,
        source=source,
        peak_1h_eur_kwh=0.2 + horizon / 100,
        peak_4h_eur_kwh=(0.15 + horizon / 100) if with_4h else None,
        trough_1h_eur_kwh=0.01,
        trough_4h_eur_kwh=0.03 if with_4h else None,
        negative_hours=0.0,
        negative_generation_kwh=0.0,
        confidence=0.9,
    )


def test_none_forecast_serializes_empty():
    assert _serialize_price_estimates(None) == {}


def test_tomorrow_estimate_shown_until_actual_prices_arrive():
    before = PriceForecast(days=(
        _stat(0, PRICE_STAT_SOURCE_ACTUAL),
        _stat(1, PRICE_STAT_SOURCE_CLIMATOLOGY),
        _stat(2, PRICE_STAT_SOURCE_MODEL),
    ))
    out = _serialize_price_estimates(before)
    assert set(out) == {"tomorrow", "d2"}
    assert out["tomorrow"] == {
        "trough_1h": 0.01, "peak_1h": 0.21, "trough_4h": 0.03, "peak_4h": 0.16,
        "source": "climatology", "confidence": 0.9,
    }

    after = PriceForecast(days=(
        _stat(0, PRICE_STAT_SOURCE_ACTUAL),
        _stat(1, PRICE_STAT_SOURCE_ACTUAL),
        _stat(2, PRICE_STAT_SOURCE_MODEL),
    ))
    assert set(_serialize_price_estimates(after)) == {"d2"}


def test_4h_band_without_history_serializes_as_none():
    forecast = PriceForecast(days=(_stat(1, PRICE_STAT_SOURCE_CLIMATOLOGY, with_4h=False),))
    tomorrow = _serialize_price_estimates(forecast)["tomorrow"]
    assert tomorrow["trough_4h"] is None and tomorrow["peak_4h"] is None
    assert tomorrow["peak_1h"] == 0.21


def test_today_never_included_even_when_modelled():
    forecast = PriceForecast(days=(_stat(0, PRICE_STAT_SOURCE_CLIMATOLOGY),))
    assert _serialize_price_estimates(forecast) == {}
