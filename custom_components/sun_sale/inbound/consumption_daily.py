"""Per-day consumption-bucket finalisation + bootstrap backfill.

Each local day is rolled up exactly once — after the day has ended — into a
``ConsumptionDayRecord`` carrying 24 hour-bucket kWh sums and the per-hour
fraction of expected coordinator cycles that contributed a derived sample.
The rolling window of records feeds ``pipeline.base_load.build_base_load_profile``
which derives the per-hour P15 floor.

Pure Python — no Home Assistant imports.

Two entry points:

  - ``try_finalise_yesterday_consumption`` — called every cycle from the
    coordinator. Writes yesterday's record at most once, then trims.
  - ``backfill_from_derived_history`` — called once on coordinator startup
    when the consumption-daily store is empty (or sparse). Re-aggregates
    any complete local days already present in the rolling
    ``DerivedPowerHistory`` so the baseload profile bootstraps 1–2 days
    earlier than the cold-start path would allow.

Aggregation matches ``inbound/observer/derived._consumption_extract``:
``consumption_kw = max(0, backup + ac_port_signed + grid_net_signed)``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import tzinfo as TzInfo
from typing import Iterable, Sequence

from ..contract.const import (
    CONSUMPTION_DAILY_WINDOW_DAYS,
    UPDATE_INTERVAL_MINUTES,
)
from ..contract.models import (
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
    DerivedPowerHistory,
    DerivedPowerSample,
)


# Expected number of derived samples per hour at the standard coordinator
# cadence. Used to scale completeness — a fully-covered hour reaches 1.0.
EXPECTED_SAMPLES_PER_HOUR = 60 // UPDATE_INTERVAL_MINUTES


def try_finalise_yesterday_consumption(
    derived_history: DerivedPowerHistory,
    existing: ConsumptionDailyBuckets,
    local_tz: TzInfo,
    now: datetime,
    window_days: int = CONSUMPTION_DAILY_WINDOW_DAYS,
) -> ConsumptionDailyBuckets:
    """Append yesterday's hour-bucket rollup; trim records past the window.

    Idempotent per local date: if a record already exists for yesterday,
    only trimming runs. Returns the input ``existing`` object unchanged
    when nothing was added and nothing was trimmed.

    Args:
        derived_history: Rolling derived-power samples persisted by the
            coordinator.
        existing: Current rolling consumption-daily history.
        local_tz: Local timezone defining the day boundary.
        now: Cycle timestamp (UTC).
        window_days: Maximum records to retain after trimming.

    Returns:
        Updated ``ConsumptionDailyBuckets`` with at most one new record
        (yesterday) and old records trimmed.
    """
    yesterday_local = (now.astimezone(local_tz) - timedelta(days=1)).date()
    return try_finalise_dates(
        derived_history=derived_history,
        existing=existing,
        local_tz=local_tz,
        now=now,
        target_dates=(yesterday_local,),
        window_days=window_days,
    )


def backfill_from_derived_history(
    derived_history: DerivedPowerHistory,
    existing: ConsumptionDailyBuckets,
    local_tz: TzInfo,
    now: datetime,
    window_days: int = CONSUMPTION_DAILY_WINDOW_DAYS,
) -> ConsumptionDailyBuckets:
    """Seed the consumption-daily store from any complete local days in derived history.

    Identifies the distinct local dates spanned by derived samples and
    finalises every date strictly older than today (today is still in
    progress and is left for the regular per-cycle hook). Idempotent —
    records already present are skipped. Safe to invoke on every restart.

    Args:
        derived_history: Rolling derived-power samples persisted by the
            coordinator. Typically holds ~2 days at the standard retention.
        existing: Current rolling consumption-daily history.
        local_tz: Local timezone defining the day boundary.
        now: Coordinator-startup timestamp (UTC).
        window_days: Maximum records to retain after trimming.

    Returns:
        Updated ``ConsumptionDailyBuckets`` seeded with backfilled records.
    """
    if not derived_history.samples:
        return _trim(existing, now=now, local_tz=local_tz, window_days=window_days)

    today_local = now.astimezone(local_tz).date()
    candidate_dates = {
        s.timestamp.astimezone(local_tz).date()
        for s in derived_history.samples
    }
    targets = sorted(d for d in candidate_dates if d < today_local)
    return try_finalise_dates(
        derived_history=derived_history,
        existing=existing,
        local_tz=local_tz,
        now=now,
        target_dates=targets,
        window_days=window_days,
    )


def try_finalise_dates(
    derived_history: DerivedPowerHistory,
    existing: ConsumptionDailyBuckets,
    local_tz: TzInfo,
    now: datetime,
    target_dates: Iterable[date],
    window_days: int = CONSUMPTION_DAILY_WINDOW_DAYS,
) -> ConsumptionDailyBuckets:
    """Finalise each target local date (idempotent) and trim to the window.

    Iterates ``target_dates`` in input order; for each date with no
    existing record, aggregates the derived samples falling on that local
    date and appends a new ``ConsumptionDayRecord``. Dates with no samples
    produce no record (they cannot be backfilled later either — the
    derived history retention is short).

    Args:
        derived_history: Rolling derived-power samples.
        existing: Current consumption-daily history.
        local_tz: Local timezone.
        now: Cycle timestamp (UTC) — used as ``finalised_at`` on new records.
        target_dates: Local dates to consider for finalisation.
        window_days: Maximum records to retain after trimming.

    Returns:
        Updated ``ConsumptionDailyBuckets``.
    """
    existing_dates = {r.local_date for r in existing.records}
    new_records: list[ConsumptionDayRecord] = list(existing.records)
    added = False

    for d in target_dates:
        if d in existing_dates:
            continue
        record = _aggregate_day_from_derived(
            samples=derived_history.samples,
            target_date_local=d,
            local_tz=local_tz,
            finalised_at=now,
        )
        if record is None:
            continue
        new_records.append(record)
        existing_dates.add(d)
        added = True

    if not added:
        return _trim(
            existing, now=now, local_tz=local_tz, window_days=window_days,
        )

    new_records.sort(key=lambda r: r.local_date)
    return _trim(
        ConsumptionDailyBuckets(records=tuple(new_records)),
        now=now,
        local_tz=local_tz,
        window_days=window_days,
    )


def _aggregate_day_from_derived(
    samples: Sequence[DerivedPowerSample],
    target_date_local: date,
    local_tz: TzInfo,
    finalised_at: datetime,
) -> ConsumptionDayRecord | None:
    """Bucket derived samples by local hour for one local date.

    Each sample contributes its consumption_kw to the hour-bucket of its
    local timestamp. The hour's kWh is the mean of contributing samples
    times one hour (the bucket duration). Completeness is the share of
    expected coordinator cycles per hour that actually contributed.

    Returns ``None`` when no sample falls on ``target_date_local`` — the
    caller will not append a record in that case so the rolling window
    stays an accurate count of finalised days.

    Args:
        samples: Derived-power samples (any order).
        target_date_local: Local date to aggregate.
        local_tz: Local timezone for date / hour dispatch.
        finalised_at: UTC timestamp stored on the returned record.

    Returns:
        ConsumptionDayRecord, or None when no relevant samples exist.
    """
    per_hour_powers: list[list[float]] = [[] for _ in range(24)]
    any_sample = False
    for s in samples:
        local_t = s.timestamp.astimezone(local_tz)
        if local_t.date() != target_date_local:
            continue
        kw = max(
            0.0,
            s.backup_kw + s.ac_port_kw_signed + s.grid_net_kw_signed,
        )
        per_hour_powers[local_t.hour].append(kw)
        any_sample = True

    if not any_sample:
        return None

    hour_kwh: list[float] = []
    hour_completeness: list[float] = []
    for h in range(24):
        powers = per_hour_powers[h]
        n = len(powers)
        if n == 0:
            hour_kwh.append(0.0)
            hour_completeness.append(0.0)
            continue
        mean_kw = sum(powers) / n
        hour_kwh.append(round(mean_kw, 6))
        hour_completeness.append(round(min(1.0, n / EXPECTED_SAMPLES_PER_HOUR), 4))

    return ConsumptionDayRecord(
        local_date=target_date_local,
        hour_kwh=tuple(hour_kwh),
        hour_completeness=tuple(hour_completeness),
        finalised_at=finalised_at,
    )


def _trim(
    buckets: ConsumptionDailyBuckets,
    now: datetime,
    local_tz: TzInfo,
    window_days: int,
) -> ConsumptionDailyBuckets:
    """Drop records whose local_date lies outside the rolling window.

    Args:
        buckets: Current rolling history.
        now: Cycle timestamp (UTC).
        local_tz: Local timezone.
        window_days: Window length in days; only records with
            ``local_date > (today_local - window_days)`` are retained.

    Returns:
        Trimmed history. Returns the input when nothing changed.
    """
    if not buckets.records:
        return buckets
    today_local = now.astimezone(local_tz).date()
    cutoff = today_local - timedelta(days=window_days)
    kept = tuple(r for r in buckets.records if r.local_date > cutoff)
    if len(kept) == len(buckets.records):
        return buckets
    return ConsumptionDailyBuckets(records=kept)
