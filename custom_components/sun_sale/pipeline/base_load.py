"""Base load estimation + battery-runtime forecast.

Pure Python — no Home Assistant imports, no third-party deps.

Two public functions:

  - `build_base_load_profile(buckets, local_tz, ...)` — per local hour-of-day
    P15 floor across a rolling window of finalised daily hour-bucket rollups
    (one ``ConsumptionDayRecord`` per local date, populated by
    ``inbound/consumption_daily.try_finalise_yesterday_consumption``).
  - `estimate_battery_runtime(status, config, profile, ...)` —
    forward-simulate **pure baseload drain** from `now` and return the time
    until the battery hits `min_soc`. Solar generation and scheduled
    actions are intentionally NOT modelled: the output is a worst-case
    "household-only depletion" reserve, comparable cycle-to-cycle.

Design decisions:

  - **P15 over per-day hour-bucket sums**: each day contributes one value
    per hour bucket (the day's mean consumption_kw × 1 hour). P15 across
    20–30 days picks roughly the 3rd-quietest day, rejecting EV / oven /
    laundry spikes while staying robust against a single anomalously quiet
    day. Higher percentile than the classic P10 to avoid being anchored to
    one outlier early in the rolling window.
  - **Per-hour completeness gate**: a day's contribution to a particular
    hour bucket is dropped when fewer than 80% of the expected coordinator
    cycles in that hour produced a derived sample. Prevents inverter
    outages from artificially depressing the floor.
  - All bucket keys are computed in local time; persistent records key on
    ``date`` so DST and timezone changes are unambiguous.
  - Runtime estimate ignores forecast solar and the schedule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterable

from ..contract.const import CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS
from ..contract.models import (
    BaseLoadProfile,
    BaseLoadSlot,
    BatteryConfig,
    BatteryRuntimeEstimate,
    BatteryStatus,
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
)


# Below this many qualified days in the window, the profile is considered
# too sparse to bucket: confidence=None, every slot uses fallback_kw.
MIN_HISTORY_DAYS = 7

# Buckets with fewer qualified-day contributions than this fall back to the
# cross-bucket fallback. Lower than the legacy per-cycle threshold because
# each contribution here is already a higher-quality whole-hour aggregate.
MIN_BUCKET_DAYS = 5

# Floor percentile per bucket. P15 instead of the historical P10 to reduce
# the single-quiet-day anchor effect — the window can be as small as 7 days
# during bootstrap, and P10 of 7 collapses to "min of 7" which is volatile.
DEFAULT_PERCENTILE = 0.15

# Rolling window of finalised days considered. Aligned with
# ``CONSUMPTION_DAILY_WINDOW_DAYS`` in const.py; passed explicitly to keep
# this module free of any storage-layer coupling.
DEFAULT_WINDOW_DAYS = 30

# Cross-bucket fallback percentile — wider than the per-bucket P15 since the
# sample pool spans every hour (less signal per hour kept).
DEFAULT_FALLBACK_PERCENTILE = 0.20

# Last-resort default when there is no qualified history at all (fresh install).
# Matches `inbound/battery._DEFAULT_HOUSEHOLD_LOAD_KW`.
DEFAULT_STUB_KW = 0.2

# How far ahead the runtime estimate simulates.
DEFAULT_HORIZON_HOURS = 48

# Simulation step. 5 min matches the coordinator update interval and gives
# sub-percent precision on the "until" timestamp.
SIMULATION_STEP_MINUTES = 5


# ---------------------------------------------------------------------------
# Profile construction
# ---------------------------------------------------------------------------

def build_base_load_profile(
    buckets: ConsumptionDailyBuckets,
    local_tz: tzinfo,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    percentile: float = DEFAULT_PERCENTILE,
    min_hour_completeness: float = CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS,
) -> BaseLoadProfile:
    """Build a 24-bucket (local hour) P15-floor baseload profile from daily rollups.

    Each input ``ConsumptionDayRecord`` contributes its 24 ``hour_kwh``
    values to the corresponding hour buckets, gated by per-hour
    completeness. The per-hour ``baseload_kw`` is the configured
    percentile (default P15) across the qualified contributions; bucket
    duration is 1 hour so ``kwh == kw`` per slot.

    Args:
        buckets: Rolling history of finalised per-day hour-bucket rollups.
        local_tz: Local timezone used to define the rolling window cutoff
            (records with ``local_date <= today_local - window_days`` are
            dropped from the input). Reserved for symmetry with
            ``estimate_battery_runtime``.
        now: Reference time for the rolling window; defaults to UTC now.
        window_days: How many recent finalised days to include.
        percentile: Per-bucket percentile (default 0.15). 0 ≤ p ≤ 1.
        min_hour_completeness: Per-day per-hour minimum fraction of expected
            samples (0..1) for that day's contribution to qualify.

    Returns:
        BaseLoadProfile with 24 slots; uses fallback_kw for sparse buckets
        and `confidence=None` when total qualified days fall below
        ``MIN_HISTORY_DAYS``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    window_records = _records_in_window(buckets.records, now, local_tz, window_days)

    sample_count = sum(
        1
        for r in window_records
        for c in r.hour_completeness
        if c >= min_hour_completeness
    )
    distinct_days = len(window_records)

    qualified_per_hour = _qualified_values_per_hour(
        window_records, min_hour_completeness,
    )
    all_qualified_values = [
        v for hour_values in qualified_per_hour for v in hour_values
    ]

    overall_p10 = (
        _percentile(all_qualified_values, 0.10)
        if all_qualified_values
        else DEFAULT_STUB_KW
    )
    overall_median = (
        _percentile(all_qualified_values, 0.50)
        if all_qualified_values
        else DEFAULT_STUB_KW
    )

    if distinct_days < MIN_HISTORY_DAYS:
        fallback = (
            _percentile(all_qualified_values, DEFAULT_FALLBACK_PERCENTILE)
            if all_qualified_values
            else DEFAULT_STUB_KW
        )
        slots = tuple(
            BaseLoadSlot(
                hour=h,
                baseload_kw=fallback,
                sample_count=0,
                is_fallback=True,
            )
            for h in range(24)
        )
        return BaseLoadProfile(
            slots=slots,
            fallback_kw=fallback,
            overall_p10_kw=overall_p10,
            overall_median_kw=overall_median,
            confidence=None,
            sample_count=sample_count,
            distinct_days=distinct_days,
            computed_at=now,
        )

    fallback_kw = _percentile(all_qualified_values, DEFAULT_FALLBACK_PERCENTILE)

    slots_list: list[BaseLoadSlot] = []
    for h in range(24):
        values = qualified_per_hour[h]
        if len(values) < MIN_BUCKET_DAYS:
            slots_list.append(BaseLoadSlot(
                hour=h,
                baseload_kw=fallback_kw,
                sample_count=len(values),
                is_fallback=True,
            ))
        else:
            slots_list.append(BaseLoadSlot(
                hour=h,
                baseload_kw=_percentile(values, percentile),
                sample_count=len(values),
                is_fallback=False,
            ))

    return BaseLoadProfile(
        slots=tuple(slots_list),
        fallback_kw=fallback_kw,
        overall_p10_kw=overall_p10,
        overall_median_kw=overall_median,
        confidence=min(1.0, distinct_days / float(window_days)),
        sample_count=sample_count,
        distinct_days=distinct_days,
        computed_at=now,
    )


def _records_in_window(
    records: Iterable[ConsumptionDayRecord],
    now: datetime,
    local_tz: tzinfo,
    window_days: int,
) -> list[ConsumptionDayRecord]:
    """Filter records to those whose local_date lies within the rolling window.

    Args:
        records: Persisted day records.
        now: Window reference (UTC).
        local_tz: Local timezone defining "today".
        window_days: Window length in days.

    Returns:
        List of records with ``local_date > (today_local - window_days)``.
    """
    today_local = now.astimezone(local_tz).date()
    cutoff = today_local - timedelta(days=window_days)
    return [r for r in records if r.local_date > cutoff]


def _qualified_values_per_hour(
    records: Iterable[ConsumptionDayRecord],
    min_hour_completeness: float,
) -> list[list[float]]:
    """Collect per-hour kWh contributions that clear the completeness gate.

    Args:
        records: Day records inside the rolling window.
        min_hour_completeness: Minimum per-hour completeness for the
            record's contribution to count.

    Returns:
        Length-24 list. Element ``h`` is the list of qualified ``hour_kwh[h]``
        values across the input records, in input order.
    """
    per_hour: list[list[float]] = [[] for _ in range(24)]
    for r in records:
        for h in range(24):
            if r.hour_completeness[h] >= min_hour_completeness:
                per_hour[h].append(r.hour_kwh[h])
    return per_hour


# ---------------------------------------------------------------------------
# Runtime estimation
# ---------------------------------------------------------------------------

def estimate_battery_runtime(
    battery_status: BatteryStatus,
    battery_config: BatteryConfig,
    profile: BaseLoadProfile,
    local_tz: tzinfo,
    now: datetime,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> BatteryRuntimeEstimate:
    """Forward-simulate pure baseload drain to estimate how long the battery lasts.

    Steps through SIMULATION_STEP_MINUTES intervals, subtracting the per-hour
    baseload at each step. Solar and scheduled charge/discharge are intentionally
    ignored — this is a worst-case "household-only" reserve estimate.

    Args:
        battery_status: Live SoC and configured total capacity.
        battery_config: Battery limits (min_soc used as the depletion threshold).
        profile: 24-bucket baseload profile supplying per-hour drain rates.
        local_tz: Timezone for mapping simulation steps to local hours.
        now: Start of the simulation.
        horizon_hours: How far ahead to simulate before giving up.

    Returns:
        BatteryRuntimeEstimate with runtime_minutes/until set to None when the
        battery does not deplete within the horizon.
    """
    usable_kwh = max(
        0.0,
        (battery_status.soc - battery_config.min_soc) * battery_status.total_capacity_kwh,
    )

    step = timedelta(minutes=SIMULATION_STEP_MINUTES)
    step_hours = SIMULATION_STEP_MINUTES / 60.0
    horizon_end = now + timedelta(hours=horizon_hours)
    one_hour_after_now = now + timedelta(hours=1)

    drain_kw_first_hour: list[float] = []
    remaining = usable_kwh
    elapsed_minutes = 0.0
    t = now

    while t < horizon_end:
        drain_kw = profile.at(t, local_tz)

        if t < one_hour_after_now:
            drain_kw_first_hour.append(drain_kw)

        drain_kwh = drain_kw * step_hours

        if remaining <= 0:
            return BatteryRuntimeEstimate(
                remaining_kwh_usable=usable_kwh,
                avg_drain_kw_next_hour=_mean(drain_kw_first_hour),
                runtime_minutes=elapsed_minutes,
                until=t,
                horizon_hours=horizon_hours,
                computed_at=now,
            )

        if drain_kwh > 0 and drain_kwh >= remaining:
            fraction = remaining / drain_kwh
            partial = SIMULATION_STEP_MINUTES * fraction
            return BatteryRuntimeEstimate(
                remaining_kwh_usable=usable_kwh,
                avg_drain_kw_next_hour=_mean(drain_kw_first_hour),
                runtime_minutes=elapsed_minutes + partial,
                until=t + timedelta(minutes=partial),
                horizon_hours=horizon_hours,
                computed_at=now,
            )

        remaining -= drain_kwh
        elapsed_minutes += SIMULATION_STEP_MINUTES
        t += step

    return BatteryRuntimeEstimate(
        remaining_kwh_usable=usable_kwh,
        avg_drain_kw_next_hour=_mean(drain_kw_first_hour),
        runtime_minutes=None,
        until=None,
        horizon_hours=horizon_hours,
        computed_at=now,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(values: list[float], p: float) -> float:
    """Compute a linear-interpolation percentile.

    Args:
        values: Non-empty list of floats (unsorted is fine).
        p: Percentile in [0, 1].

    Returns:
        Interpolated percentile value, or 0.0 for empty input.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_v = sorted(values)
    n = len(sorted_v)
    rank = p * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac


def _mean(values: Iterable[float]) -> float:
    """Return the arithmetic mean of values, or 0.0 for empty input.

    Args:
        values: Iterable of floats.

    Returns:
        Mean value, or 0.0 when values is empty.
    """
    seq = list(values)
    return sum(seq) / len(seq) if seq else 0.0
