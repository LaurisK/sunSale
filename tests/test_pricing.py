"""Tests for pricing.py — pure Python, no HA required."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import (
    DaySchedule,
    NordpoolData,
    PriceEntry,
    PriceFormula,
    TariffConfig,
    TariffSwitch,
    YesterdayPrices,
)
from custom_components.sun_sale.inbound.pricing import (
    _zero_fill_tomorrow,
    build_price_series,
    build_price_series_72h,
)
from tests.conftest import BASE_DT, default_tariff_config, flat_tariff, make_price

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
    tc = flat_tariff(buy_fee=0.03, vat=0.21, markup=0.01, sell_fee=0.0, sell_tax=0.0, sell_markup=0.0)
    ps = build_price_series([make_price(0, 0.10)], tc, now=NOW)
    slot = ps.slots[0]
    expected_buy = (0.10 + 0.03 + 0.01) * (1.0 + 0.21)
    assert abs(slot.buy_eur_kwh - expected_buy) < 1e-9


def test_sell_price_formula():
    tc = flat_tariff(buy_fee=0.0, vat=0.0, markup=0.0, sell_fee=0.02, sell_tax=0.05, sell_markup=0.005)
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
    tc = flat_tariff(buy_fee=0.0, vat=0.0, markup=0.0, sell_fee=0.10, sell_tax=0.0, sell_markup=0.0)
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


def test_72h_empty_yesterday_pads_the_grid_but_prices_only_today_tomorrow():
    """With nothing stored for yesterday the grid still spans 72h, unpriced.

    The observed and generation series are resampled onto this grid, so it has
    to keep its full span; the padding carries ``priced=False`` so no consumer
    mistakes the filler zero for a market price.
    """
    today_tomorrow = [_entry(0, h, 0.10) for h in range(24)] + [_entry(1, h, 0.15) for h in range(24)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        nordpool, YesterdayPrices(entries=()), default_tariff_config(), now=NOW
    )
    assert len(ps.slots) == 72
    assert len(ps.priced_slots) == 48
    assert ps.priced_slots[0].start == BASE_DT
    # Yesterday's 24 unknown slots are present, but flagged.
    assert ps.slots[0].start == BASE_DT - timedelta(days=1)
    assert [s.priced for s in ps.slots[:24]] == [False] * 24


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
    today_start_utc = datetime(2026, 5, 16, 21, 0, tzinfo=UTC)
    today = [_q(today_start_utc + i * res, 0.10, res) for i in range(96)]
    tomorrow_partial = [_q(today_start_utc + (96 + i) * res, 0.15, res) for i in range(4)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=UTC)

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
    today_start_utc = datetime(2026, 5, 16, 21, 0, tzinfo=UTC)
    today = [_q(today_start_utc + i * res, 0.10, res) for i in range(96)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=UTC)

    out = _zero_fill_tomorrow(today, res, now)

    for prev, cur in zip(out, out[1:]):
        assert cur.start - prev.start == res
    assert len(out) == 192
    assert out[96].start == today_start_utc + timedelta(hours=24)


def test_zero_fill_30min_resolution_produces_correct_slot_count():
    # The old branch hard-coded 96/24 slots-per-day; a 30-min sensor would be
    # mis-sized. The new fill is resolution-driven.
    res = timedelta(minutes=30)
    start = datetime(2026, 5, 16, 21, 0, tzinfo=UTC)
    real = [_q(start + i * res, 0.10, res) for i in range(48)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=UTC)

    out = _zero_fill_tomorrow(real, res, now)

    assert len(out) == 96
    assert out[0].start == start
    assert out[-1].end == start + timedelta(hours=48)
    for prev, cur in zip(out, out[1:]):
        assert cur.start - prev.start == res


def test_zero_fill_passthrough_when_already_covers_48h():
    res = timedelta(hours=1)
    start = datetime(2026, 5, 16, 21, 0, tzinfo=UTC)
    full = [_q(start + i * res, 0.10, res) for i in range(48)]
    now = datetime(2026, 5, 17, 14, 15, tzinfo=UTC)

    out = _zero_fill_tomorrow(full, res, now)

    assert out == full


def test_zero_fill_empty_input_passthrough():
    res = timedelta(hours=1)
    now = datetime(2026, 5, 17, 14, 15, tzinfo=UTC)
    assert _zero_fill_tomorrow([], res, now) == []


# ---------------------------------------------------------------------------
# Grid-fee tariffs applied through the series builder
# ---------------------------------------------------------------------------

def _day_night_tariff() -> TariffConfig:
    """Return a buy formula with T1 0.10 from local 08:00 and T2 0.02 from 22:00 on workdays."""
    buy = PriceFormula(
        markup=0.01, vat=0.21, grid_fees=(0.10, 0.02),
        schedules=(DaySchedule("all", "workday", (TariffSwitch(8 * 60, 1), TariffSwitch(22 * 60, 2))),),
    )
    return TariffConfig(buy=buy, sell=flat_tariff().sell)


def test_local_tz_selects_the_tariff_per_slot():
    # Europe/Riga is UTC+2 in January; BASE_DT is 2024-01-15 (Monday) 00:00 UTC == 02:00 local.
    tz = ZoneInfo("Europe/Riga")
    ps = build_price_series([make_price(h, 0.10) for h in range(24)], _day_night_tariff(), now=NOW, local_tz=tz)
    # UTC 06:00 == local 08:00 → T1 (0.10).
    assert abs(ps.slots[6].buy_eur_kwh - (0.10 + 0.10 + 0.01) * 1.21) < 1e-9
    # UTC 00:00 == local 02:00 → T2, carried over from 22:00 (0.02).
    assert abs(ps.slots[0].buy_eur_kwh - (0.10 + 0.02 + 0.01) * 1.21) < 1e-9


def test_no_local_tz_uses_the_first_tariff():
    ps = build_price_series([make_price(0, 0.10)], _day_night_tariff(), now=NOW)  # no local_tz
    assert abs(ps.slots[0].buy_eur_kwh - (0.10 + 0.10 + 0.01) * 1.21) < 1e-9


def test_the_holiday_predicate_threads_into_tariff_selection():
    buy = PriceFormula(grid_fees=(0.10, 0.02), holidays=True, schedules=(
        DaySchedule("all", "workday", (TariffSwitch(0, 1),)),
        DaySchedule("all", "holiday", (TariffSwitch(0, 2),)),
    ))
    tc = TariffConfig(buy=buy, sell=PriceFormula())
    prices = [make_price(12, 0.0)]
    assert build_price_series(prices, tc, now=NOW, local_tz=UTC).slots[0].buy_eur_kwh == 0.10
    on_holiday = build_price_series(prices, tc, now=NOW, local_tz=UTC, is_holiday=lambda day: True)
    assert on_holiday.slots[0].buy_eur_kwh == 0.02


def test_72h_applies_tariff_to_all_segments():
    tc = flat_tariff(buy_fee=0.03, vat=0.21, markup=0.01, sell_fee=0.0, sell_tax=0.0, sell_markup=0.0)
    yesterday = (_entry(-1, 0, 0.10),)
    today_tomorrow = [_entry(0, 0, 0.10)]
    nordpool = NordpoolData(entries=today_tomorrow, resolution=timedelta(hours=1))
    ps = build_price_series_72h(nordpool, YesterdayPrices(entries=yesterday), tc, now=NOW)
    expected_buy = (0.10 + 0.03 + 0.01) * 1.21
    priced = ps.priced_slots
    assert len(priced) == 2
    assert abs(priced[0].buy_eur_kwh - expected_buy) < 1e-9
    assert abs(priced[1].buy_eur_kwh - expected_buy) < 1e-9


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


def test_a_dynamic_sell_price_uses_the_export_price_where_the_feed_has_one():
    entry = PriceEntry(
        start=BASE_DT, end=BASE_DT + timedelta(hours=1),
        price_eur_kwh=0.10, export_price_eur_kwh=0.07,
    )
    tc = flat_tariff(sell_fee=0.0, sell_markup=0.0)
    ps = build_price_series([entry, make_price(1, 0.10)], tc, now=NOW)
    assert ps.slots[0].sell_eur_kwh == pytest.approx(0.07)
    assert ps.slots[1].sell_eur_kwh == pytest.approx(0.10)


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


def test_octopus_translator_reads_todays_and_tomorrows_day_rate_events():
    """Current Octopus Energy versions publish rates on event entities, in GBP/kWh, with datetime starts."""
    from custom_components.sun_sale.inbound.pricing import OctopusAgileTranslator

    base = "event.octopus_energy_electricity_1_2"
    today = _FakeState({"rates": [
        {"start": datetime(2024, 1, 15, 0, 0, tzinfo=UTC), "value_inc_vat": 0.21},
        {"start": datetime(2024, 1, 15, 0, 30, tzinfo=UTC), "value_inc_vat": 0.25},
    ]})
    tomorrow = _FakeState({"rates": [{"start": datetime(2024, 1, 16, 0, 0, tzinfo=UTC), "value_inc_vat": 0.30}]})
    export = _FakeState({"rates": [{"start": datetime(2024, 1, 15, 0, 0, tzinfo=UTC), "value_inc_vat": 0.15}]})
    hass = _FakeHass({
        f"{base}_current_day_rates": today,
        f"{base}_next_day_rates": tomorrow,
        f"{base}_export_current_day_rates": export,
    })
    feed = OctopusAgileTranslator(f"{base}_current_day_rates", f"{base}_export_current_day_rates").parse(hass, now=NOW)
    by_start = {entry.start: entry for entry in feed.entries}
    assert by_start[datetime(2024, 1, 15, tzinfo=UTC)].price_eur_kwh == 0.21
    assert by_start[datetime(2024, 1, 15, tzinfo=UTC)].export_price_eur_kwh == 0.15
    assert by_start[datetime(2024, 1, 16, tzinfo=UTC)].price_eur_kwh == 0.30
    assert feed.resolution == timedelta(minutes=30)


def test_octopus_rate_entities_cover_today_and_tomorrow_from_any_pick():
    from custom_components.sun_sale.inbound.pricing import octopus_rate_entities

    base = "octopus_energy_electricity_1_2"
    # A current-rate sensor (no rates attribute in current versions) leads to its event entities.
    assert octopus_rate_entities(f"sensor.{base}_current_rate") == [
        f"sensor.{base}_current_rate", f"event.{base}_current_day_rates", f"event.{base}_next_day_rates",
    ]
    assert octopus_rate_entities(f"event.{base}_export_current_day_rates") == [
        f"event.{base}_export_current_day_rates", f"event.{base}_export_next_day_rates",
    ]
    assert octopus_rate_entities("sensor.legacy_rates") == ["sensor.legacy_rates"]


def test_amber_translator_reads_dollars_as_published():
    """HA's Amber integration already divides the API's cents by 100 and flips feed-in's sign."""
    from custom_components.sun_sale.inbound.pricing import AmberTranslator

    general = _FakeState({"forecasts": [
        {"start_time": "2024-01-15T00:00:00+00:00", "end_time": "2024-01-15T00:30:00+00:00", "per_kwh": 0.30},
    ]})
    feed_in = _FakeState({"forecasts": [
        {"start_time": "2024-01-15T00:00:00+00:00", "end_time": "2024-01-15T00:30:00+00:00", "per_kwh": 0.08},
    ]})
    hass = _FakeHass({"sensor.amber_general": general, "sensor.amber_feed_in": feed_in})
    feed = AmberTranslator("sensor.amber_general", "sensor.amber_feed_in").parse(hass, now=NOW)
    assert abs(feed.entries[0].export_price_eur_kwh - 0.08) < 1e-9
    assert abs(feed.entries[0].price_eur_kwh - 0.30) < 1e-9


def test_fixed_price_translator_emits_a_zero_spot_slot_grid():
    from custom_components.sun_sale.inbound.pricing import FixedPriceTranslator

    translator = FixedPriceTranslator(resolution=timedelta(minutes=15), local_tz=UTC)
    feed = translator.parse(None, now=NOW.replace(hour=13))
    assert len(feed.entries) == 48 * 4  # today + tomorrow at 15 min
    assert feed.entries[0].start == BASE_DT
    assert {e.price_eur_kwh for e in feed.entries} == {0.0}


def test_fixed_price_translator_counts_days_in_local_time():
    from custom_components.sun_sale.inbound.pricing import FixedPriceTranslator

    # NOW is 02:00 in Riga (UTC+2), so local today started at 22:00 UTC the day before.
    feed = FixedPriceTranslator(local_tz=ZoneInfo("Europe/Riga")).parse(None, now=NOW)
    assert feed.entries[0].start == datetime(2024, 1, 14, 22, tzinfo=UTC)
    assert len(feed.entries) == 48


def test_build_price_translator_dispatch():
    from custom_components.sun_sale.inbound import pricing as p

    assert isinstance(p.build_price_translator("nordpool", entity_id="x"), p.NordpoolTranslator)
    assert isinstance(p.build_price_translator("entsoe", entity_id="x"), p.EntsoeTranslator)
    assert isinstance(p.build_price_translator("octopus", entity_id="x"), p.OctopusAgileTranslator)
    assert isinstance(p.build_price_translator("amber", entity_id="x"), p.AmberTranslator)
    assert isinstance(p.build_price_translator("fixed", entity_id=""), p.FixedPriceTranslator)
    # Unknown source (including the legacy "tou") falls back to Nordpool.
    assert isinstance(p.build_price_translator("???", entity_id="x"), p.NordpoolTranslator)


# ---------------------------------------------------------------------------
# Price-feed outage — the 2026-09-20 incident
#
# A restart with no DNS left the Nordpool sensor permanently `unavailable`, so
# the translator emitted nothing. The series then consisted of the persisted
# yesterday entries alone: the slot grid — which the generation and every
# observed series are resampled onto — collapsed to yesterday, and the
# resolution was reported as the translator's unused 1h default while the
# slots were really 15-min, quadrupling every `kW x slot_hours` downstream.
# ---------------------------------------------------------------------------

def _quarters(day_offset: int, price: float) -> tuple[PriceEntry, ...]:
    """Build one local day of 15-min entries at a constant price."""
    start = BASE_DT + timedelta(days=day_offset)
    q = timedelta(minutes=15)
    return tuple(
        PriceEntry(start=start + i * q, end=start + (i + 1) * q, price_eur_kwh=price)
        for i in range(96)
    )


def test_dead_feed_keeps_the_full_grid_and_marks_it_unpriced():
    """An empty feed must not shrink the grid the other series are built on."""
    dead = NordpoolData(entries=[], resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        dead, YesterdayPrices(entries=_quarters(-1, 0.05)),
        default_tariff_config(), now=NOW,
    )
    assert ps.slots[0].start == BASE_DT - timedelta(days=1)
    assert ps.slots[-1].end == BASE_DT + timedelta(days=2)
    # Only yesterday is real; today and tomorrow are placeholders.
    assert len(ps.priced_slots) == 96
    assert all(s.start < BASE_DT for s in ps.priced_slots)
    assert {s.priced for s in ps.slots} == {True, False}


def test_dead_feed_reports_the_resolution_the_slots_actually_use():
    """Resolution must come from the data, not the empty feed's 1h default."""
    dead = NordpoolData(entries=[], resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        dead, YesterdayPrices(entries=_quarters(-1, 0.05)),
        default_tariff_config(), now=NOW,
    )
    assert ps.resolution == timedelta(minutes=15)
    assert ps.slots[1].start - ps.slots[0].start == ps.resolution


def test_live_feed_resolution_still_wins_over_the_derived_spacing():
    """With data present the translator stays the source of truth."""
    today = [_entry(0, h, 0.10) for h in range(24)]
    feed = NordpoolData(entries=today, resolution=timedelta(hours=1))
    ps = build_price_series_72h(
        feed, YesterdayPrices(entries=()), default_tariff_config(), now=NOW,
    )
    assert ps.resolution == timedelta(hours=1)


def test_unpriced_slots_carry_the_provenance_tag():
    """Placeholders are greppable in the debug view / provenance tuple."""
    dead = NordpoolData(entries=[], resolution=timedelta(minutes=15))
    ps = build_price_series_72h(
        dead, YesterdayPrices(entries=_quarters(-1, 0.05)),
        default_tariff_config(), now=NOW,
    )
    placeholder = next(s for s in ps.slots if not s.priced)
    assert "unpriced" in placeholder.sources
    assert placeholder.spot_eur_kwh == 0.0


def test_zero_filled_tomorrow_is_not_presented_as_a_real_price():
    """Tomorrow before the auction publishes is unknown, not 0.00 EUR/kWh."""
    today = [_entry(0, h, 0.10) for h in range(24)]
    filled = _zero_fill_tomorrow(today, timedelta(hours=1), NOW)
    assert len(filled) == 48
    assert all(e.priced for e in filled[:24])
    assert not any(e.priced for e in filled[24:])
