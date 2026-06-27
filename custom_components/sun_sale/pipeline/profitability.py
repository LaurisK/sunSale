"""Profitability scoring: how attractive is today's peak vs recent history.

Pure Python — no Home Assistant imports, no third-party deps.

Algorithm (see docs in `memory/project_profitability_scoring.md`):

  1. Classify each historical day into WEEKDAY / HOLIDAY / WEEKEND.
  2. Compute per-class median peak across the full history window.
  3. Normalise every daily peak by its class median (adjusted = peak / median).
  4. Score today's adjusted peak as its percentile rank within the most
     recent N days of adjusted peaks.

The module accepts an `is_holiday(date) -> bool` predicate so callers can
plug in the `holidays` package (or any other source) without this module
depending on it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from statistics import median
from typing import Callable, Iterable

from ..contract.models import (
    DailyPeak,
    DayClass,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
)


# Minimum days required in the rolling window before a score is meaningful.
MIN_HISTORY_DAYS = 14

# Days used for the percentile rank (the rolling window).
DEFAULT_RANK_WINDOW_DAYS = 30


def classify_day(
    d: date,
    is_holiday: Callable[[date], bool] | None = None,
) -> DayClass:
    """Return WEEKEND, HOLIDAY, or WEEKDAY for the given date.

    Holiday-on-weekend collapses to WEEKEND (already the cheaper bucket).

    Args:
        d: Date to classify.
        is_holiday: Optional predicate; called only for weekdays.

    Returns:
        DayClass enum value.
    """
    if d.weekday() >= 5:                       # 5 = Sat, 6 = Sun
        return DayClass.WEEKEND
    # `is_holiday` is an intentional, currently-unwired plug point: no caller
    # supplies a predicate today, so the HOLIDAY class is never produced in
    # production. Wire the `holidays` package here (and at the ProfitabilityNode
    # / coordinator call sites) to activate it without touching this module.
    if is_holiday is not None and is_holiday(d):
        return DayClass.HOLIDAY
    return DayClass.WEEKDAY


def compute_class_medians(
    peaks: Iterable[DailyPeak],
) -> dict[DayClass, float]:
    """Compute the median spot peak per day-class across the input window.

    Missing classes fall back to the WEEKDAY median (preferred) or, failing
    that, the overall median. A returned class with value `0.0` would break
    division during normalisation; callers should treat the dict as opaque
    and use `_class_divisor` to read it.

    Args:
        peaks: Historical DailyPeak records (any date range).

    Returns:
        Dict mapping each DayClass to its median peak_eur_kwh; may be partial
        when some classes have no data.
    """
    buckets: dict[DayClass, list[float]] = {c: [] for c in DayClass}
    for p in peaks:
        buckets[p.day_class].append(p.peak_eur_kwh)

    result: dict[DayClass, float] = {}
    for c, values in buckets.items():
        if values:
            result[c] = median(values)
    return result


def _class_divisor(
    medians: dict[DayClass, float],
    cls: DayClass,
    fallback_pool: list[float],
) -> float:
    """Return a non-zero normalisation divisor for the given day-class.

    Priority: class-specific median → WEEKDAY median → overall median of pool.
    Returns 1.0 when no usable value exists (treats data as already normalised).

    Args:
        medians: Per-class medians from compute_class_medians.
        cls: Day-class of the value being normalised.
        fallback_pool: Raw peak values used for the overall-median fallback.

    Returns:
        A positive divisor suitable for normalising a spot peak.
    """
    for candidate in (medians.get(cls), medians.get(DayClass.WEEKDAY)):
        if candidate and candidate > 0:
            return candidate
    if fallback_pool:
        overall = median(fallback_pool)
        if overall > 0:
            return overall
    return 1.0


def percentile_rank(value: float, distribution: list[float]) -> float:
    """Compute the percentile rank of value within distribution, in [0.0, 1.0].

    Uses the "mean rank" convention: ties contribute 0.5 each, so a value
    equal to every entry returns 0.5 (not 0.0 and not 1.0). Empty input
    returns 0.5 — the neutral midpoint.

    Args:
        value: The value to score.
        distribution: Reference sample to rank against.

    Returns:
        Percentile rank in [0.0, 1.0]; 0.5 for empty distribution.
    """
    if not distribution:
        return 0.5
    below = sum(1 for x in distribution if x < value)
    equal = sum(1 for x in distribution if x == value)
    return (below + 0.5 * equal) / len(distribution)


def today_peak_from_price_series(
    price_series: PriceSeries,
    today: date,
    local_tz: tzinfo = timezone.utc,
) -> float | None:
    """Extract the highest Nordpool spot price for the given local date.

    Uses spot (not buy/sell) because the market signal is what we score —
    user-specific tariff fees shouldn't influence the relative ranking.

    Args:
        price_series: PriceSeries containing today's slots. Slot starts are
            stored in UTC, so they are projected into `local_tz` before the
            date comparison.
        today: Local date to filter by.
        local_tz: Timezone used to map each slot's UTC start to a local date,
            so the day boundary is the operator's local midnight rather than
            UTC midnight. Defaults to UTC.

    Returns:
        Maximum spot_eur_kwh for the date, or None if no slots fall on it.
    """
    today_slots = [
        s for s in price_series.slots
        if s.start.astimezone(local_tz).date() == today
    ]
    if not today_slots:
        return None
    return max(s.spot_eur_kwh for s in today_slots)


def compute_profitability_score(
    price_series: PriceSeries,
    history: PriceHistory,
    now: datetime | None = None,
    is_holiday: Callable[[date], bool] | None = None,
    rank_window_days: int = DEFAULT_RANK_WINDOW_DAYS,
    local_tz: tzinfo = timezone.utc,
) -> ProfitabilityScore:
    """Score today's day-class-normalised peak against the rolling history window.

    Args:
        price_series: Current PriceSeries; today's peak is extracted from it.
        history: Rolling history of daily peaks used as the reference distribution.
        now: Cycle timestamp; defaults to UTC now.
        is_holiday: Optional predicate for holiday classification.
        rank_window_days: How many recent history days to rank against.
        local_tz: Timezone used to resolve "today" and to bucket price slots
            by local date, matching how DailyPeak history is keyed. Defaults
            to UTC.

    Returns:
        ProfitabilityScore with a 0–1 percentile score (None when history is sparse).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # "Today" is the operator's local date — history peaks are keyed the same
    # way, so day-class and window-exclusion stay consistent. Using now.date()
    # (UTC) would misclassify the first UTC-offset hours of each local day.
    today = now.astimezone(local_tz).date()
    today_class = classify_day(today, is_holiday)
    today_peak = today_peak_from_price_series(price_series, today, local_tz) or 0.0

    medians = compute_class_medians(history.peaks)
    raw_values = [p.peak_eur_kwh for p in history.peaks]

    # Restrict to the rolling rank window. `history.peaks` is sorted ascending,
    # so we keep the tail. Compare against the most recent days excluding today
    # itself — today's peak is the value being scored.
    window_peaks = [p for p in history.peaks if p.day != today][-rank_window_days:]

    if len(window_peaks) < MIN_HISTORY_DAYS:
        return ProfitabilityScore(
            score=None,
            today_peak_eur_kwh=today_peak,
            today_class=today_class,
            class_medians=medians,
            window_days=len(window_peaks),
            computed_at=now,
        )

    adjusted_distribution = [
        p.peak_eur_kwh / _class_divisor(medians, p.day_class, raw_values)
        for p in window_peaks
    ]
    adjusted_today = today_peak / _class_divisor(medians, today_class, raw_values)
    score = percentile_rank(adjusted_today, adjusted_distribution)

    return ProfitabilityScore(
        score=score,
        today_peak_eur_kwh=today_peak,
        today_class=today_class,
        class_medians=medians,
        window_days=len(window_peaks),
        computed_at=now,
    )


def daily_peak_from_entries(
    day: date,
    entries: Iterable,
    is_holiday: Callable[[date], bool] | None = None,
    local_tz: tzinfo = timezone.utc,
) -> DailyPeak | None:
    """Build a DailyPeak snapshot for a given day from PriceEntry-like objects.

    Convenience for the coordinator when persisting today's peak at day rollover.
    Each entry must expose `.start` (datetime) and `.price_eur_kwh` attributes.

    Args:
        day: The local date to summarise.
        entries: Iterable of price entries (PriceEntry or compatible). Starts
            are stored in UTC and projected into `local_tz` before matching.
        is_holiday: Optional holiday predicate for day classification.
        local_tz: Timezone used to map each entry's UTC start to a local date.
            Defaults to UTC.

    Returns:
        DailyPeak with the maximum spot price, or None if no entries fall on day.
    """
    matching = [
        e.price_eur_kwh for e in entries
        if e.start.astimezone(local_tz).date() == day
    ]
    if not matching:
        return None
    return DailyPeak(
        day=day,
        peak_eur_kwh=max(matching),
        day_class=classify_day(day, is_holiday),
    )
