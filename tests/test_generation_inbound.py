"""Tests for inbound/observer/generation.py — pure Python, no HA required.

After the bake-in redesign, the per-cycle counter correction and the
counter-difference fallback have been removed; only the PV-power averaging
path remains. Yesterday's slots are raw averages until the once-per-day
bake-in (Phase 3) replaces them with proportionally-corrected values.
"""
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.const import SOURCE_KIND_DEDICATED_SENSOR
from custom_components.sun_sale.contract.models import (
    BakedDayRecord,
    BakedObservedHistory,
    PriceEntry,
    PriceSeries,
    PvPowerHistory,
    PvPowerReading,
    SlotKwh,
)
from custom_components.sun_sale.inbound.observer.generation import (
    GENERATION_SIDE_ID,
    build_observed_generation_series,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_tariff_config, make_price

# BASE_DT is 2024-01-15 00:00 UTC. Use NOW for "today", YESTERDAY for the day before.
NOW = BASE_DT
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)

_NO_POWER = PvPowerHistory(samples=())


def _hourly_72h_price_series() -> PriceSeries:
    """72h hourly grid covering yesterday 00:00 → tomorrow 23:59 (UTC)."""
    base = NOW - timedelta(days=1)
    entries = [
        PriceEntry(
            start=base + timedelta(hours=h),
            end=base + timedelta(hours=h + 1),
            price_eur_kwh=0.10,
        )
        for h in range(72)
    ]
    return build_price_series(entries, default_tariff_config(), now=NOW)


def _empty_hourly_today() -> PriceSeries:
    return build_price_series(
        [make_price(h, 0.10) for h in range(24)],
        default_tariff_config(),
        now=NOW,
    )


def _power(t: datetime, w: float) -> PvPowerReading:
    return PvPowerReading(power_w=w, timestamp=t)


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------

def test_empty_history_yields_empty_series():
    series = build_observed_generation_series(
        _NO_POWER, _empty_hourly_today().slots, now=NOW
    )
    assert series.slots == ()
    assert series.total_yesterday_kwh == 0.0
    assert series.total_today_so_far_kwh == 0.0


def test_empty_price_grid_yields_empty_series():
    history = PvPowerHistory(samples=(_power(NOW, 2000.0),))
    series = build_observed_generation_series(history, (), now=NOW)
    assert series.slots == ()


# ---------------------------------------------------------------------------
# PV power averaging — slot kWh
# ---------------------------------------------------------------------------

def test_power_averaging_one_sample_per_slot():
    """2000 W reading at 10:30 → 1h slot → 2.0 kWh."""
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 2000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 2.0) < 1e-6


def test_power_averaging_multiple_samples_averaged():
    """1000 W and 3000 W in hour-10 → average 2000 W → 2.0 kWh."""
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=10), 1000.0),
        _power(NOW.replace(hour=10, minute=50), 3000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 2.0) < 1e-6


def test_power_slot_with_no_samples_gives_zero():
    """Only hour-10 has a reading; hour-11 reports 0."""
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    slot_11 = next(s for s in series.slots if s.start == NOW.replace(hour=11))
    assert slot_11.generated_kwh == 0.0


# ---------------------------------------------------------------------------
# Window: yesterday 00:00 → now
# ---------------------------------------------------------------------------

def test_power_path_covers_yesterday_slots():
    """Power readings from yesterday produce kWh for yesterday's slots."""
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(yesterday_dt.replace(hour=10, minute=30), 4000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    slot = next(
        s for s in series.slots
        if s.start.date() == YESTERDAY and s.start.hour == 10
    )
    assert abs(slot.generated_kwh - 4.0) < 1e-6


def test_slots_in_tomorrow_excluded():
    now = NOW.replace(hour=10)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=8, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    tomorrow_date = (TODAY + timedelta(days=1))
    assert all(s.start.date() != tomorrow_date for s in series.slots)


def test_slots_starting_at_or_after_now_excluded():
    now = NOW.replace(hour=10, minute=30)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=9, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    # Hour-10 slot starts at 10:00 (< 10:30), so it's included.
    # Hour-11 starts at 11:00 (>= 10:30), so it's excluded.
    starts = {s.start for s in series.slots}
    assert NOW.replace(hour=10) in starts
    assert NOW.replace(hour=11) not in starts


def test_slots_before_yesterday_midnight_excluded():
    """A sample two days back must not produce a slot in the series."""
    two_days_ago = NOW - timedelta(days=2)
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(two_days_ago.replace(hour=10, minute=30), 1000.0),
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    assert all(s.start >= NOW - timedelta(days=1) for s in series.slots)


# ---------------------------------------------------------------------------
# Per-day totals
# ---------------------------------------------------------------------------

def test_totals_split_between_yesterday_and_today():
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(yesterday_dt.replace(hour=10, minute=30), 3000.0),
        _power(NOW.replace(hour=10, minute=30), 2000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    assert abs(series.total_yesterday_kwh - 3.0) < 1e-6
    assert abs(series.total_today_so_far_kwh - 2.0) < 1e-6


def test_source_is_inverter_on_every_slot():
    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now
    )
    assert all(s.source == "inverter" for s in series.slots)


def test_yesterday_slots_substituted_from_baked_history():
    """When baked_history holds a record for yesterday, those slots are used."""
    yesterday_dt = NOW - timedelta(days=1)
    now = NOW.replace(hour=12)
    # Raw averaging would give yesterday hour-10 = 2.0 kWh; baked says 3.5.
    power_history = PvPowerHistory(samples=(
        _power(yesterday_dt.replace(hour=10, minute=30), 2000.0),
        _power(NOW.replace(hour=10, minute=30), 1000.0),
    ))
    baked_slot = SlotKwh(
        start=yesterday_dt.replace(hour=10),
        end=yesterday_dt.replace(hour=11),
        kwh=3.5,
    )
    baked_record = BakedDayRecord(
        date_str=YESTERDAY.isoformat(),
        side_id=GENERATION_SIDE_ID,
        counter_total_used=3.5,
        source_kind=SOURCE_KIND_DEDICATED_SENSOR,
        baked_slots=(baked_slot,),
        baked_sum=3.5,
        baked_at=now,
    )
    baked_history = BakedObservedHistory(records=(baked_record,))

    series = build_observed_generation_series(
        power_history, _hourly_72h_price_series().slots, now=now,
        baked_history=baked_history,
    )
    yest_slot = next(
        s for s in series.slots
        if s.start.date() == YESTERDAY and s.start.hour == 10
    )
    assert abs(yest_slot.generated_kwh - 3.5) < 1e-6
    # total_yesterday_kwh should reflect the baked sum, not the raw 2.0.
    assert abs(series.total_yesterday_kwh - 3.5) < 1e-3


def test_resamples_onto_quarter_hour_grid():
    """A 1h-flat reading at 10:30 produces four 0.25 kWh slots on a 15-min grid."""
    quarter_entries = [
        PriceEntry(
            start=NOW + timedelta(minutes=15 * q),
            end=NOW + timedelta(minutes=15 * (q + 1)),
            price_eur_kwh=0.10,
        )
        for q in range(96)
    ]
    ps = build_price_series(quarter_entries, default_tariff_config(), now=NOW)
    now = NOW.replace(hour=12)
    # Five samples within hour-10 — one in each quarter — all at 1000 W. Each
    # 15-min slot averages 1 kW × 0.25 h = 0.25 kWh.
    power_history = PvPowerHistory(samples=tuple(
        _power(NOW.replace(hour=10, minute=m), 1000.0) for m in (5, 20, 35, 50)
    ))
    series = build_observed_generation_series(power_history, ps.slots, now=now)
    slots_10 = [s for s in series.slots if NOW.replace(hour=10) <= s.start < NOW.replace(hour=11)]
    assert len(slots_10) == 4
    for s in slots_10:
        assert abs(s.generated_kwh - 0.25) < 1e-6


# ---------------------------------------------------------------------------
# Node wiring
# ---------------------------------------------------------------------------

def test_observed_generation_node_produces_series_from_pv_power():
    """The DAG node reads PvPowerHistory + PriceSeries + baked history."""
    import asyncio

    from custom_components.sun_sale.contract.models import (
        BakedObservedHistory,
        ObservedGenerationSeries,
        SunSaleConfig,
    )
    from custom_components.sun_sale.pipeline.dag_engine import NodeContext
    from custom_components.sun_sale.pipeline.nodes import ObservedGenerationNode

    now = NOW.replace(hour=12)
    power_history = PvPowerHistory(samples=(
        _power(NOW.replace(hour=10, minute=30), 2500.0),
    ))
    config = SunSaleConfig(tariff=None, battery=None)
    ctx = NodeContext(
        primary={
            PvPowerHistory: power_history,
            BakedObservedHistory: BakedObservedHistory(records=()),
        },
        secondary={PriceSeries: _hourly_72h_price_series()},
        config=config,
        now=now,
    )

    node = ObservedGenerationNode()
    asyncio.run(node.run(ctx))

    series = ctx.secondary[ObservedGenerationSeries]
    assert isinstance(series, ObservedGenerationSeries)
    slot_10 = next(s for s in series.slots if s.start == NOW.replace(hour=10))
    assert abs(slot_10.generated_kwh - 2.5) < 1e-6
    assert abs(series.total_today_so_far_kwh - 2.5) < 1e-6
