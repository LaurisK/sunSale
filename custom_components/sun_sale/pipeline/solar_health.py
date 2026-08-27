"""Detect a physically degrading array — soiling, snow, shading, dead strings.

Why this can work when the forecast cannot
------------------------------------------
The generation forecast currently has no skill at daily-total resolution, so
"observed came in under forecast" says almost nothing. This check does not use
the forecast at all: it compares the array against **its own past self**, via
the clear-sky index. That only requires the error distribution to be stationary,
not centred — so it stays informative however bad the weather model is.

Why the *ceiling*, not the average
----------------------------------
Cloud can only ever push the clear-sky index down, never up. So the maximum CSI
observed across a window estimates what the array is physically capable of,
while the mean mostly measures that window's weather. Comparing a recent
ceiling against an older ceiling therefore isolates hardware change from
meteorology — which is the entire point.

Known limitation: a week in which the sun never once comes out is
indistinguishable from a broken array, and is reported as ``degraded``. That is
a deliberate trade — a week whose *best* moment reaches a small fraction of
capacity warrants a look whatever the cause — but on a genuinely sunless week
the verdict will be a false positive. Ordinary cloudy weather, which still has
brighter spells, is handled correctly by the ceiling comparison.
"""
from __future__ import annotations

from datetime import datetime

from ..contract.models import (
    ArrayCalibration,
    BakedObservedHistory,
    SolarHealth,
)
from .clear_sky import clear_sky_index, clear_sky_power_kw
from .solar_geometry import solar_position

_GENERATION_SIDE_ID = "generation"

# Only high-sun slots are considered: near the horizon, ordinary seasonal
# shading is indistinguishable from a fault.
_HEALTH_MIN_ELEVATION_DEG = 30.0

# A day needs this many high-sun slots before its ceiling means anything.
_MIN_SLOTS_PER_DAY = 4

# Window, in days, treated as "now". Long enough to almost always contain one
# reasonably clear day, short enough to notice a change within a week.
_RECENT_DAYS = 7

# Baseline needs at least this many older days to compare against.
_MIN_BASELINE_DAYS = 7

# Recent ceiling below this fraction of the baseline ceiling is reported as
# degraded. Set well clear of 1.0: the ceiling estimator is noisy, and a false
# "clean your panels" is worse than a slow true positive.
_DEGRADED_RATIO = 0.80

# Percentile of baseline daily ceilings used as the reference. Not the max —
# one freak day would set an unreachable bar forever.
_BASELINE_PERCENTILE = 0.90


def _percentile(values: list[float], fraction: float) -> float:
    """Return the value at a fractional rank of a non-empty list.

    Args:
        values: Values to rank; must not be empty.
        fraction: Rank position in [0, 1].

    Returns:
        The value at that rank (nearest-rank, no interpolation).
    """
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def _daily_ceilings(
    history: BakedObservedHistory,
    calibration: ArrayCalibration,
    latitude: float,
    longitude: float,
) -> dict[str, float]:
    """Return the peak clear-sky index reached on each day with enough high sun.

    Args:
        history: Baked generation history.
        calibration: Fitted array geometry supplying the clear-sky reference.
        latitude: Site latitude in degrees.
        longitude: Site longitude in degrees.

    Returns:
        Mapping of local ISO date → that day's maximum clear-sky index.
    """
    ceilings: dict[str, float] = {}
    for record in history.records:
        if record.side_id != _GENERATION_SIDE_ID:
            continue
        day_indices: list[float] = []
        for slot in record.baked_slots:
            hours = (slot.end - slot.start).total_seconds() / 3600.0
            if hours <= 0:
                continue
            midpoint = slot.start + (slot.end - slot.start) / 2
            position = solar_position(midpoint, latitude, longitude)
            if position.elevation_deg < _HEALTH_MIN_ELEVATION_DEG:
                continue
            reference = clear_sky_power_kw(
                position.elevation_deg, position.azimuth_deg,
                calibration.kwp_eff, calibration.tilt_deg, calibration.azimuth_deg,
            )
            index = clear_sky_index(slot.kwh / hours, reference)
            if index is not None:
                day_indices.append(index)
        if len(day_indices) >= _MIN_SLOTS_PER_DAY:
            ceilings[record.date_str] = max(day_indices)
    return ceilings


def build_solar_health(
    history: BakedObservedHistory | None,
    calibration: ArrayCalibration | None,
    latitude: float | None,
    longitude: float | None,
    now: datetime,
) -> SolarHealth | None:
    """Compare the array's recent achievable output against its own history.

    Args:
        history: Baked generation history.
        calibration: Fitted array geometry; without it there is no clear-sky
            reference and therefore no comparison to make.
        latitude: Site latitude in degrees.
        longitude: Site longitude in degrees.
        now: Cycle timestamp, stamped onto the result.

    Returns:
        A SolarHealth verdict, or ``None`` when the inputs needed to form one
        are absent entirely.
    """
    if history is None or calibration is None:
        return None
    if latitude is None or longitude is None:
        return None

    ceilings = _daily_ceilings(history, calibration, latitude, longitude)
    if not ceilings:
        return SolarHealth(
            status="unknown", recent_ceiling=None, baseline_ceiling=None,
            ratio=None, recent_days=0, baseline_days=0, computed_at=now,
        )

    ordered_dates = sorted(ceilings)
    recent_dates = ordered_dates[-_RECENT_DAYS:]
    baseline_dates = ordered_dates[:-_RECENT_DAYS]

    if len(baseline_dates) < _MIN_BASELINE_DAYS:
        # Not yet enough history to know what "normal" looks like for this
        # array. Reporting "ok" here would be an unearned all-clear.
        return SolarHealth(
            status="unknown",
            recent_ceiling=max(ceilings[d] for d in recent_dates) if recent_dates else None,
            baseline_ceiling=None, ratio=None,
            recent_days=len(recent_dates), baseline_days=len(baseline_dates),
            computed_at=now,
        )

    recent_ceiling = max(ceilings[d] for d in recent_dates)
    baseline_ceiling = _percentile([ceilings[d] for d in baseline_dates], _BASELINE_PERCENTILE)
    if baseline_ceiling <= 0:
        return SolarHealth(
            status="unknown", recent_ceiling=recent_ceiling, baseline_ceiling=None,
            ratio=None, recent_days=len(recent_dates),
            baseline_days=len(baseline_dates), computed_at=now,
        )

    ratio = recent_ceiling / baseline_ceiling
    return SolarHealth(
        status="degraded" if ratio < _DEGRADED_RATIO else "ok",
        recent_ceiling=recent_ceiling,
        baseline_ceiling=baseline_ceiling,
        ratio=ratio,
        recent_days=len(recent_dates),
        baseline_days=len(baseline_dates),
        computed_at=now,
    )
