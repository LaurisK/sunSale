"""Tests for pipeline/profitability.py — pure Python, no HA mocking needed."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import (
    DailyPeak,
    DayClass,
    PriceEntry,
    PriceHistory,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline.profitability import (
    MIN_HISTORY_DAYS,
    classify_day,
    compute_class_medians,
    compute_profitability_score,
    daily_peak_from_entries,
    percentile_rank,
    today_peak_from_price_series,
)
from tests.conftest import default_tariff_config


def _peak(day: date, value: float, cls: DayClass | None = None) -> DailyPeak:
    """Build a DailyPeak; class defaults to weekday-vs-weekend (no holidays)."""
    if cls is None:
        cls = classify_day(day)
    return DailyPeak(day=day, peak_eur_kwh=value, day_class=cls)


def _history(peaks: list[DailyPeak]) -> PriceHistory:
    return PriceHistory(peaks=tuple(sorted(peaks, key=lambda p: p.day)))


def _price_series_for_day(d: date, peak_at_hour: int, peak_value: float):
    """Build a 24h PriceSeries on `d` with a single peak hour."""
    base = datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc)
    entries = [
        PriceEntry(
            start=base + timedelta(hours=h),
            end=base + timedelta(hours=h + 1),
            price_eur_kwh=peak_value if h == peak_at_hour else 0.05,
        )
        for h in range(24)
    ]
    return build_price_series(entries, default_tariff_config(), now=base)


# ---------------------------------------------------------------------------
# classify_day
# ---------------------------------------------------------------------------

def test_classify_weekday():
    # 2024-01-15 is a Monday
    assert classify_day(date(2024, 1, 15)) == DayClass.WEEKDAY


def test_classify_weekend_saturday():
    # 2024-01-13 is a Saturday
    assert classify_day(date(2024, 1, 13)) == DayClass.WEEKEND


def test_classify_weekend_sunday():
    # 2024-01-14 is a Sunday
    assert classify_day(date(2024, 1, 14)) == DayClass.WEEKEND


def test_classify_holiday_on_weekday():
    # Monday flagged as holiday
    monday = date(2024, 1, 15)
    assert classify_day(monday, is_holiday=lambda d: d == monday) == DayClass.HOLIDAY


def test_holiday_on_weekend_collapses_to_weekend():
    saturday = date(2024, 1, 13)
    assert classify_day(saturday, is_holiday=lambda d: True) == DayClass.WEEKEND


# ---------------------------------------------------------------------------
# percentile_rank
# ---------------------------------------------------------------------------

def test_percentile_rank_empty_returns_midpoint():
    assert percentile_rank(0.5, []) == 0.5


def test_percentile_rank_max_value():
    assert percentile_rank(10.0, [1.0, 2.0, 3.0]) == 1.0


def test_percentile_rank_min_value():
    assert percentile_rank(0.0, [1.0, 2.0, 3.0]) == 0.0


def test_percentile_rank_ties_share_mass():
    # value equals all entries → 0.5 (neutral midpoint)
    assert percentile_rank(2.0, [2.0, 2.0, 2.0]) == 0.5


def test_percentile_rank_median_position():
    # 5 entries, value strictly greater than 2 of them → 2/5 = 0.4
    assert percentile_rank(2.5, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# compute_class_medians
# ---------------------------------------------------------------------------

def test_class_medians_separate_buckets():
    peaks = [
        _peak(date(2024, 1, 15), 0.20),  # Mon — weekday
        _peak(date(2024, 1, 16), 0.22),  # Tue — weekday
        _peak(date(2024, 1, 17), 0.18),  # Wed — weekday
        _peak(date(2024, 1, 13), 0.10),  # Sat — weekend
        _peak(date(2024, 1, 14), 0.12),  # Sun — weekend
    ]
    medians = compute_class_medians(peaks)
    assert medians[DayClass.WEEKDAY] == pytest.approx(0.20)
    assert medians[DayClass.WEEKEND] == pytest.approx(0.11)
    # No holidays in input → not in dict
    assert DayClass.HOLIDAY not in medians


def test_class_medians_empty_input():
    assert compute_class_medians([]) == {}


# ---------------------------------------------------------------------------
# today_peak_from_price_series
# ---------------------------------------------------------------------------

def test_today_peak_picks_max_spot_on_date():
    today = date(2024, 1, 15)
    series = _price_series_for_day(today, peak_at_hour=18, peak_value=0.35)
    assert today_peak_from_price_series(series, today) == pytest.approx(0.35)


def test_today_peak_returns_none_when_no_slots_today():
    today = date(2024, 1, 15)
    other = date(2024, 1, 20)
    series = _price_series_for_day(other, peak_at_hour=18, peak_value=0.35)
    assert today_peak_from_price_series(series, today) is None


def test_today_peak_buckets_slots_by_local_date():
    """A peak in the local early-morning (UTC previous day) counts as today.

    Slot starts are stored in UTC. Europe/Riga is UTC+2 in January, so the
    local-Saturday window 00:00–23:59 spans UTC Fri 22:00 → Sat 21:59. A peak
    at local 01:00 Sat (UTC Fri 23:00) must be picked up when filtering for
    the local Saturday — UTC bucketing would drop it into Friday.
    """
    riga = ZoneInfo("Europe/Riga")
    saturday = date(2024, 1, 13)
    # UTC start of local Saturday 00:00 in Riga (UTC+2) is Fri 22:00.
    base_utc = datetime(2024, 1, 12, 22, 0, tzinfo=timezone.utc)
    entries = [
        PriceEntry(
            start=base_utc + timedelta(hours=h),
            end=base_utc + timedelta(hours=h + 1),
            price_eur_kwh=0.50 if h == 1 else 0.05,   # peak at local 01:00 Sat
        )
        for h in range(24)
    ]
    series = build_price_series(entries, default_tariff_config())
    assert today_peak_from_price_series(series, saturday, riga) == pytest.approx(0.50)
    # Default UTC bucketing assigns the peak slot to Friday → excluded, so the
    # UTC-Saturday max is only the 0.05 baseline. Demonstrates the old bug path.
    assert today_peak_from_price_series(series, saturday) == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# compute_profitability_score
# ---------------------------------------------------------------------------

def _build_30d_weekday_only_history(end_day: date, peak_value: float = 0.20):
    """Build 30 consecutive weekdays of history (skipping weekends).

    Caller passes the desired end day; days walked back until 30 weekday peaks
    are accumulated. All peaks identical → percentile rank of the same value
    sits at 0.5 (neutral).
    """
    peaks: list[DailyPeak] = []
    d = end_day
    while len(peaks) < 30:
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            peaks.append(_peak(d, peak_value))
    return _history(peaks)


def test_score_sparse_history_returns_none():
    today = date(2024, 1, 15)
    now = datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)
    history = _history([_peak(today - timedelta(days=i), 0.20) for i in range(1, 5)])
    series = _price_series_for_day(today, peak_at_hour=18, peak_value=0.30)
    result = compute_profitability_score(series, history, now=now)
    assert result.score is None
    assert result.window_days == 4
    assert result.today_class == DayClass.WEEKDAY
    assert result.today_peak_eur_kwh == pytest.approx(0.30)


def test_score_today_far_above_history():
    today = date(2024, 1, 15)  # Monday
    now = datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)
    history = _build_30d_weekday_only_history(today, peak_value=0.20)
    series = _price_series_for_day(today, peak_at_hour=18, peak_value=10.0)
    result = compute_profitability_score(series, history, now=now)
    assert result.score == 1.0


def test_score_today_far_below_history():
    today = date(2024, 1, 15)
    now = datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)
    history = _build_30d_weekday_only_history(today, peak_value=0.20)
    series = _price_series_for_day(today, peak_at_hour=18, peak_value=0.001)
    result = compute_profitability_score(series, history, now=now)
    assert result.score == 0.0


def test_score_today_equals_history_is_midpoint():
    today = date(2024, 1, 15)
    now = datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)
    history = _build_30d_weekday_only_history(today, peak_value=0.20)
    series = _price_series_for_day(today, peak_at_hour=18, peak_value=0.20)
    result = compute_profitability_score(series, history, now=now)
    assert result.score == 0.5


def test_score_excludes_today_from_distribution():
    today = date(2024, 1, 15)
    now = datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)
    history = _build_30d_weekday_only_history(today, peak_value=0.20)
    # Inject a today-dated outlier; it must NOT be part of the distribution
    spiked = _history(list(history.peaks) + [_peak(today, 999.0)])
    series = _price_series_for_day(today, peak_at_hour=18, peak_value=0.20)
    result = compute_profitability_score(series, spiked, now=now)
    # If today were included, the outlier would skew the rank away from 0.5.
    assert result.score == 0.5


def test_score_normalises_by_day_class():
    """A Saturday peak that's high *for a Saturday* should score high.

    Setup: weekday peaks cluster at 0.30, weekend peaks at 0.10. Today is a
    Saturday with peak 0.20 — above the weekend median (0.10) but well below
    the weekday median. Day-class normalisation makes adjusted_today = 2.0,
    which sits at the *top* of the adjusted distribution (weekday adjusted ~1,
    weekend adjusted ~1). Expected: score very high.
    """
    saturday = date(2024, 1, 20)  # Saturday
    now = datetime(2024, 1, 20, 20, 0, tzinfo=timezone.utc)

    peaks: list[DailyPeak] = []
    d = saturday
    while len(peaks) < 30:
        d = d - timedelta(days=1)
        if d.weekday() >= 5:
            peaks.append(_peak(d, 0.10))   # weekend baseline
        else:
            peaks.append(_peak(d, 0.30))   # weekday baseline
    history = _history(peaks)

    series = _price_series_for_day(saturday, peak_at_hour=18, peak_value=0.20)
    result = compute_profitability_score(series, history, now=now)
    assert result.today_class == DayClass.WEEKEND
    assert result.score == 1.0


def test_score_uses_holiday_bucket():
    """A holiday on a weekday is classified as HOLIDAY and gets its own median."""
    holiday = date(2024, 1, 15)  # Monday flagged as holiday
    now = datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc)

    peaks: list[DailyPeak] = []
    d = holiday
    holidays_set = {holiday}
    # Build 30 days: every 5th weekday is a holiday at 0.15, others at 0.30
    while len(peaks) < 30:
        d = d - timedelta(days=1)
        cls = classify_day(d, is_holiday=lambda x: False)  # historical days not holidays
        # synthesise a few past holidays so the HOLIDAY bucket isn't empty
        if d.weekday() < 5 and d.day % 7 == 0:
            cls = DayClass.HOLIDAY
            peaks.append(DailyPeak(day=d, peak_eur_kwh=0.15, day_class=cls))
        elif d.weekday() >= 5:
            peaks.append(_peak(d, 0.10))
        else:
            peaks.append(_peak(d, 0.30))
    history = _history(peaks)

    series = _price_series_for_day(holiday, peak_at_hour=18, peak_value=0.15)
    result = compute_profitability_score(
        series, history, now=now, is_holiday=lambda d: d in holidays_set
    )
    assert result.today_class == DayClass.HOLIDAY
    # Today equals the holiday-median → adjusted=1.0, percentile around midpoint.
    assert result.score is not None
    assert 0.3 < result.score < 0.7


def test_score_rank_window_limits_to_recent_days():
    """Old days outside the rolling window must not influence the score.

    Builds 100 weekdays of history where the 30 most-recent days share one
    price level and older days are very cheap. With `rank_window_days=30`
    the distribution should consist only of the recent level, so today set
    to that same level scores at the midpoint. If the window cap leaked,
    today would be near the top of the distribution instead.
    """
    today = date(2024, 6, 3)  # Monday — keep today_class = WEEKDAY
    now = datetime(2024, 6, 3, 20, 0, tzinfo=timezone.utc)

    peaks: list[DailyPeak] = []
    d = today
    while len(peaks) < 100:
        d = d - timedelta(days=1)
        if d.weekday() >= 5:
            continue
        # First 30 weekdays collected (most-recent) → 0.30; older → 0.05.
        peaks.append(_peak(d, 0.30 if len(peaks) < 30 else 0.05))
    history = _history(peaks)

    series = _price_series_for_day(today, peak_at_hour=18, peak_value=0.30)
    result = compute_profitability_score(series, history, now=now, rank_window_days=30)
    # Today equals recent baseline → 0.5; if old days leaked in, score → 1.0.
    assert result.score == 0.5


def test_score_resolves_today_in_local_time():
    """'Today' is the operator's local date, not the UTC date of `now`.

    At 01:00 local Saturday (Riga, UTC+2) the UTC instant is still Friday
    23:00. With local_tz the day-class is WEEKEND; UTC logic says WEEKDAY.
    """
    riga = ZoneInfo("Europe/Riga")
    now = datetime(2024, 1, 12, 23, 0, tzinfo=timezone.utc)  # == local Sat 01:00
    history = _build_30d_weekday_only_history(date(2024, 1, 13))
    series = _price_series_for_day(date(2024, 1, 13), peak_at_hour=12, peak_value=0.20)

    local_result = compute_profitability_score(series, history, now=now, local_tz=riga)
    assert local_result.today_class == DayClass.WEEKEND

    # Without local_tz the scorer falls back to the UTC date → Friday.
    utc_result = compute_profitability_score(series, history, now=now)
    assert utc_result.today_class == DayClass.WEEKDAY


# ---------------------------------------------------------------------------
# daily_peak_from_entries
# ---------------------------------------------------------------------------

def test_daily_peak_from_entries_picks_max():
    day = date(2024, 1, 15)
    base = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
    entries = [
        PriceEntry(start=base + timedelta(hours=h), end=base + timedelta(hours=h + 1), price_eur_kwh=v)
        for h, v in [(0, 0.10), (12, 0.25), (18, 0.40), (23, 0.15)]
    ]
    peak = daily_peak_from_entries(day, entries)
    assert peak is not None
    assert peak.day == day
    assert peak.peak_eur_kwh == pytest.approx(0.40)
    assert peak.day_class == DayClass.WEEKDAY


def test_daily_peak_from_entries_none_when_no_match():
    day = date(2024, 1, 15)
    other = datetime(2024, 1, 16, 12, tzinfo=timezone.utc)
    entries = [PriceEntry(start=other, end=other + timedelta(hours=1), price_eur_kwh=0.20)]
    assert daily_peak_from_entries(day, entries) is None
