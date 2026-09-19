"""Tests for the price-level entities: the enum sensor and the cheap / expensive binary sensors."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.sun_sale.binary_sensor import CheapPriceBinarySensor, ExpensivePriceBinarySensor
from custom_components.sun_sale.contract.models import PriceLevelConfig, PriceSeries, PriceSlot
from custom_components.sun_sale.pipeline.price_level import classify_price_levels
from custom_components.sun_sale.sensor import PriceLevelSensor


def _coord(data: dict | None) -> MagicMock:
    """Return a coordinator mock carrying ``data``."""
    coord = MagicMock()
    coord.data = data
    return coord


def _entry() -> MagicMock:
    """Return a config-entry mock."""
    entry = MagicMock()
    entry.entry_id = "entry1"
    return entry


def _levels_cheap_now() -> dict:
    """Return coordinator data whose current hour is today's cheapest (UTC day)."""
    now = datetime.now(UTC)
    midnight = datetime.combine(now.date(), datetime.min.time(), UTC)
    step = timedelta(hours=1)
    slots = tuple(
        PriceSlot(start=midnight + step * h, end=midnight + step * (h + 1),
                  buy_eur_kwh=float(abs(h - now.hour) + 1), sell_eur_kwh=0.0, spot_eur_kwh=0.0,
                  sources=("test",))
        for h in range(24)
    )
    series = PriceSeries(slots=slots, resolution=step, computed_at=midnight)
    return {"price_level": classify_price_levels(series, PriceLevelConfig(), UTC, now)}


def test_sensor_publishes_the_current_level_with_its_details():
    sensor = PriceLevelSensor(_coord(_levels_cheap_now()), _entry())
    assert sensor.native_value == "cheap"
    attrs = sensor.extra_state_attributes
    assert attrs["buy_price"] == 1.0
    assert attrs["rank_pct"] == 0
    assert "level" not in attrs
    assert {"cheap_threshold", "expensive_threshold", "next_change", "next_level"} <= attrs.keys()


def test_binary_sensors_mirror_the_level():
    data = _levels_cheap_now()
    cheap = CheapPriceBinarySensor(_coord(data), _entry())
    expensive = ExpensivePriceBinarySensor(_coord(data), _entry())
    assert cheap.is_on is True
    assert "until" in cheap.extra_state_attributes
    assert expensive.is_on is False
    assert "next_start" in expensive.extra_state_attributes


def test_entities_are_unknown_without_prices():
    assert PriceLevelSensor(_coord(None), _entry()).native_value is None
    assert CheapPriceBinarySensor(_coord({}), _entry()).is_on is None
    assert ExpensivePriceBinarySensor(_coord(None), _entry()).extra_state_attributes == {}
