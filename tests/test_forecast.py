"""Tests for forecast.py — pure Python, no HA mocking needed."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.sun_sale.inbound.forecast import build_generation_series
from custom_components.sun_sale.contract.models import PriceSeries, SolarData, SolarEntry
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.inbound.forecast import _tomorrow_entity
from tests.conftest import BASE_DT, default_tariff_config, make_price

NOW = BASE_DT


def _empty_price_series() -> PriceSeries:
    prices = [make_price(h, 0.10) for h in range(24)]
    return build_price_series(prices, default_tariff_config(), now=NOW)


def _make_solar_data_from_watts(watts_by_iso: dict[str, float], now=NOW) -> SolarData:
    """Build SolarData from {iso_str: watts} dict."""
    from custom_components.sun_sale.inbound.forecast import _watts_to_solar_entries, _make_solar_data
    parsed: dict[datetime, float] = {}
    for ts_str, w in watts_by_iso.items():
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed[dt.astimezone(timezone.utc).replace(second=0, microsecond=0)] = w
    entries = _watts_to_solar_entries(parsed)
    return _make_solar_data(entries, "open_meteo", now)


def _make_solar_data_from_forecast(forecast_slots: list[dict], now=NOW) -> SolarData:
    """Build SolarData from Forecast.Solar-style forecast_slots."""
    entries = []
    for slot in forecast_slots:
        try:
            dt = datetime.fromisoformat(slot["time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kwh = float(slot.get("pv_estimate", slot.get("energy", 0.0)))
            entries.append(SolarEntry(start=dt, end=dt + timedelta(hours=1), expected_kwh=kwh, source="forecast_solar"))
        except (KeyError, ValueError):
            continue
    from custom_components.sun_sale.inbound.forecast import _make_solar_data
    return _make_solar_data(entries, "forecast_solar" if entries else "none", now)


# ---------------------------------------------------------------------------
# Empty / missing cases
# ---------------------------------------------------------------------------

def test_empty_when_no_data():
    solar = SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    assert gen.slots == ()


def test_empty_when_empty_forecast_slots():
    solar = SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    assert gen.slots == ()


# ---------------------------------------------------------------------------
# Open Meteo (watts dict)
# ---------------------------------------------------------------------------

def test_open_meteo_watts_parsed():
    solar = _make_solar_data_from_watts({
        "2024-01-15T10:00:00+00:00": 2000.0,
        "2024-01-15T11:00:00+00:00": 3000.0,
    })
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    starts = {s.start.hour for s in gen.slots}
    assert 10 in starts
    assert 11 in starts


def test_open_meteo_15min_aggregated_to_hourly():
    # Four 15-min watts entries at 1000 W → each = 0.25 kWh, sum = 1.0 kWh.
    # With an hourly price grid we expect exactly one hour-10 slot at 1.0 kWh.
    solar = _make_solar_data_from_watts({
        "2024-01-15T10:00:00+00:00": 1000.0,
        "2024-01-15T10:15:00+00:00": 1000.0,
        "2024-01-15T10:30:00+00:00": 1000.0,
        "2024-01-15T10:45:00+00:00": 1000.0,
    })
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    slots_10 = [s for s in gen.slots if s.start.hour == 10]
    assert len(slots_10) == 1
    assert abs(slots_10[0].expected_kwh - 1.0) < 1e-5
    assert slots_10[0].end - slots_10[0].start == timedelta(hours=1)


def test_open_meteo_two_arrays_summed():
    # Simulate two arrays combined: translator already sums → 2000 W total
    solar = _make_solar_data_from_watts({"2024-01-15T10:00:00+00:00": 2000.0})
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    slot_10 = next(s for s in gen.slots if s.start.hour == 10)
    # 2000 W × 1 h / 1000 = 2.0 kWh (hourly slot)
    assert abs(slot_10.expected_kwh - 2.0) < 1e-5


# ---------------------------------------------------------------------------
# Forecast.Solar / Solcast fallback
# ---------------------------------------------------------------------------

def test_forecast_solar_pv_estimate_parsed():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 1.5},
        {"time": "2024-01-15T11:00:00", "pv_estimate": 2.0},
    ])
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    by_hour = {s.start.hour: s.expected_kwh for s in gen.slots}
    assert abs(by_hour[10] - 1.5) < 1e-9
    assert abs(by_hour[11] - 2.0) < 1e-9


def test_forecast_solar_energy_fallback():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "energy": 1.2},
    ])
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    slot_10 = next(s for s in gen.slots if s.start.hour == 10)
    assert abs(slot_10.expected_kwh - 1.2) < 1e-9


def test_forecast_solar_skips_bad_entries():
    solar = _make_solar_data_from_forecast([
        {"time": "bad-date", "pv_estimate": 1.0},
        {"time": "2024-01-15T10:00:00", "pv_estimate": 2.0},
    ])
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    non_zero = [s for s in gen.slots if s.expected_kwh > 0]
    assert len(non_zero) == 1
    assert abs(non_zero[0].expected_kwh - 2.0) < 1e-9


# ---------------------------------------------------------------------------
# energy_between helper
# ---------------------------------------------------------------------------

def test_energy_between_full_slot():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 3.0},
    ])
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    t1 = NOW.replace(hour=10)
    t2 = NOW.replace(hour=11)
    assert abs(gen.energy_between(t1, t2) - 3.0) < 1e-9


def test_energy_between_partial_slot():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 4.0},
    ])
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    t1 = NOW.replace(hour=10, minute=30)
    t2 = NOW.replace(hour=11)
    assert abs(gen.energy_between(t1, t2) - 2.0) < 1e-6


def test_energy_between_no_overlap():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 3.0},
    ])
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    t1 = NOW.replace(hour=12)
    t2 = NOW.replace(hour=13)
    assert gen.energy_between(t1, t2) == 0.0


# ---------------------------------------------------------------------------
# Resampling to PriceSeries grid
# ---------------------------------------------------------------------------

def _quarter_hour_price_series() -> PriceSeries:
    """24h × 4 slots/h = 96 slots at 15-min resolution."""
    base = NOW
    entries = []
    for q in range(24 * 4):
        start = base + timedelta(minutes=15 * q)
        from custom_components.sun_sale.contract.models import PriceEntry
        entries.append(PriceEntry(start=start, end=start + timedelta(minutes=15), price_eur_kwh=0.10))
    return build_price_series(entries, default_tariff_config(), now=NOW)


def test_hourly_forecast_upsampled_to_quarter_hour_grid():
    # 4.0 kWh in one hourly entry → on a 15-min grid that becomes 4 × 1.0 kWh slots
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 4.0},
    ])
    gen = build_generation_series(solar, _quarter_hour_price_series().slots, now=NOW)
    slots_10 = [s for s in gen.slots if s.start.hour == 10]
    assert len(slots_10) == 4
    for s in slots_10:
        assert abs(s.expected_kwh - 1.0) < 1e-6
        assert s.end - s.start == timedelta(minutes=15)


def test_resampled_slots_match_price_grid_one_to_one():
    """Every price slot has exactly one matching generation slot, same grid."""
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T10:00:00", "pv_estimate": 1.0},
        {"time": "2024-01-15T11:00:00", "pv_estimate": 1.0},
    ])
    ps = _empty_price_series()
    gen = build_generation_series(solar, ps.slots, now=NOW)
    assert len(gen.slots) == len(ps.slots)
    for g, p in zip(gen.slots, ps.slots):
        assert g.start == p.start
        assert g.end == p.end


def test_continuous_72h_coverage_with_zero_fill():
    """72h hourly price grid → 72 generation slots; gaps in solar are 0-filled."""
    from custom_components.sun_sale.contract.models import PriceEntry
    entries = [
        PriceEntry(
            start=NOW - timedelta(days=1) + timedelta(hours=h),
            end=NOW - timedelta(days=1) + timedelta(hours=h + 1),
            price_eur_kwh=0.10,
        )
        for h in range(72)
    ]
    ps = build_price_series(entries, default_tariff_config(), now=NOW)
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T12:00:00", "pv_estimate": 4.0},  # only today noon
    ])
    gen = build_generation_series(solar, ps.slots, now=NOW)
    assert len(gen.slots) == 72
    # nighttime/yesterday slots zero-filled
    nighttime_slot = next(s for s in gen.slots if s.start.hour == 3 and s.start.date().day == 14)
    assert nighttime_slot.expected_kwh == 0.0
    # solar slot carries the kwh
    noon_slot = next(s for s in gen.slots if s.start == NOW.replace(hour=12))
    assert abs(noon_slot.expected_kwh - 4.0) < 1e-6


# ---------------------------------------------------------------------------
# Per-day totals
# ---------------------------------------------------------------------------

def test_per_day_totals_split_across_yesterday_today_tomorrow():
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-14T10:00:00", "pv_estimate": 2.0},   # yesterday
        {"time": "2024-01-15T10:00:00", "pv_estimate": 3.0},   # today
        {"time": "2024-01-15T11:00:00", "pv_estimate": 1.0},   # today
        {"time": "2024-01-16T10:00:00", "pv_estimate": 5.0},   # tomorrow
    ])
    # Build a 72h hourly price grid so all three days are covered
    from custom_components.sun_sale.contract.models import PriceEntry
    entries = [
        PriceEntry(
            start=NOW - timedelta(days=1) + timedelta(hours=h),
            end=NOW - timedelta(days=1) + timedelta(hours=h + 1),
            price_eur_kwh=0.10,
        )
        for h in range(72)
    ]
    ps = build_price_series(entries, default_tariff_config(), now=NOW)
    gen = build_generation_series(solar, ps.slots, now=NOW)
    assert abs(gen.total_yesterday_kwh - 2.0) < 1e-6
    assert abs(gen.total_today_kwh - 4.0) < 1e-6
    assert abs(gen.total_tomorrow_kwh - 5.0) < 1e-6


def test_today_remaining_excludes_past_slots():
    # NOW is midnight, advance "now" to 10:30 — only slots starting >= 10:30 count
    now = NOW.replace(hour=10, minute=30)
    solar = _make_solar_data_from_forecast([
        {"time": "2024-01-15T09:00:00", "pv_estimate": 2.0},   # past
        {"time": "2024-01-15T11:00:00", "pv_estimate": 3.0},   # future, today
        {"time": "2024-01-15T12:00:00", "pv_estimate": 4.0},   # future, today
    ], now=now)
    gen = build_generation_series(solar, _empty_price_series().slots, now=now)
    assert abs(gen.total_today_kwh - 9.0) < 1e-6
    assert abs(gen.today_remaining_kwh - 7.0) < 1e-6


def test_empty_solar_yields_zero_totals():
    solar = SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")
    gen = build_generation_series(solar, _empty_price_series().slots, now=NOW)
    assert gen.total_yesterday_kwh == 0.0
    assert gen.total_today_kwh == 0.0
    assert gen.total_tomorrow_kwh == 0.0
    assert gen.today_remaining_kwh == 0.0


# ---------------------------------------------------------------------------
# _tomorrow_entity helper
# ---------------------------------------------------------------------------

def test_tomorrow_entity_today_suffix():
    assert _tomorrow_entity("sensor.energy_production_today") == "sensor.energy_production_tomorrow"


def test_tomorrow_entity_today_infix():
    assert _tomorrow_entity("sensor.energy_production_today_2") == "sensor.energy_production_tomorrow_2"


def test_tomorrow_entity_no_today():
    assert _tomorrow_entity("sensor.energy_production") == ""


# ---------------------------------------------------------------------------
# SolarTranslator.parse — base_entities list signature (device-based selection)
# ---------------------------------------------------------------------------


class _FakeHass:
    """Minimal hass exposing only states.get for SolarTranslator.parse."""

    def __init__(self, states: dict):
        self._states = states
        self.states = self

    def get(self, entity_id):
        return self._states.get(entity_id)


class _FakeState:
    """Minimal HA state stub carrying only attributes."""

    def __init__(self, attributes: dict):
        self.attributes = attributes


def test_translator_sums_n_base_entities():
    """Three base 'today' entities (each with a watts dict) sum per timestamp."""
    from custom_components.sun_sale.inbound.forecast import SolarTranslator

    ts = "2024-01-15T10:00:00+00:00"
    states = {
        f"sensor.array{i}_today": _FakeState({"watts": {ts: 1000.0}})
        for i in range(1, 4)
    }
    translator = SolarTranslator(base_entities=[
        "sensor.array1_today", "sensor.array2_today", "sensor.array3_today",
    ])
    solar = translator.parse(_FakeHass(states), now=NOW)
    # 3 × 1000 W = 3000 W summed at the shared timestamp; single-timestamp slot
    # defaults to a 1 h duration → 3000 W × 1 h / 1000 = 3.0 kWh.
    entry = next(e for e in solar.entries if e.start.hour == 10)
    assert abs(entry.expected_kwh - 3.0) < 1e-6


def test_translator_empty_base_entities_yields_empty():
    from custom_components.sun_sale.inbound.forecast import SolarTranslator

    solar = SolarTranslator(base_entities=[]).parse(_FakeHass({}), now=NOW)
    assert solar.entries == []
    assert solar.primary_source == "none"


def test_translator_filters_empty_strings():
    from custom_components.sun_sale.inbound.forecast import SolarTranslator

    t = SolarTranslator(base_entities=["", "sensor.x_today", ""])
    assert t._base_entities == ["sensor.x_today"]


# ---------------------------------------------------------------------------
# local_tz: post-midnight UTC+3 boundary
# ---------------------------------------------------------------------------


def test_local_tz_post_midnight_buckets_by_local_date():
    """After local midnight the UTC date is still the previous day.

    Without local_tz, slots for the new local day fall on UTC 'today' only for
    the nighttime hours (near-zero solar), making total_today_kwh ≈ 0 and the
    13 daytime kWh appear as 'tomorrow'.  With local_tz all slots for the new
    local day correctly bucket to 'today'.
    """
    from custom_components.sun_sale.inbound.forecast import _compute_totals
    # Use a fixed UTC+3 offset to avoid DST complications in the test.
    tz_plus3 = timezone(timedelta(hours=3))

    # Scenario: UTC 21:43 Jan 15 = local 00:43 Jan 16 (new local day just started)
    now_utc = datetime(2024, 1, 15, 21, 43, 0, tzinfo=timezone.utc)

    slot_dur = timedelta(hours=1)

    # 13 daytime slots for UTC Jan 16 (= local Jan 16, daytime), 1 kWh each
    daytime_slots = tuple(
        SolarEntry(
            start=datetime(2024, 1, 16, h, 0, 0, tzinfo=timezone.utc),
            end=datetime(2024, 1, 16, h, 0, 0, tzinfo=timezone.utc) + slot_dur,
            expected_kwh=1.0,
            source="open_meteo",
        )
        for h in range(5, 18)  # UTC 05-17 = local 08-20
    )
    # 3 nighttime slots for UTC Jan 15 21-23 (= local Jan 16 00-02), 0 kWh each
    night_slots = tuple(
        SolarEntry(
            start=datetime(2024, 1, 15, h, 0, 0, tzinfo=timezone.utc),
            end=datetime(2024, 1, 15, h, 0, 0, tzinfo=timezone.utc) + slot_dur,
            expected_kwh=0.0,
            source="open_meteo",
        )
        for h in range(21, 24)  # UTC 21-23 = local 00-02
    )
    all_slots = night_slots + daytime_slots

    # UTC bucketing: UTC today = Jan 15; nighttime slots (UTC Jan 15) go to today (0 kWh);
    # daytime slots (UTC Jan 16) go to tomorrow (13 kWh)
    totals_utc = _compute_totals(all_slots, now_utc)
    assert abs(totals_utc["today"] - 0.0) < 1e-4
    assert abs(totals_utc["tomorrow"] - 13.0) < 1e-4

    # Local bucketing: local today = Jan 16; both nighttime and daytime slots are
    # local Jan 16, so all 13 daytime kWh go to "today"
    totals_local = _compute_totals(all_slots, now_utc, local_tz=tz_plus3)
    assert abs(totals_local["today"] - 13.0) < 1e-4
    assert abs(totals_local["tomorrow"] - 0.0) < 1e-4
