"""Tests for pricing.py — pure Python, no HA required."""
import dataclasses
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.sun_sale.contract.models import (
    NordpoolData,
    PriceEntry,
    TariffBand,
    TariffConfig,
    YesterdayPrices,
)
from custom_components.sun_sale.inbound.pricing import (
    _zero_fill_tomorrow,
    build_price_series,
    build_price_series_72h,
)
from tests.conftest import BASE_DT, default_tariff_config, make_price

NOW = BASE_DT


# ---------------------------------------------------------------------------
# Basic round-trip
# ---------------------------------------------------------------------------

def test_empty_prices_returns_empty_series():
    ps = build_price_series([], default_tariff_config(), now=NOW)
    assert ps.slots == ()


def test_slot_count_matches_input():
    prices = [make_price(h, 0.10) for h in range(24)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    assert len(ps.slots) == 24


def test_computed_at_is_set():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.computed_at == NOW


def test_sources_tuple():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.slots[0].sources == ("nordpool", "tariff")


# ---------------------------------------------------------------------------
# Tariff math round-trips
# ---------------------------------------------------------------------------

def test_buy_price_formula():
    tc = TariffConfig(distribution_fee=0.03, tax_rate=0.21, markup=0.01,
                      sell_distribution_fee=0.0, sell_tax_rate=0.0, sell_markup=0.0)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    slot = ps.slots[0]
    expected_buy = (0.10 + 0.03 + 0.01) * (1.0 + 0.21)
    assert abs(slot.buy_eur_kwh - expected_buy) < 1e-9


def test_sell_price_formula():
    tc = TariffConfig(distribution_fee=0.0, tax_rate=0.0, markup=0.0,
                      sell_distribution_fee=0.02, sell_tax_rate=0.05, sell_markup=0.005)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    slot = ps.slots[0]
    expected_sell = (0.10 - 0.02 - 0.005) * (1.0 - 0.05)
    assert abs(slot.sell_eur_kwh - expected_sell) < 1e-9


def test_spot_price_preserved():
    ps = build_price_series([make_price(0, 0.12345)], default_tariff_config(), now=NOW)
    assert abs(ps.slots[0].spot_eur_kwh - 0.12345) < 1e-9


# ---------------------------------------------------------------------------
# Negative-spot → negative-sell
# ---------------------------------------------------------------------------

def test_negative_spot_produces_negative_sell():
    ps = build_price_series([make_price(0, -0.05)], default_tariff_config(), now=NOW)
    assert ps.slots[0].sell_eur_kwh < 0


def test_positive_spot_produces_positive_sell():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.slots[0].sell_eur_kwh > 0


def test_sell_price_can_be_exactly_zero():
    tc = TariffConfig(distribution_fee=0.0, tax_rate=0.0, markup=0.0,
                      sell_distribution_fee=0.10, sell_tax_rate=0.0, sell_markup=0.0)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    # sell = (0.10 - 0.10) * 1 = 0.0 exactly
    assert ps.slots[0].sell_eur_kwh == 0.0


# ---------------------------------------------------------------------------
# Resolution detection
# ---------------------------------------------------------------------------

def test_hourly_resolution_detected():
    prices = [make_price(h, 0.10) for h in range(4)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    assert ps.resolution == timedelta(hours=1)


def test_single_slot_defaults_to_hourly_resolution():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    assert ps.resolution == timedelta(hours=1)


# ---------------------------------------------------------------------------
# slot_at / window helpers
# ---------------------------------------------------------------------------

def test_slot_at_returns_correct_slot():
    prices = [make_price(h, float(h) * 0.01) for h in range(24)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    t = NOW.replace(hour=5, minute=30)
    slot = ps.slot_at(t)
    assert slot is not None
    assert slot.start.hour == 5


def test_slot_at_returns_none_outside_range():
    ps = build_price_series([make_price(0, 0.10)], default_tariff_config(), now=NOW)
    t = NOW.replace(hour=5)
    assert ps.slot_at(t) is None


def test_window_returns_overlapping_slots():
    prices = [make_price(h, 0.10) for h in range(24)]
    ps = build_price_series(prices, default_tariff_config(), now=NOW)
    t1 = NOW.replace(hour=2)
    t2 = NOW.replace(hour=5)
    window = ps.window(t1, t2)
    assert len(window) == 3
    assert window[0].start.hour == 2
    assert window[-1].start.hour == 4


# ---------------------------------------------------------------------------
# 72h yesterday→today→tomorrow assembly
# ---------------------------------------------------------------------------

def _entry(day_offset: int, hour: int, price: float):
    start = (BASE_DT + timedelta(days=day_offset)).replace(hour=hour)
    from custom_components.sun_sale.contract.models import PriceEntry
    return PriceEntry(start=start, end=start + timedelta(hours=1), price_eur_kwh=price)


def test_72h_combines_yesterday_today_tomorrow():
    yesterday = tuple(_entry(-1, h, 0.05) for h in range(24))
    today_tomorrow = [_entry(0, h, 0.10) for h in range(24)] + [_entry(1, h, 0.15) for h in range(24)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=yesterday), default_tariff_config(), now=NOW
    )
    assert len(ps.slots) == 72
    assert ps.slots[0].start == BASE_DT - timedelta(days=1)
    assert ps.slots[23].start.hour == 23
    assert ps.slots[24].start == BASE_DT
    assert ps.slots[-1].start == BASE_DT + timedelta(days=1, hours=23)


def test_72h_uses_nordpool_resolution_not_derived():
    # Sparse entries (a single today slot) — derivation would default to 1h.
    # Explicit 15-min resolution from NordpoolData must be preserved.
    today = [_entry(0, 0, 0.10)]
    nordpool = NordpoolData(entries=today, resolution=timedelta(minutes=15))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=()), default_tariff_config(), now=NOW
    )
    assert ps.resolution == timedelta(minutes=15)


def test_72h_empty_yesterday_returns_only_today_tomorrow():
    today_tomorrow = [_entry(0, h, 0.10) for h in range(24)] + [_entry(1, h, 0.15) for h in range(24)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=()), default_tariff_config(), now=NOW
    )
    assert len(ps.slots) == 48
    assert ps.slots[0].start == BASE_DT


# ---------------------------------------------------------------------------
# _zero_fill_tomorrow — gap regression tests
#
# Regression: the original implementation derived "tomorrow" from a UTC date.
# Nordpool reports in local time, so for any timezone east of UTC the early
# slots of the local-tomorrow day land on UTC-today's date — the fill then
# started at UTC midnight, leaving a gap. These tests pin the contiguous,
# resolution-agnostic behavior.
# ---------------------------------------------------------------------------

def _q(start_utc: datetime, value: float, resolution: timedelta) -> PriceEntry:
    return PriceEntry(start=start_utc, end=start_utc + resolution, price_eur_kwh=value)


def test_zero_fill_no_gap_when_tomorrow_partial_in_local_tz():
    # LT (UTC+3): raw_today covers UTC 21:00 16 May → UTC 21:00 17 May; the
    # first 4 raw_tomorrow entries (LT 00:00–01:00 18 May) land on UTC-today.
    res = timedelta(minutes=15)
    today_start_utc = datetime(2026, 5, 16, 21, 0, tzinfo=timezone.utc)
    today = [_q(today_start_utc + i * res, 0.10, res) for i in range(96)]
    tomorrow_partial = [_q(today_start_utc + (96 + i) * res, 0.15, res) for i in range(4)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=timezone.utc)

    out = _zero_fill_tomorrow(today + tomorrow_partial, res, now)

    for prev, cur in zip(out, out[1:]):
        assert cur.start - prev.start == res, f"gap before {cur.start}"
    assert out[0].start == today_start_utc
    assert out[-1].end == today_start_utc + timedelta(hours=48)
    assert all(e.price_eur_kwh == 0.10 for e in out[:96])
    assert all(e.price_eur_kwh == 0.15 for e in out[96:100])
    assert all(e.price_eur_kwh == 0.0 for e in out[100:])


def test_zero_fill_no_gap_when_raw_tomorrow_empty_in_local_tz():
    # Only raw_today published. Fill must start where raw_today ends, not at
    # UTC midnight (the old buggy anchor).
    res = timedelta(minutes=15)
    today_start_utc = datetime(2026, 5, 16, 21, 0, tzinfo=timezone.utc)
    today = [_q(today_start_utc + i * res, 0.10, res) for i in range(96)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=timezone.utc)

    out = _zero_fill_tomorrow(today, res, now)

    for prev, cur in zip(out, out[1:]):
        assert cur.start - prev.start == res
    assert len(out) == 192
    assert out[96].start == today_start_utc + timedelta(hours=24)


def test_zero_fill_30min_resolution_produces_correct_slot_count():
    # The old branch hard-coded 96/24 slots-per-day; a 30-min sensor would be
    # mis-sized. The new fill is resolution-driven.
    res = timedelta(minutes=30)
    start = datetime(2026, 5, 16, 21, 0, tzinfo=timezone.utc)
    real = [_q(start + i * res, 0.10, res) for i in range(48)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=timezone.utc)

    out = _zero_fill_tomorrow(real, res, now)

    assert len(out) == 96
    assert out[0].start == start
    assert out[-1].end == start + timedelta(hours=48)
    for prev, cur in zip(out, out[1:]):
        assert cur.start - prev.start == res


def test_zero_fill_passthrough_when_already_covers_48h():
    res = timedelta(hours=1)
    start = datetime(2026, 5, 16, 21, 0, tzinfo=timezone.utc)
    full = [_q(start + i * res, 0.10, res) for i in range(48)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=timezone.utc)

    out = _zero_fill_tomorrow(full, res, now)

    assert out == full


def test_zero_fill_empty_input_passthrough():
    res = timedelta(hours=1)
    now = datetime(2026, 5, 17, 14, 15, tzinfo=timezone.utc)
    assert _zero_fill_tomorrow([], res, now) == []


# ---------------------------------------------------------------------------
# Time-of-use bands applied through the series builder
# ---------------------------------------------------------------------------

def test_local_tz_selects_band_per_slot():
    # Europe/Riga is UTC+2 in January. A weekday band starting at local 08:00
    # has buy fee 0.10; everything else falls to the night band fee 0.02.
    tz = ZoneInfo("Europe/Riga")
    tc = dataclasses.replace(
        default_tariff_config(),
        weekday_bands=(
            TariffBand(start_minute=8 * 60, distribution_fee=0.10, sell_distribution_fee=0.0),
            TariffBand(start_minute=22 * 60, distribution_fee=0.02, sell_distribution_fee=0.0),
        ),
    )
    # BASE_DT is 2024-01-15 (Monday) 00:00 UTC == 02:00 local. Build 24 hourly slots.
    prices = [make_price(h, 0.10) for h in range(24)]
    ps = build_price_series(prices, tc, now=NOW, local_tz=tz)

    # UTC 06:00 == local 08:00 → day band (fee 0.10).
    day_slot = ps.slots[6]
    assert abs(day_slot.buy_eur_kwh - (0.10 + 0.10 + tc.markup) * (1 + tc.tax_rate)) < 1e-9
    # UTC 00:00 == local 02:00 → night band (fee 0.02).
    night_slot = ps.slots[0]
    assert abs(night_slot.buy_eur_kwh - (0.10 + 0.02 + tc.markup) * (1 + tc.tax_rate)) < 1e-9


def test_no_local_tz_uses_flat_fee_even_with_bands():
    tc = dataclasses.replace(
        default_tariff_config(),
        weekday_bands=(TariffBand(start_minute=0, distribution_fee=0.99, sell_distribution_fee=0.0),),
    )
    ps = build_price_series([make_price(6, 0.10)], tc, now=NOW)  # no local_tz
    expected = (0.10 + tc.distribution_fee + tc.markup) * (1 + tc.tax_rate)
    assert abs(ps.slots[0].buy_eur_kwh - expected) < 1e-9


def test_72h_applies_tariff_to_all_segments():
    tc = TariffConfig(distribution_fee=0.03, tax_rate=0.21, markup=0.01,
                      sell_distribution_fee=0.0, sell_tax_rate=0.0, sell_markup=0.0)
    yesterday = (_entry(-1, 0, 0.10),)
    today_tomorrow = [_entry(0, 0, 0.10)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(nordpool, YesterdayPrices(entries=yesterday), tc, now=NOW)
    expected_buy = (0.10 + 0.03 + 0.01) * 1.21
    assert abs(ps.slots[0].buy_eur_kwh - expected_buy) < 1e-9
    assert abs(ps.slots[1].buy_eur_kwh - expected_buy) < 1e-9


def test_source_name_threads_into_provenance_tag():
    ps = build_price_series(
        [make_price(0, 0.10)], default_tariff_config(), now=NOW, source="entsoe"
    )
    assert ps.slots[0].sources == ("entsoe", "tariff")


def test_export_price_threads_into_slot():
    entry = PriceEntry(
        start=BASE_DT, end=BASE_DT + timedelta(hours=1),
        price_eur_kwh=0.10, export_price_eur_kwh=0.07,
    )
    ps = build_price_series([entry], default_tariff_config(), now=NOW)
    assert ps.slots[0].export_eur_kwh == 0.07


# ---------------------------------------------------------------------------
# Price-feed translators (synthetic HA states built to documented schemas)
# ---------------------------------------------------------------------------

class _FakeHass:
    """Minimal hass exposing only states.get."""

    def __init__(self, states: dict):
        self._states = states
        self.states = self

    def get(self, entity_id):
        return self._states.get(entity_id)


class _FakeState:
    """Minimal HA state stub carrying only attributes."""

    def __init__(self, attributes: dict):
        self.attributes = attributes


def test_entsoe_translator_parses_today_tomorrow():
    from custom_components.sun_sale.inbound.pricing import EntsoeTranslator

    state = _FakeState({
        "prices_today": [
            {"time": "2024-01-15T00:00:00+00:00", "price": 0.10},
            {"time": "2024-01-15T01:00:00+00:00", "price": 0.12},
        ],
        "prices_tomorrow": [
            {"time": "2024-01-16T00:00:00+00:00", "price": 0.20},
        ],
    })
    feed = EntsoeTranslator("sensor.entsoe").parse(_FakeHass({"sensor.entsoe": state}), now=NOW)
    by_start = {e.start: e.price_eur_kwh for e in feed.entries}
    assert by_start[BASE_DT] == 0.10
    assert by_start[BASE_DT.replace(hour=1)] == 0.12
    assert by_start[BASE_DT + timedelta(days=1)] == 0.20
    assert feed.resolution == timedelta(hours=1)


def test_entsoe_translator_missing_entity_returns_empty():
    from custom_components.sun_sale.inbound.pricing import EntsoeTranslator

    feed = EntsoeTranslator("sensor.absent").parse(_FakeHass({}), now=NOW)
    assert feed.entries == []


def test_octopus_translator_parses_rates_and_export():
    from custom_components.sun_sale.inbound.pricing import OctopusAgileTranslator

    imp = _FakeState({"all_rates": [
        {"start": "2024-01-15T00:00:00+00:00", "end": "2024-01-15T00:30:00+00:00", "value_inc_vat": 0.21},
        {"start": "2024-01-15T00:30:00+00:00", "end": "2024-01-15T01:00:00+00:00", "value_inc_vat": 0.25},
    ]})
    exp = _FakeState({"all_rates": [
        {"start": "2024-01-15T00:00:00+00:00", "end": "2024-01-15T00:30:00+00:00", "value_inc_vat": 0.05},
    ]})
    hass = _FakeHass({"sensor.octo_import": imp, "sensor.octo_export": exp})
    feed = OctopusAgileTranslator("sensor.octo_import", "sensor.octo_export").parse(hass, now=NOW)
    first = feed.entries[0]
    assert first.price_eur_kwh == 0.21
    assert first.export_price_eur_kwh == 0.05
    assert feed.resolution == timedelta(minutes=30)


def test_amber_translator_scales_cents_to_unit():
    from custom_components.sun_sale.inbound.pricing import AmberTranslator

    state = _FakeState({"forecasts": [
        {"start_time": "2024-01-15T00:00:00+00:00", "end_time": "2024-01-15T00:30:00+00:00", "per_kwh": 30.0},
    ]})
    feed = AmberTranslator("sensor.amber").parse(_FakeHass({"sensor.amber": state}), now=NOW)
    # 30 cents/kWh → 0.30 unit/kWh
    assert abs(feed.entries[0].price_eur_kwh - 0.30) < 1e-9


def test_tou_translator_emits_synthetic_schedule():
    from custom_components.sun_sale.inbound.pricing import TouScheduleTranslator

    # Two bands: cheap overnight (00:00) and peak (08:00), UTC for simplicity.
    bands = ((0, 0.10), (8 * 60, 0.40))
    translator = TouScheduleTranslator(bands, resolution=timedelta(hours=1), local_tz=timezone.utc)
    feed = translator.parse(None, now=NOW)
    assert len(feed.entries) == 48  # today + tomorrow at 1h
    by_start = {e.start: e.price_eur_kwh for e in feed.entries}
    assert by_start[BASE_DT.replace(hour=2)] == 0.10   # pre-peak → cheap band
    assert by_start[BASE_DT.replace(hour=9)] == 0.40   # post-08:00 → peak band


def test_tou_translator_empty_bands_returns_empty():
    from custom_components.sun_sale.inbound.pricing import TouScheduleTranslator

    feed = TouScheduleTranslator((), local_tz=timezone.utc).parse(None, now=NOW)
    assert feed.entries == []


def test_build_price_translator_dispatch():
    from custom_components.sun_sale.inbound import pricing as p

    assert isinstance(p.build_price_translator("nordpool", entity_id="x"), p.NordpoolTranslator)
    assert isinstance(p.build_price_translator("entsoe", entity_id="x"), p.EntsoeTranslator)
    assert isinstance(p.build_price_translator("octopus", entity_id="x"), p.OctopusAgileTranslator)
    assert isinstance(p.build_price_translator("amber", entity_id="x"), p.AmberTranslator)
    assert isinstance(p.build_price_translator("tou", entity_id="x"), p.TouScheduleTranslator)
    # Unknown source falls back to Nordpool.
    assert isinstance(p.build_price_translator("???", entity_id="x"), p.NordpoolTranslator)


def test_tou_bands_from_config_parses_and_sorts():
    from custom_components.sun_sale.inbound.pricing import tou_bands_from_config

    bands = tou_bands_from_config([
        {"start": "08:00", "price": 0.40},
        {"start": "00:00", "price": 0.10},
        {"start": "bad", "price": 0.99},   # dropped
    ])
    assert bands == ((0, 0.10), (480, 0.40))
