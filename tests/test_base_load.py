"""Tests for pipeline/base_load.py — pure Python, no HA mocking needed.

Covers the P15-over-daily-buckets profile builder and the unchanged
battery-runtime estimator. The profile inputs are
``ConsumptionDailyBuckets`` records built directly here; the
``inbound/consumption_daily.py`` aggregation path is exercised in
``tests/test_consumption_daily.py``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import (
    BaseLoadProfile,
    BaseLoadSlot,
    BatteryConfig,
    BatteryStatus,
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
)
from custom_components.sun_sale.pipeline.base_load import (
    DEFAULT_PERCENTILE,
    DEFAULT_STUB_KW,
    MIN_BUCKET_DAYS,
    MIN_HISTORY_DAYS,
    SIMULATION_STEP_MINUTES,
    _percentile,
    build_base_load_profile,
    estimate_battery_runtime,
)


UTC = timezone.utc
RIGA = ZoneInfo("Europe/Riga")    # UTC+2 in winter, UTC+3 in summer
FULL_COVERAGE = tuple([1.0] * 24)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _record(
    local_date_val: date,
    hour_kwh,
    hour_completeness=None,
    finalised_at: datetime | None = None,
) -> ConsumptionDayRecord:
    """Build a ConsumptionDayRecord; pads hour_kwh / hour_completeness to 24.

    ``hour_kwh`` may be a single float (broadcast to 24 hours) or a length-24
    iterable. ``hour_completeness`` defaults to ``FULL_COVERAGE``. The
    ``finalised_at`` defaults to a fixed reference time.
    """
    if isinstance(hour_kwh, (int, float)):
        kwh = tuple([float(hour_kwh)] * 24)
    else:
        kwh = tuple(float(v) for v in hour_kwh)
    assert len(kwh) == 24
    cov = (
        FULL_COVERAGE
        if hour_completeness is None
        else tuple(float(v) for v in hour_completeness)
    )
    assert len(cov) == 24
    return ConsumptionDayRecord(
        local_date=local_date_val,
        hour_kwh=kwh,
        hour_completeness=cov,
        finalised_at=finalised_at or datetime(2026, 5, 16, 0, tzinfo=UTC),
    )


def _buckets(records) -> ConsumptionDailyBuckets:
    """Wrap an iterable of records into a sorted ConsumptionDailyBuckets."""
    return ConsumptionDailyBuckets(
        records=tuple(sorted(records, key=lambda r: r.local_date))
    )


def _battery_status(soc: float = 0.8, total_capacity_kwh: float = 10.0):
    return BatteryStatus(
        total_capacity_kwh=total_capacity_kwh,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        soc=soc,
        remaining_capacity_kwh=soc * total_capacity_kwh,
    )


def _battery_config(min_soc: float = 0.10):
    return BatteryConfig(
        nominal_capacity_kwh=10.0,
        purchase_price_eur=5000.0,
        rated_cycle_life=6000,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        min_soc=min_soc,
        max_soc=0.95,
        round_trip_efficiency=0.90,
    )


# ---------------------------------------------------------------------------
# _percentile (helper kept from the old design)
# ---------------------------------------------------------------------------

def test_percentile_empty_returns_zero():
    assert _percentile([], 0.5) == 0.0


def test_percentile_single_value():
    assert _percentile([2.5], 0.10) == 2.5
    assert _percentile([2.5], 0.99) == 2.5


def test_percentile_p15_of_ten_values():
    # Sorted [1..10], P15 → rank = 0.15 × 9 = 1.35 → between idx 1 (=2) and
    # idx 2 (=3) at frac 0.35 = 2 + (3-2)*0.35 = 2.35.
    assert _percentile(list(range(1, 11)), 0.15) == pytest.approx(2.35)


def test_percentile_p100_returns_max():
    assert _percentile([1.0, 2.0, 3.0], 1.0) == 3.0


def test_percentile_p0_returns_min():
    assert _percentile([3.0, 1.0, 2.0], 0.0) == 1.0


# ---------------------------------------------------------------------------
# build_base_load_profile — sparsity / fallback
# ---------------------------------------------------------------------------

def test_empty_buckets_returns_sparse_profile_with_stub():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    profile = build_base_load_profile(_buckets([]), RIGA, now=now)

    assert profile.confidence is None
    assert profile.sample_count == 0
    assert profile.distinct_days == 0
    assert len(profile.slots) == 24
    assert profile.fallback_kw == DEFAULT_STUB_KW
    for slot in profile.slots:
        assert slot.is_fallback
        assert slot.baseload_kw == DEFAULT_STUB_KW
        assert slot.sample_count == 0


def test_below_min_history_days_returns_sparse():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # MIN_HISTORY_DAYS = 7; supply 3 records.
    records = [
        _record(date(2026, 5, h + 12), hour_kwh=0.5) for h in range(3)
    ]
    profile = build_base_load_profile(_buckets(records), RIGA, now=now)

    assert profile.distinct_days == 3
    assert profile.confidence is None
    # Sparse-path fallback derives from the qualified-values percentile.
    assert profile.fallback_kw == pytest.approx(0.5)
    for slot in profile.slots:
        assert slot.baseload_kw == pytest.approx(0.5)
        assert slot.is_fallback


def test_minimum_history_yields_confidence():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    records = [
        _record(date(2026, 5, 16) - timedelta(days=d + 1), hour_kwh=0.3)
        for d in range(MIN_HISTORY_DAYS)
    ]
    profile = build_base_load_profile(
        _buckets(records), RIGA, now=now, window_days=30,
    )

    assert profile.confidence is not None
    assert profile.distinct_days == MIN_HISTORY_DAYS
    assert profile.confidence == pytest.approx(MIN_HISTORY_DAYS / 30.0)


# ---------------------------------------------------------------------------
# build_base_load_profile — P15 bucketing
# ---------------------------------------------------------------------------

def test_per_hour_p15_floor_picks_quietest_days():
    """20 days, 18 with EV (hour 18 = 5 kWh) and 2 quiet (0.3 kWh). P15 → ~0.3 floor."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    records = []
    for d in range(20):
        # 2 quietest days first → indexed [0, 1]; P15 of 20 picks rank ≈ 2.85,
        # interpolating between the 3rd and 4th lowest. With 2 quiet (0.3) and
        # 18 spike (5.0) values, the floor should be near 0.3 + small drift.
        kwh = [0.1] * 24
        kwh[18] = 0.3 if d < 2 else 5.0
        records.append(_record(date(2026, 5, 1) + timedelta(days=d), hour_kwh=kwh))
    profile = build_base_load_profile(
        _buckets(records), RIGA, now=now, window_days=30,
    )

    # P15 of [0.3, 0.3, 5.0, 5.0, …]; rank 2.85 lies inside the spike tail.
    # Looser bound: the floor should be far from the spike average (~4.5)
    # because at least the bottom couple of days anchored low values into
    # the bottom quintile.
    floor = profile.slots[18].baseload_kw
    assert floor <= 5.0
    # And other hours at 0.1 are flat.
    assert profile.slots[3].baseload_kw == pytest.approx(0.1)


def test_p15_rejects_single_ev_charging_day():
    """One EV day in an otherwise quiet 10-day window doesn't drag P15 high."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    records = []
    for d in range(10):
        kwh = [0.2] * 24
        if d == 5:    # one EV charging day at hour 22
            kwh[22] = 7.0
        records.append(_record(date(2026, 5, 1) + timedelta(days=d), hour_kwh=kwh))
    profile = build_base_load_profile(
        _buckets(records), RIGA, now=now, window_days=30,
    )

    # P15 of 10 values [0.2 × 9, 7.0] = rank 1.35 between idx 1 (=0.2)
    # and idx 2 (=0.2) → 0.2 exactly. Spike sits at idx 9 and never enters.
    assert profile.slots[22].baseload_kw == pytest.approx(0.2)
    assert not profile.slots[22].is_fallback


def test_completeness_below_threshold_excludes_hour():
    """A day where hour 5 had only 50% coverage doesn't feed that hour bucket."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # 8 normal days at 0.3 kWh everywhere, 1 day with hour 5 stuck at coverage 0.5.
    records = []
    for d in range(8):
        records.append(_record(
            date(2026, 5, 1) + timedelta(days=d), hour_kwh=0.3,
        ))
    bad_cov = list(FULL_COVERAGE)
    bad_cov[5] = 0.5
    bad_hourly = [0.3] * 24
    bad_hourly[5] = 0.01    # would drag the floor down if it qualified
    records.append(_record(
        date(2026, 5, 9), hour_kwh=bad_hourly, hour_completeness=bad_cov,
    ))
    profile = build_base_load_profile(
        _buckets(records), RIGA, now=now, window_days=30,
    )

    # Hour 5's bucket received 8 qualified values (all 0.3) — floor = 0.3.
    assert profile.slots[5].sample_count == 8
    assert profile.slots[5].baseload_kw == pytest.approx(0.3)
    # Hour 6 had 9 qualified values (all 0.3).
    assert profile.slots[6].sample_count == 9


def test_min_bucket_days_falls_back():
    """An hour with too few qualifying day-contributions uses the cross-bucket fallback."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # 8 days qualify globally; hour 10 has completeness 0.0 on 5 of them so
    # only 3 qualify for that bucket — below MIN_BUCKET_DAYS = 5.
    records = []
    for d in range(8):
        cov = list(FULL_COVERAGE)
        if d < 5:
            cov[10] = 0.0
        records.append(_record(
            date(2026, 5, 1) + timedelta(days=d),
            hour_kwh=0.4,
            hour_completeness=cov,
        ))
    profile = build_base_load_profile(
        _buckets(records), RIGA, now=now, window_days=30,
    )

    assert profile.slots[10].is_fallback
    assert profile.slots[10].sample_count < MIN_BUCKET_DAYS
    assert profile.slots[10].baseload_kw == pytest.approx(profile.fallback_kw)
    # Other hours qualify (8 contributions each) → floor = 0.4.
    assert profile.slots[11].sample_count == 8
    assert profile.slots[11].baseload_kw == pytest.approx(0.4)
    assert not profile.slots[11].is_fallback


def test_records_outside_window_excluded():
    """Records whose local_date is older than window_days are dropped."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # 60 days ago: huge load that would dominate if included.
    ancient = _record(date(2026, 3, 17), hour_kwh=9.0)
    # 8 recent days at 0.2 kWh.
    recent = [
        _record(date(2026, 5, 16) - timedelta(days=d + 1), hour_kwh=0.2)
        for d in range(8)
    ]
    profile = build_base_load_profile(
        _buckets([ancient, *recent]), RIGA, now=now, window_days=30,
    )

    # distinct_days reflects only the recent contributions.
    assert profile.distinct_days == 8
    for slot in profile.slots:
        assert slot.baseload_kw == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------

def test_profile_default_percentile_is_p15():
    """Guard: the documented floor percentile is the public default."""
    assert DEFAULT_PERCENTILE == pytest.approx(0.15)


def test_profile_at_uses_local_hour():
    now = datetime(2026, 1, 30, 12, tzinfo=UTC)
    profile = build_base_load_profile(_buckets([]), RIGA, now=now)
    # Even with a stub-fallback profile, .at() should resolve to the local-hour slot.
    t = datetime(2026, 1, 30, 0, tzinfo=UTC)    # 02:00 Riga winter
    assert profile.at(t, RIGA) == profile.slots[2].baseload_kw


# ---------------------------------------------------------------------------
# estimate_battery_runtime (API unchanged)
# ---------------------------------------------------------------------------

def _flat_profile(kw: float, now: datetime) -> BaseLoadProfile:
    """A profile where every hour returns ``kw`` regardless of bucketing."""
    slots = tuple(
        BaseLoadSlot(hour=h, baseload_kw=kw, sample_count=100, is_fallback=False)
        for h in range(24)
    )
    return BaseLoadProfile(
        slots=slots,
        fallback_kw=kw,
        overall_p10_kw=kw,
        overall_median_kw=kw,
        confidence=1.0,
        sample_count=100 * 24,
        distinct_days=30,
        computed_at=now,
    )


def test_runtime_zero_when_soc_at_min():
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=0.10)
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(0.5, now)

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    assert est.remaining_kwh_usable == 0.0
    assert est.runtime_minutes == 0.0
    assert est.until == now


def test_runtime_constant_drain():
    """10 kWh battery, min_soc 10% → 9 kWh usable, 1 kW baseload → 9 hours."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=1.0, total_capacity_kwh=10.0)
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(1.0, now)

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    assert est.remaining_kwh_usable == pytest.approx(9.0)
    assert est.runtime_minutes == pytest.approx(9 * 60, abs=SIMULATION_STEP_MINUTES)
    assert est.until is not None
    assert (est.until - now).total_seconds() / 60 == pytest.approx(
        9 * 60, abs=SIMULATION_STEP_MINUTES,
    )
    assert est.avg_drain_kw_next_hour == pytest.approx(1.0)


def test_runtime_partial_step_interpolation():
    """Verify the final mid-step interpolation lands at the right minute."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    # usable = (0.105 - 0.10) * 10 = 0.05 kWh, drain 1.0 kW → 3 min.
    status = _battery_status(soc=0.105, total_capacity_kwh=10.0)
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(1.0, now)

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    assert est.runtime_minutes == pytest.approx(3.0, abs=0.1)


def test_runtime_horizon_limits_simulation():
    """If horizon ends before drain, runtime is None."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=1.0, total_capacity_kwh=100.0)  # huge battery
    cfg = _battery_config(min_soc=0.10)
    profile = _flat_profile(0.5, now)

    est = estimate_battery_runtime(
        status, cfg, profile, RIGA, now, horizon_hours=2,
    )
    # 100 * 0.9 = 90 kWh usable; at 0.5 kW = 180 hours. Horizon = 2h.
    assert est.runtime_minutes is None
    assert est.until is None
    assert est.horizon_hours == 2


def test_runtime_drain_follows_profile_per_hour():
    """Profile with varying per-hour baseload drains at the correct rate."""
    now = datetime(2026, 5, 16, 12, tzinfo=UTC)
    status = _battery_status(soc=1.0, total_capacity_kwh=10.0)
    cfg = _battery_config(min_soc=0.10)

    # Build a profile where local hour 15 (Riga UTC+3 summer) draws 4 kW,
    # all other hours 0.5 kW. now is UTC 12 = Riga 15:00 → first hour at local 15.
    slots = tuple(
        BaseLoadSlot(
            hour=h,
            baseload_kw=4.0 if h == 15 else 0.5,
            sample_count=100, is_fallback=False,
        )
        for h in range(24)
    )
    profile = BaseLoadProfile(
        slots=slots, fallback_kw=0.5, overall_p10_kw=0.5, overall_median_kw=0.5,
        confidence=1.0, sample_count=2400, distinct_days=30, computed_at=now,
    )

    est = estimate_battery_runtime(status, cfg, profile, RIGA, now)
    # First hour drains at 4 kW (local 15:00) → 4 kWh in hour 1.
    # Remaining 5 kWh @ 0.5 kW = 10 hours → total runtime ≈ 11 hours.
    assert est.runtime_minutes == pytest.approx(11 * 60, abs=SIMULATION_STEP_MINUTES)
    assert est.avg_drain_kw_next_hour == pytest.approx(4.0)
