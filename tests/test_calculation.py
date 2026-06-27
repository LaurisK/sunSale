"""Tests for calculation.py — pure Python, no HA required."""
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.contract.models import (
    BatteryState, GenerationSeries, GenerationSlot,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_battery_state, default_tariff_config, make_price

NOW = BASE_DT


def _gen_series(hour_kwh: dict[int, float]) -> GenerationSeries:
    slots = tuple(
        GenerationSlot(
            start=NOW.replace(hour=h),
            end=NOW.replace(hour=h) + timedelta(hours=1),
            expected_kwh=kwh,
        )
        for h, kwh in sorted(hour_kwh.items())
    )
    return GenerationSeries(slots=slots)


def _empty_gen() -> GenerationSeries:
    return GenerationSeries(slots=())


def run(prices, gen=None, battery_state=None):
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    g = gen or _empty_gen()
    bs = battery_state or default_battery_state()
    return calculate(ps, g, bs, NOW)


# ---------------------------------------------------------------------------
# (a) all-positive prices → no lockouts
# ---------------------------------------------------------------------------

def test_all_positive_prices_no_lockouts():
    prices = [make_price(h, 0.10) for h in range(24)]
    result = run(prices)
    assert result.feed_in_lockout_windows == ()
    assert all(s.expected_solar_negative_sale_kwh == 0.0 for s in result.slots)
    assert result.total_negative_sale_kwh == 0.0


def test_all_positive_no_lockout_notes():
    prices = [make_price(h, 0.10) for h in range(24)]
    result = run(prices)
    for slot in result.slots:
        assert "battery_full_during_lockout" not in slot.notes


# ---------------------------------------------------------------------------
# (b) midday negative-sell window → lockout flagged + production reported
# ---------------------------------------------------------------------------

def _make_negative_sell_config():
    """High sell fees so even normal spot prices produce negative sell prices."""
    from custom_components.sun_sale.contract.models import TariffConfig
    return TariffConfig(
        distribution_fee=0.0, tax_rate=0.0, markup=0.0,
        sell_distribution_fee=0.20, sell_tax_rate=0.0, sell_markup=0.0,
    )


def test_negative_sell_window_flagged():
    # spot=0.05 → sell = 0.05 - 0.20 = -0.15 → locked out
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    prices = [make_price(h, 0.05 if 10 <= h < 14 else 0.30) for h in range(24)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _gen_series({h: 2.0 for h in range(10, 14)})
    result = calculate(ps, gen, default_battery_state(), NOW)

    assert len(result.feed_in_lockout_windows) == 1
    w = result.feed_in_lockout_windows[0]
    assert w[0].hour == 10
    assert w[1].hour == 14

    locked = [s for s in result.slots if s.expected_solar_negative_sale_kwh > 0]
    assert len(locked) == 4
    assert all(10 <= s.start.hour < 14 for s in locked)


def test_negative_sell_window_production_reported():
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    prices = [make_price(h, 0.05 if 10 <= h < 12 else 0.30) for h in range(24)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _gen_series({10: 1.5, 11: 2.5})
    result = calculate(ps, gen, default_battery_state(), NOW)

    neg_slots = [s for s in result.slots if s.expected_solar_negative_sale_kwh > 0]
    assert len(neg_slots) == 2
    assert abs(result.total_negative_sale_kwh - 4.0) < 1e-9


def test_lockout_windows_coalesced():
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    prices = [make_price(h, 0.05 if 10 <= h < 13 else 0.30) for h in range(24)]
    ps = build_price_series(prices, tc, now=NOW)
    result = calculate(ps, _empty_gen(), default_battery_state(), NOW)
    assert len(result.feed_in_lockout_windows) == 1
    w = result.feed_in_lockout_windows[0]
    assert w[0].hour == 10
    assert w[1].hour == 13


def test_non_contiguous_lockouts_separate_windows():
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    # Hours 10–11 and 20–21 locked out, 12–19 positive
    prices = [
        make_price(h, 0.05 if h in (10, 11, 20, 21) else 0.30)
        for h in range(24)
    ]
    ps = build_price_series(prices, tc, now=NOW)
    result = calculate(ps, _empty_gen(), default_battery_state(), NOW)
    assert len(result.feed_in_lockout_windows) == 2


# ---------------------------------------------------------------------------
# (c) lockout exceeds battery headroom → warning note
# ---------------------------------------------------------------------------

def test_battery_full_during_lockout_note():
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    prices = [make_price(10, 0.05), make_price(11, 0.30)]
    ps = build_price_series(prices, tc, now=NOW)
    # Battery almost full: SoC=0.95, capacity=10 kWh → headroom=0.5 kWh
    bs = BatteryState(soc=0.95, estimated_capacity_kwh=10.0)
    # Solar produces 2 kWh > 0.5 kWh headroom
    gen = _gen_series({10: 2.0})
    result = calculate(ps, gen, bs, NOW)
    slot_10 = next(s for s in result.slots if s.start.hour == 10)
    assert "battery_full_during_lockout" in slot_10.notes


def test_no_battery_full_note_when_headroom_sufficient():
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    prices = [make_price(10, 0.05), make_price(11, 0.30)]
    ps = build_price_series(prices, tc, now=NOW)
    bs = BatteryState(soc=0.50, estimated_capacity_kwh=10.0)  # 5 kWh headroom
    gen = _gen_series({10: 2.0})  # well within headroom
    result = calculate(ps, gen, bs, NOW)
    slot_10 = next(s for s in result.slots if s.start.hour == 10)
    assert "battery_full_during_lockout" not in slot_10.notes


# ---------------------------------------------------------------------------
# (d) negative buy price → paid_to_charge note
# ---------------------------------------------------------------------------

def test_paid_to_charge_note_on_negative_buy():
    from custom_components.sun_sale.contract.models import TariffConfig
    # Zero fees so buy_price ≈ spot price
    tc = TariffConfig(
        distribution_fee=0.0, tax_rate=0.0, markup=0.0,
        sell_distribution_fee=0.0, sell_tax_rate=0.0, sell_markup=0.0,
    )
    prices = [make_price(5, -0.05), make_price(6, 0.10)]
    ps = build_price_series(prices, tc, now=NOW)
    result = calculate(ps, _empty_gen(), default_battery_state(), NOW)
    slot_5 = next(s for s in result.slots if s.start.hour == 5)
    assert "paid_to_charge" in slot_5.notes


def test_no_paid_to_charge_on_positive_buy():
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices)
    for slot in result.slots:
        assert "paid_to_charge" not in slot.notes


# ---------------------------------------------------------------------------
# Feed-in lockout does not affect expected_solar_kwh
# ---------------------------------------------------------------------------

def test_expected_solar_kwh_always_reported():
    from custom_components.sun_sale.contract.models import TariffConfig
    tc = _make_negative_sell_config()
    prices = [make_price(10, 0.05)]  # locked out
    ps = build_price_series(prices, tc, now=NOW)
    gen = _gen_series({10: 3.0})
    result = calculate(ps, gen, default_battery_state(), NOW)
    slot_10 = next(s for s in result.slots if s.start.hour == 10)
    assert abs(slot_10.expected_solar_kwh - 3.0) < 1e-9
    assert abs(slot_10.expected_solar_negative_sale_kwh - 3.0) < 1e-9
