"""Tests for the realized trade prices the running buy / sell averages learn from.

Pure Python, no HA required. One local day (2024-01-15, UTC) of hourly price
slots whose buy price is the spot plus 0.10 and whose sell price is the spot, and
5-minute power samples in which the battery trades in chosen hours.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.contract.models import (
    DerivedPowerHistory,
    DerivedPowerSample,
    PriceSeries,
    PriceSlot,
)
from custom_components.sun_sale.inbound.observer.battery_trade import (
    MIN_SLOT_KWH,
    RealizedPrices,
    realized_trade_prices,
)
from tests.conftest import BASE_DT

DAY = date(2024, 1, 15)
START = BASE_DT


def _slot(hour: int, spot: float, *, priced: bool = True, day: datetime = START) -> PriceSlot:
    """Build an hourly slot of ``day`` selling at ``spot`` and buying 0.10 above it."""
    start = day + timedelta(hours=hour)
    return PriceSlot(
        start=start, end=start + timedelta(hours=1), spot_eur_kwh=spot,
        buy_eur_kwh=spot + 0.10, sell_eur_kwh=spot, sources=(), priced=priced,
    )


def _series(slots: list[PriceSlot]) -> PriceSeries:
    """Wrap slots into a PriceSeries."""
    return PriceSeries(slots=tuple(slots), resolution=timedelta(hours=1), computed_at=START)


def _day(spots: dict[int, float] | None = None, **kw) -> PriceSeries:
    """A full day of hourly slots at 0.10, with ``spots`` overriding single hours."""
    spots = spots or {}
    return _series([_slot(h, spots.get(h, 0.10), **kw) for h in range(24)])


def _history(power: dict[int, tuple[float, float]], day: datetime = START) -> DerivedPowerHistory:
    """5-minute samples of ``day``; ``power[hour] = (battery_kw, grid_net_kw)``, else idle."""
    samples = []
    for i in range(24 * 12):
        ts = day + timedelta(minutes=5 * i)
        battery, grid = power.get(ts.hour, (0.0, 0.0))
        samples.append(DerivedPowerSample(
            timestamp=ts, ac_port_kw_signed=0.0, backup_kw=0.0,
            grid_net_kw_signed=grid, solar_kw=0.0, battery_kw_signed=battery,
        ))
    return DerivedPowerHistory(samples=tuple(samples))


def test_prices_are_the_formula_prices_of_the_traded_slots():
    series = _day({3: 0.05, 18: 0.30})
    got = realized_trade_prices(
        _history({3: (4.0, 4.0), 18: (-4.0, -4.0)}), series, DAY, UTC)
    assert got.buy == pytest.approx(0.15)       # the slot's buy price (spot 0.05 + 0.10), not the spot
    assert got.sell == pytest.approx(0.30)      # the slot's sell price


def test_the_mean_is_weighted_by_the_energy_of_each_slot():
    series = _day({2: 0.04, 3: 0.10})
    # 2 kW in the cheap hour, 6 kW in the dearer one: three quarters of the energy.
    got = realized_trade_prices(
        _history({2: (2.0, 2.0), 3: (6.0, 6.0)}), series, DAY, UTC)
    assert got.buy == pytest.approx((2 * 0.14 + 6 * 0.20) / 8)


def test_charging_from_the_sun_is_not_a_purchase():
    # Battery charges while the house exports: nothing came from the grid.
    got = realized_trade_prices(_history({12: (5.0, -3.0)}), _day(), DAY, UTC)
    assert got == RealizedPrices(None, None)


def test_covering_the_house_is_not_a_sale():
    # Battery discharges but the grid still imports: nothing reached the grid.
    got = realized_trade_prices(_history({19: (-3.0, 1.0)}), _day(), DAY, UTC)
    assert got == RealizedPrices(None, None)


def test_the_grid_leg_caps_what_counts_as_bought():
    # 5 kW into the battery but only 1 kW imported: 1 kW is the purchase.
    series = _day({1: 0.0, 2: 0.20})
    got = realized_trade_prices(
        _history({1: (5.0, 1.0), 2: (1.0, 1.0)}), series, DAY, UTC)
    assert got.buy == pytest.approx(0.20)       # equal 1 kW each hour -> mean(0.10, 0.30)


def test_a_slot_under_the_noise_floor_does_not_count():
    tiny_kw = MIN_SLOT_KWH * 0.5 / 1.0          # a quarter of the floor over an hour
    got = realized_trade_prices(_history({4: (tiny_kw, tiny_kw)}), _day(), DAY, UTC)
    assert got.buy is None


def test_each_direction_is_independent():
    got = realized_trade_prices(_history({3: (4.0, 4.0)}), _day({3: 0.05}), DAY, UTC)
    assert got.buy == pytest.approx(0.15) and got.sell is None


def test_forecast_filled_slots_never_count():
    series = _day({3: 0.01}, priced=False)
    assert realized_trade_prices(_history({3: (4.0, 4.0)}), series, DAY, UTC) == RealizedPrices(None, None)


def test_only_the_asked_local_day_is_read():
    other = START + timedelta(days=1)
    series = _series([_slot(h, 0.10) for h in range(24)] + [_slot(3, 0.90, day=other)])
    samples = _history({3: (4.0, 4.0)}, other).samples
    assert realized_trade_prices(DerivedPowerHistory(samples=samples), series, DAY, UTC) == RealizedPrices(None, None)


def test_the_day_is_the_local_day():
    plus3 = timezone(timedelta(hours=3))
    # Local 2024-01-15 starts at 21:00 UTC the evening before: trading at 22:00 UTC
    # on the 14th belongs to local the 15th.
    eve = START - timedelta(hours=3)
    series = _series([_slot(h, 0.10, day=eve) for h in range(24)])
    got = realized_trade_prices(
        _history({1: (4.0, 4.0)}, eve), series, DAY, plus3)
    assert got.buy == pytest.approx(0.20)


def test_no_samples_or_no_slots_means_no_price():
    assert realized_trade_prices(DerivedPowerHistory(samples=()), _day(), DAY, UTC) == RealizedPrices(None, None)
    assert realized_trade_prices(_history({3: (4.0, 4.0)}), _series([]), DAY, UTC) == RealizedPrices(None, None)
