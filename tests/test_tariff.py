"""Tests for tariff.py — pure Python, no HA required."""
import dataclasses
from datetime import UTC, datetime

from custom_components.sun_sale.contract.models import TariffBand
from custom_components.sun_sale.pipeline.tariff import (
    bands_from_config,
    buy_price,
    select_band,
    sell_price,
)
from tests.conftest import default_tariff_config


def test_buy_price_basic():
    config = default_tariff_config()
    # (0.05 + 0.03 + 0.01) * 1.21 = 0.09 * 1.21 = 0.1089
    result = buy_price(0.05, config)
    assert abs(result - 0.1089) < 1e-9


def test_sell_price_basic():
    config = default_tariff_config()
    # (0.15 - 0.02 - 0.005) * (1 - 0.0) = 0.125
    result = sell_price(0.15, config)
    assert abs(result - 0.125) < 1e-9


def test_buy_price_zero_spot():
    config = default_tariff_config()
    # (0 + 0.03 + 0.01) * 1.21 = 0.04 * 1.21 = 0.0484
    result = buy_price(0.0, config)
    assert abs(result - 0.0484) < 1e-9


def test_buy_price_negative_spot():
    config = default_tariff_config()
    # Nordpool can go negative; formula should still work
    result = buy_price(-0.05, config)
    # (-0.05 + 0.03 + 0.01) * 1.21 = -0.01 * 1.21 = -0.0121
    assert abs(result - (-0.0121)) < 1e-9


def test_sell_price_negative_spot():
    config = default_tariff_config()
    result = sell_price(-0.10, config)
    # (-0.10 - 0.02 - 0.005) = -0.125
    assert result < 0


def test_buy_always_greater_than_sell_for_same_spot():
    config = default_tariff_config()
    for spot in [-0.05, 0.0, 0.05, 0.10, 0.20, 0.50]:
        assert buy_price(spot, config) > sell_price(spot, config), (
            f"buy should exceed sell at spot={spot}"
        )


# ---------------------------------------------------------------------------
# Time-of-use bands
# ---------------------------------------------------------------------------

# 2024-01-15 is a Monday; 2024-01-20 is a Saturday.
_MON = datetime(2024, 1, 15, tzinfo=UTC)
_SAT = datetime(2024, 1, 20, tzinfo=UTC)


def _tou_config():
    """Return a tariff config with a two-band weekday schedule and one weekend band."""
    base = default_tariff_config()
    return dataclasses.replace(
        base,
        weekday_bands=(
            TariffBand(start_minute=8 * 60, distribution_fee=0.06, sell_distribution_fee=0.04),   # day rate
            TariffBand(start_minute=23 * 60, distribution_fee=0.02, sell_distribution_fee=0.01),  # night rate
        ),
        weekend_bands=(
            TariffBand(start_minute=0, distribution_fee=0.015, sell_distribution_fee=0.005),
        ),
    )


def test_select_band_picks_active_window():
    config = _tou_config()
    band = select_band(config, _MON.replace(hour=10))
    assert band.distribution_fee == 0.06


def test_select_band_wraps_before_first_start():
    config = _tou_config()
    # 02:00 is before the 08:00 start → previous day's last band (the 23:00 night rate) carries over.
    band = select_band(config, _MON.replace(hour=2))
    assert band.distribution_fee == 0.02


def test_select_band_weekend_schedule():
    config = _tou_config()
    band = select_band(config, _SAT.replace(hour=12))
    assert band.distribution_fee == 0.015


def test_select_band_none_when_schedule_empty():
    config = default_tariff_config()  # no bands
    assert select_band(config, _MON.replace(hour=12)) is None


def test_buy_price_uses_band_fee():
    config = _tou_config()
    # 10:00 weekday → day band fee 0.06: (0.05 + 0.06 + 0.01) * 1.21
    expected = (0.05 + 0.06 + 0.01) * 1.21
    assert abs(buy_price(0.05, config, _MON.replace(hour=10)) - expected) < 1e-9


def test_sell_price_uses_band_fee_night():
    config = _tou_config()
    # 23:30 weekday → night band sell fee 0.01: (0.15 - 0.01 - 0.005) * (1 - 0)
    expected = (0.15 - 0.01 - 0.005) * 1.0
    assert abs(sell_price(0.15, config, _MON.replace(hour=23, minute=30)) - expected) < 1e-9


def test_buy_price_falls_back_to_flat_without_local_dt():
    config = _tou_config()
    # No local_dt → flat distribution fee 0.03, not any band fee.
    expected = (0.05 + 0.03 + 0.01) * 1.21
    assert abs(buy_price(0.05, config) - expected) < 1e-9


def test_bands_from_config_parses_and_sorts():
    bands = bands_from_config([
        {"start": "23:00", "buy_fee": 0.02, "sell_fee": 0.01},
        {"start": "08:00", "buy_fee": 0.06, "sell_fee": 0.04},
    ])
    assert [b.start_minute for b in bands] == [8 * 60, 23 * 60]


def test_bands_from_config_drops_invalid_rows():
    bands = bands_from_config([
        {"start": "", "buy_fee": 0.02, "sell_fee": 0.01},
        {"start": "bad", "buy_fee": 0.06, "sell_fee": 0.04},
        {"start": "06:15", "buy_fee": 0.05, "sell_fee": 0.03},
    ])
    assert len(bands) == 1
    assert bands[0].start_minute == 6 * 60 + 15


def test_bands_from_config_empty_or_none():
    assert bands_from_config(None) == ()
    assert bands_from_config([]) == ()


# ---------------------------------------------------------------------------
# Sell-price models (sell_mode)
# ---------------------------------------------------------------------------

WEEKDAY = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)  # Monday local-noon


def test_sell_mode_spot_is_default_formula():
    config = default_tariff_config()  # sell_mode defaults to "spot"
    assert abs(sell_price(0.15, config) - 0.125) < 1e-9


def test_sell_mode_fixed_returns_constant():
    config = dataclasses.replace(default_tariff_config(), sell_mode="fixed", fixed_sell_price=0.08)
    # Independent of spot.
    assert sell_price(0.15, config) == 0.08
    assert sell_price(-0.50, config) == 0.08


def test_sell_mode_schedule_uses_band_absolute_price():
    config = dataclasses.replace(
        default_tariff_config(),
        sell_mode="schedule",
        fixed_sell_price=0.01,
        weekday_bands=(
            TariffBand(start_minute=0, distribution_fee=0.0, sell_distribution_fee=0.05),
            TariffBand(start_minute=8 * 60, distribution_fee=0.0, sell_distribution_fee=0.30),
        ),
    )
    # 12:00 → the 08:00 band's sell value treated as the absolute price.
    assert sell_price(0.99, config, WEEKDAY) == 0.30
    # No local time → fixed fallback.
    assert sell_price(0.99, config, None) == 0.01


def test_sell_mode_schedule_empty_bands_falls_back_to_fixed():
    config = dataclasses.replace(
        default_tariff_config(), sell_mode="schedule", fixed_sell_price=0.02,
    )
    assert sell_price(0.99, config, WEEKDAY) == 0.02


def test_sell_mode_feed_uses_per_slot_export():
    config = dataclasses.replace(default_tariff_config(), sell_mode="feed", fixed_sell_price=0.01)
    assert sell_price(0.99, config, WEEKDAY, export=0.07) == 0.07
    # Missing export → fixed fallback.
    assert sell_price(0.99, config, WEEKDAY, export=None) == 0.01
