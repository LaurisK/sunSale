"""Tests for pipeline/solar_health.py — pure Python, no HA required.

The alarm's whole claim is that it separates *hardware* change from weather.
So the two load-bearing tests are the pair: a soiled array must be caught even
though nothing about the weather changed, and a spell of bad weather must NOT
raise the alarm even though output collapsed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    ArrayCalibration,
    BakedDayRecord,
    BakedObservedHistory,
    SlotKwh,
)
from custom_components.sun_sale.pipeline.clear_sky import clear_sky_power_kw
from custom_components.sun_sale.pipeline.solar_geometry import solar_position
from custom_components.sun_sale.pipeline.solar_health import build_solar_health

SITE_LAT = 54.9110344
SITE_LON = 23.9253644
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

CALIBRATION = ArrayCalibration(
    kwp_eff=7.5, tilt_deg=35.0, azimuth_deg=170.0,
    fit_quality=0.9, n_days=15, n_slots=600, confidence=0.8,
)


def _day_record(day: datetime, efficiency: float) -> BakedDayRecord:
    """Build one baked day at a given fraction of clear-sky potential.

    Args:
        day: Midnight UTC of the day.
        efficiency: Multiplier on clear-sky output (1.0 = cloudless and clean).

    Returns:
        A generation-side BakedDayRecord for that day.
    """
    slots = []
    for i in range(96):
        start = day + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        position = solar_position(start + timedelta(minutes=7.5), SITE_LAT, SITE_LON)
        power = clear_sky_power_kw(
            position.elevation_deg, position.azimuth_deg,
            CALIBRATION.kwp_eff, CALIBRATION.tilt_deg, CALIBRATION.azimuth_deg,
        ) if position.elevation_deg > 0 else 0.0
        slots.append(SlotKwh(start=start, end=end, kwh=power * efficiency / 4.0))
    total = sum(s.kwh for s in slots)
    return BakedDayRecord(
        date_str=day.date().isoformat(), side_id="generation",
        counter_total_used=total, source_kind="dedicated_sensor",
        baked_slots=tuple(slots), baked_sum=total, baked_at=day,
    )


def _history(efficiencies: list[float], start: datetime | None = None) -> BakedObservedHistory:
    """Build a baked history, one day per efficiency, oldest first."""
    base = start or datetime(2026, 6, 1, tzinfo=UTC)
    return BakedObservedHistory(records=tuple(
        _day_record(base + timedelta(days=i), eff)
        for i, eff in enumerate(efficiencies)
    ))


def _health(efficiencies: list[float]):
    """Run the health check over a synthetic history."""
    return build_solar_health(
        history=_history(efficiencies), calibration=CALIBRATION,
        latitude=SITE_LAT, longitude=SITE_LON, now=NOW,
    )


# ---------------------------------------------------------------------------
# The pair that matters
# ---------------------------------------------------------------------------


def test_soiled_array_is_flagged():
    """A persistent drop in achievable output must be caught.

    Baseline days reach full output; every recent day is capped at 60% no
    matter the weather — the signature of soiling, snow or a dead string.
    """
    health = _health([1.0] * 20 + [0.6] * 7)
    assert health is not None
    assert health.status == "degraded"
    assert health.ratio == pytest.approx(0.6, abs=0.05)


def test_bad_weather_does_not_raise_the_alarm():
    """A cloudy stretch that still contains one good day must read "ok".

    This is the false-positive case the ceiling comparison exists to avoid: a
    mean-based check would flag this week as degraded even though the hardware
    is fine.
    """
    health = _health([1.0] * 20 + [0.15, 0.2, 0.1, 0.98, 0.25, 0.1, 0.3])
    assert health is not None
    assert health.status == "ok"


# ---------------------------------------------------------------------------
# Refusing to guess
# ---------------------------------------------------------------------------


def test_a_fully_overcast_week_is_flagged_and_this_is_a_known_limitation():
    """Seven days with no clear spell at all reports degraded, not unknown.

    Documents an accepted false-positive mode rather than hiding it: the check
    cannot distinguish "array broken" from "sun never came out for a week",
    because both look identical in the only signal available. It is tuned to
    accept this — a week whose *best* moment reaches 10% of capacity is worth a
    look whatever the cause — but on a genuinely sunless week the verdict will
    be wrong. Weather that merely dips (see the cloudy-week test) is handled
    correctly; this is the tail case.
    """
    health = _health([1.0] * 20 + [0.1] * 7)
    assert health is not None
    assert health.status == "degraded"
    assert health.ratio == pytest.approx(0.1, abs=0.02)


def test_short_history_reports_unknown():
    """Without a baseline the array has no "own past" to be compared against."""
    health = _health([1.0] * 5)
    assert health is not None
    assert health.status == "unknown"
    assert health.baseline_ceiling is None


def test_no_calibration_yields_no_verdict():
    """Without a fitted array there is no clear-sky reference to divide by."""
    assert build_solar_health(
        history=_history([1.0] * 20), calibration=None,
        latitude=SITE_LAT, longitude=SITE_LON, now=NOW,
    ) is None


def test_no_site_coordinates_yields_no_verdict():
    """Solar position is required to pick the high-sun slots."""
    assert build_solar_health(
        history=_history([1.0] * 20), calibration=CALIBRATION,
        latitude=None, longitude=None, now=NOW,
    ) is None


def test_empty_history_reports_unknown_rather_than_none():
    """An empty-but-present history is a real state: nothing measured yet."""
    health = build_solar_health(
        history=BakedObservedHistory(records=()), calibration=CALIBRATION,
        latitude=SITE_LAT, longitude=SITE_LON, now=NOW,
    )
    assert health is not None
    assert health.status == "unknown"


def test_grid_side_records_are_ignored():
    """Grid import/export is not solar and must not feed the health verdict."""
    solar = _history([1.0] * 20)
    grid_only = BakedObservedHistory(records=tuple(
        BakedDayRecord(
            date_str=r.date_str, side_id="grid_import",
            counter_total_used=r.counter_total_used, source_kind=r.source_kind,
            baked_slots=r.baked_slots, baked_sum=r.baked_sum, baked_at=r.baked_at,
        ) for r in solar.records
    ))
    health = build_solar_health(
        history=grid_only, calibration=CALIBRATION,
        latitude=SITE_LAT, longitude=SITE_LON, now=NOW,
    )
    assert health is not None
    assert health.status == "unknown"


# ---------------------------------------------------------------------------
# Recovery and reporting
# ---------------------------------------------------------------------------


def test_cleaning_the_panels_clears_the_alarm():
    """Once output recovers, the verdict must return to ok on its own."""
    assert _health([1.0] * 20 + [0.6] * 7).status == "degraded"
    assert _health([1.0] * 20 + [0.6] * 5 + [0.99, 0.98]).status == "ok"


def test_verdict_reports_the_windows_it_used():
    """The day counts must be present so the number can be argued with."""
    health = _health([1.0] * 20 + [0.9] * 7)
    assert health is not None
    assert health.recent_days == 7
    assert health.baseline_days == 20
    assert health.computed_at == NOW


def test_an_improving_array_is_not_flagged():
    """A ratio above 1 (better than baseline) is healthy, not anomalous."""
    health = _health([0.7] * 20 + [1.0] * 7)
    assert health is not None
    assert health.status == "ok"
    assert health.ratio > 1.0


def test_verdict_tracks_the_calibration_it_was_given():
    """The verdict is relative to the supplied calibration, not an absolute.

    Pins that the clear-sky denominator really is the calibration passed in: a
    calibration claiming a larger array makes the same measured output look
    worse. This is why a stale calibration would quietly shift the verdict, and
    why the node reads the persisted one consistently rather than sometimes a
    fresh mid-cycle fit.
    """
    history = _history([1.0] * 20 + [1.0] * 7)
    oversized = ArrayCalibration(
        kwp_eff=CALIBRATION.kwp_eff * 2, tilt_deg=CALIBRATION.tilt_deg,
        azimuth_deg=CALIBRATION.azimuth_deg, fit_quality=0.9,
        n_days=15, n_slots=600, confidence=0.8,
    )
    matched = build_solar_health(
        history=history, calibration=CALIBRATION,
        latitude=SITE_LAT, longitude=SITE_LON, now=NOW,
    )
    inflated = build_solar_health(
        history=history, calibration=oversized,
        latitude=SITE_LAT, longitude=SITE_LON, now=NOW,
    )
    assert matched.recent_ceiling == pytest.approx(1.0, abs=0.05)
    assert inflated.recent_ceiling == pytest.approx(0.5, abs=0.05)
    # Both windows scale together, so the *ratio* — and thus the verdict — is
    # unchanged: the check is about change over time, not absolute calibration.
    assert matched.status == inflated.status == "ok"
