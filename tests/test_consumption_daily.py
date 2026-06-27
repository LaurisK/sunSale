"""Tests for inbound/consumption_daily.py — pure Python, no HA required."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import (
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
    DerivedPowerHistory,
    DerivedPowerSample,
)
from custom_components.sun_sale.inbound.consumption_daily import (
    EXPECTED_SAMPLES_PER_HOUR,
    backfill_from_derived_history,
    try_finalise_dates,
    try_finalise_yesterday_consumption,
)


UTC = timezone.utc
RIGA = ZoneInfo("Europe/Riga")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _sample(
    ts: datetime,
    ac_port: float = 0.0,
    backup: float = 0.0,
    grid_net: float = 0.0,
    solar: float = 0.0,
    battery: float = 0.0,
) -> DerivedPowerSample:
    """Build a DerivedPowerSample with named kW components.

    The consumption formula is ``max(0, backup + ac_port + grid_net)``; the
    other fields don't affect finalisation but must be set on the record.
    """
    return DerivedPowerSample(
        timestamp=ts,
        ac_port_kw_signed=ac_port,
        backup_kw=backup,
        grid_net_kw_signed=grid_net,
        solar_kw=solar,
        battery_kw_signed=battery,
    )


def _samples_on_local_day(
    local_d: date,
    *,
    consumption_kw_fn,
    samples_per_hour: int = EXPECTED_SAMPLES_PER_HOUR,
    local_tz=RIGA,
) -> list[DerivedPowerSample]:
    """Generate samples evenly spaced across one local date.

    ``consumption_kw_fn(local_hour, idx_in_hour) → kW`` decides the
    consumption value (composed into the formula via ``grid_net`` for
    simplicity). The result spans ``samples_per_hour`` samples per local
    hour, with timestamps at integer-minute boundaries.
    """
    out = []
    step_minutes = 60 // samples_per_hour
    local_midnight = datetime(local_d.year, local_d.month, local_d.day, tzinfo=local_tz)
    for h in range(24):
        for i in range(samples_per_hour):
            ts_local = local_midnight + timedelta(hours=h, minutes=i * step_minutes)
            out.append(_sample(
                ts=ts_local.astimezone(UTC),
                grid_net=consumption_kw_fn(h, i),
            ))
    return out


# ---------------------------------------------------------------------------
# try_finalise_yesterday_consumption
# ---------------------------------------------------------------------------

def test_finalise_writes_yesterday_record():
    """A full day of derived samples produces one ConsumptionDayRecord at full coverage."""
    yesterday_local = date(2026, 5, 15)
    now = datetime(2026, 5, 16, 10, tzinfo=UTC)
    samples = _samples_on_local_day(
        yesterday_local, consumption_kw_fn=lambda h, i: 0.3,
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = try_finalise_yesterday_consumption(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )

    assert len(updated.records) == 1
    record = updated.records[0]
    assert record.local_date == yesterday_local
    assert all(v == pytest.approx(0.3) for v in record.hour_kwh)
    assert all(c == pytest.approx(1.0) for c in record.hour_completeness)
    assert record.finalised_at == now


def test_finalise_is_idempotent():
    """A second call on the same cycle returns the same store object."""
    yesterday_local = date(2026, 5, 15)
    now = datetime(2026, 5, 16, 10, tzinfo=UTC)
    samples = _samples_on_local_day(
        yesterday_local, consumption_kw_fn=lambda h, i: 0.4,
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    once = try_finalise_yesterday_consumption(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )
    twice = try_finalise_yesterday_consumption(
        derived_history=history, existing=once, local_tz=RIGA, now=now,
    )

    assert twice is once


def test_finalise_skips_when_no_samples_for_yesterday():
    """No samples in yesterday's local window → no record appended."""
    now = datetime(2026, 5, 16, 10, tzinfo=UTC)
    # All samples land in today's local date.
    today_local = date(2026, 5, 16)
    samples = _samples_on_local_day(
        today_local, consumption_kw_fn=lambda h, i: 0.5,
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = try_finalise_yesterday_consumption(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )

    assert updated.records == ()


def test_finalise_consumption_formula_matches_derived_extract():
    """Per-sample consumption = max(0, backup + ac_port_signed + grid_net_signed)."""
    yesterday_local = date(2026, 5, 15)
    now = datetime(2026, 5, 16, 10, tzinfo=UTC)
    # One sample at local 12:00 splits the load across the three sign axes.
    local_noon = datetime(2026, 5, 15, 12, tzinfo=RIGA).astimezone(UTC)
    samples = [
        _sample(ts=local_noon, ac_port=0.2, backup=0.1, grid_net=0.5),
    ]
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = try_finalise_yesterday_consumption(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )

    record = updated.records[0]
    # Only the noon hour has a sample.
    assert record.hour_kwh[12] == pytest.approx(0.8)
    # 1 sample / EXPECTED_SAMPLES_PER_HOUR = 1/12 ≈ 0.0833.
    assert record.hour_completeness[12] == pytest.approx(
        1.0 / EXPECTED_SAMPLES_PER_HOUR, rel=1e-3,
    )
    # Every other hour is empty.
    for h in range(24):
        if h == 12:
            continue
        assert record.hour_kwh[h] == 0.0
        assert record.hour_completeness[h] == 0.0


def test_finalise_clamps_negative_consumption_to_zero():
    """A sample with net negative inputs (cross-cycle sensor mismatch) yields 0 kW."""
    yesterday_local = date(2026, 5, 15)
    now = datetime(2026, 5, 16, 10, tzinfo=UTC)
    local_noon = datetime(2026, 5, 15, 12, tzinfo=RIGA).astimezone(UTC)
    samples = [
        _sample(ts=local_noon, ac_port=-1.0, backup=0.0, grid_net=-0.5),
    ]
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = try_finalise_yesterday_consumption(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )

    assert updated.records[0].hour_kwh[12] == 0.0


def test_finalise_trims_to_window():
    """Records outside ``window_days`` get dropped on the next finalise."""
    yesterday_local = date(2026, 5, 15)
    now = datetime(2026, 5, 16, 10, tzinfo=UTC)
    old_record = ConsumptionDayRecord(
        local_date=date(2026, 3, 1),
        hour_kwh=tuple([0.1] * 24),
        hour_completeness=tuple([1.0] * 24),
        finalised_at=datetime(2026, 3, 2, tzinfo=UTC),
    )
    samples = _samples_on_local_day(
        yesterday_local, consumption_kw_fn=lambda h, i: 0.2,
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = try_finalise_yesterday_consumption(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=(old_record,)),
        local_tz=RIGA,
        now=now,
        window_days=30,
    )

    assert old_record not in updated.records
    assert len(updated.records) == 1
    assert updated.records[0].local_date == yesterday_local


# ---------------------------------------------------------------------------
# backfill_from_derived_history
# ---------------------------------------------------------------------------

def test_backfill_seeds_complete_local_days():
    """Backfill produces records for every complete past local day in derived history."""
    now = datetime(2026, 5, 16, 14, tzinfo=UTC)
    # Two complete past days plus partial today.
    samples = (
        _samples_on_local_day(date(2026, 5, 14), consumption_kw_fn=lambda h, i: 0.3)
        + _samples_on_local_day(date(2026, 5, 15), consumption_kw_fn=lambda h, i: 0.4)
        + _samples_on_local_day(date(2026, 5, 16), consumption_kw_fn=lambda h, i: 0.5)
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = backfill_from_derived_history(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )

    dates = sorted(r.local_date for r in updated.records)
    assert dates == [date(2026, 5, 14), date(2026, 5, 15)]
    # Today (2026-05-16) is NOT backfilled.
    assert all(r.local_date < date(2026, 5, 16) for r in updated.records)


def test_backfill_is_idempotent_with_existing_records():
    """Backfill doesn't overwrite or duplicate already-finalised dates."""
    now = datetime(2026, 5, 16, 14, tzinfo=UTC)
    existing_record = ConsumptionDayRecord(
        local_date=date(2026, 5, 14),
        hour_kwh=tuple([0.999] * 24),    # sentinel — must not be overwritten
        hour_completeness=tuple([1.0] * 24),
        finalised_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    samples = _samples_on_local_day(
        date(2026, 5, 14), consumption_kw_fn=lambda h, i: 0.3,
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = backfill_from_derived_history(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=(existing_record,)),
        local_tz=RIGA,
        now=now,
    )

    assert len(updated.records) == 1
    assert updated.records[0] is existing_record


def test_backfill_with_empty_history():
    """No samples → no records; trimming runs but nothing to drop."""
    now = datetime(2026, 5, 16, 14, tzinfo=UTC)
    updated = backfill_from_derived_history(
        derived_history=DerivedPowerHistory(samples=()),
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
    )
    assert updated.records == ()


# ---------------------------------------------------------------------------
# try_finalise_dates — explicit target dates
# ---------------------------------------------------------------------------

def test_finalise_dates_orders_output_by_local_date():
    """Mixed-order targets produce records sorted by local_date."""
    now = datetime(2026, 5, 16, 14, tzinfo=UTC)
    samples = (
        _samples_on_local_day(date(2026, 5, 12), consumption_kw_fn=lambda h, i: 0.2)
        + _samples_on_local_day(date(2026, 5, 14), consumption_kw_fn=lambda h, i: 0.3)
    )
    history = DerivedPowerHistory(samples=tuple(samples))

    updated = try_finalise_dates(
        derived_history=history,
        existing=ConsumptionDailyBuckets(records=()),
        local_tz=RIGA,
        now=now,
        target_dates=[date(2026, 5, 14), date(2026, 5, 12)],
    )

    assert [r.local_date for r in updated.records] == [
        date(2026, 5, 12), date(2026, 5, 14),
    ]
