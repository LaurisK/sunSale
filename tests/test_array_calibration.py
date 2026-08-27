"""Tests for pipeline/array_calibration.py and the geometry fit.

The load-bearing test is ``test_fit_recovers_known_synthetic_geometry``:
synthetic output is generated from a *known* tilt/azimuth/kWp, and the fit must
recover them. That checks the objective function actually has its minimum where
the truth is, which no amount of testing against real data can establish (real
data has no ground-truth geometry to compare against).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    BakedDayRecord,
    BakedObservedHistory,
    GenerationSeries,
    GenerationSlot,
    SlotKwh,
)
from custom_components.sun_sale.pipeline.array_calibration import (
    build_array_calibration,
)
from custom_components.sun_sale.pipeline.clear_sky import (
    clear_sky_power_kw,
    fit_array_geometry,
    fit_confidence,
)
from custom_components.sun_sale.pipeline.solar_geometry import solar_position

SITE_LAT = 54.9110344
SITE_LON = 23.9253644

TRUE_KWP = 7.5
TRUE_TILT = 35.0
TRUE_AZIMUTH = 170.0


def _synthetic_day(
    day: datetime,
    kwp: float = TRUE_KWP,
    tilt: float = TRUE_TILT,
    azimuth: float = TRUE_AZIMUTH,
    cloud: float = 1.0,
) -> list[tuple[float, float, float]]:
    """Build one day of (elevation, azimuth, power_kw) from a known geometry.

    Args:
        day: Midnight UTC of the day to synthesise.
        kwp: Array capacity to generate from.
        tilt: Array tilt to generate from.
        azimuth: Array azimuth to generate from.
        cloud: Multiplier applied to every slot (1.0 = cloudless).

    Returns:
        Daylight slot tuples for that day at 15-minute resolution.
    """
    out = []
    for i in range(96):
        midpoint = day + timedelta(minutes=15 * i + 7.5)
        position = solar_position(midpoint, SITE_LAT, SITE_LON)
        if position.elevation_deg <= 0:
            continue
        power = clear_sky_power_kw(
            position.elevation_deg, position.azimuth_deg, kwp, tilt, azimuth
        )
        out.append((position.elevation_deg, position.azimuth_deg, power * cloud))
    return out


# ---------------------------------------------------------------------------
# The geometry fit
# ---------------------------------------------------------------------------


def test_fit_recovers_known_synthetic_geometry():
    """Given output generated from a known array, the fit must recover it.

    Establishes that the objective's minimum sits at the truth. Tolerances are
    one grid step for the angles, since the search is deliberately coarse.
    """
    days = {
        (datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=d)).date().isoformat():
            _synthetic_day(datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=d))
        for d in range(0, 40, 4)
    }
    fit = fit_array_geometry(days)
    assert fit is not None

    kwp, tilt, azimuth, quality, n_days, n_slots = fit
    assert kwp == pytest.approx(TRUE_KWP, rel=0.05)
    assert abs(tilt - TRUE_TILT) <= 5.0
    assert abs(azimuth - TRUE_AZIMUTH) <= 10.0
    assert quality > 0.95, "a noiseless synthetic day should fit almost exactly"
    assert n_days >= 4
    assert n_slots > 0


def test_fit_prefers_clear_days_over_overcast_ones():
    """Capacity must come from the clear days, not be dragged down by cloud.

    The model describes a cloudless sky, so overcast slots are not evidence
    about capacity. If they were admitted, kwp_eff would track average weather
    and the clear-sky index would sit permanently near 1.0 — destroying the
    entire signal.
    """
    base = datetime(2026, 5, 1, tzinfo=UTC)
    days = {}
    for d in range(20):
        day = base + timedelta(days=d)
        # Three quarters of the days are heavily overcast.
        cloud = 1.0 if d % 4 == 0 else 0.25
        days[day.date().isoformat()] = _synthetic_day(day, cloud=cloud)

    fit = fit_array_geometry(days)
    assert fit is not None
    assert fit[0] == pytest.approx(TRUE_KWP, rel=0.10)


def test_fit_returns_none_without_enough_history():
    """A couple of days is not evidence — the fit must decline to guess."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    days = {
        (base + timedelta(days=d)).date().isoformat(): _synthetic_day(base + timedelta(days=d))
        for d in range(2)
    }
    assert fit_array_geometry(days) is None


def test_fit_returns_none_for_empty_history():
    """No history at all yields no calibration rather than a default one."""
    assert fit_array_geometry({}) is None


def test_fit_returns_none_when_days_have_no_daylight():
    """Polar-night-style input has nothing to fit and must not crash."""
    assert fit_array_geometry({f"2026-01-{d:02d}": [] for d in range(1, 12)}) is None


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def test_confidence_needs_both_fit_quality_and_sample_count():
    """A tight fit over few days and a loose fit over many both score low.

    They are independent failure modes: three lucky days is not evidence, and a
    year of a wrong model is a wrong model measured patiently.
    """
    assert fit_confidence(0.99, 2) < 0.2
    assert fit_confidence(0.3, 100) < 0.4
    assert fit_confidence(0.95, 30) > 0.9


def test_confidence_is_bounded_to_unit_range():
    """Degenerate inputs must not produce a confidence outside [0, 1]."""
    assert fit_confidence(-5.0, 50) == 0.0
    assert 0.0 <= fit_confidence(2.0, 1000) <= 1.0


# ---------------------------------------------------------------------------
# End-to-end calibration
# ---------------------------------------------------------------------------


def _baked_history(days: int = 40, kwp: float = TRUE_KWP) -> BakedObservedHistory:
    """Build a baked generation history from the synthetic geometry."""
    records = []
    base = datetime(2026, 5, 1, tzinfo=UTC)
    for d in range(0, days, 2):
        day = base + timedelta(days=d)
        slots = []
        for i in range(96):
            start = day + timedelta(minutes=15 * i)
            end = start + timedelta(minutes=15)
            position = solar_position(start + timedelta(minutes=7.5), SITE_LAT, SITE_LON)
            power = clear_sky_power_kw(
                position.elevation_deg, position.azimuth_deg, kwp, TRUE_TILT, TRUE_AZIMUTH
            ) if position.elevation_deg > 0 else 0.0
            slots.append(SlotKwh(start=start, end=end, kwh=power / 4.0))
        records.append(BakedDayRecord(
            date_str=day.date().isoformat(),
            side_id="generation",
            counter_total_used=sum(s.kwh for s in slots),
            source_kind="dedicated_sensor",
            baked_slots=tuple(slots),
            baked_sum=sum(s.kwh for s in slots),
            baked_at=day,
        ))
    return BakedObservedHistory(records=tuple(records))


def _forecast_series(kwp: float) -> GenerationSeries:
    """Build a forecast series from the synthetic geometry at a given capacity."""
    slots = []
    base = datetime(2026, 6, 10, tzinfo=UTC)
    for i in range(96 * 2):
        start = base + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        position = solar_position(start + timedelta(minutes=7.5), SITE_LAT, SITE_LON)
        power = clear_sky_power_kw(
            position.elevation_deg, position.azimuth_deg, kwp, TRUE_TILT, TRUE_AZIMUTH
        ) if position.elevation_deg > 0 else 0.0
        slots.append(GenerationSlot(start=start, end=end, expected_kwh=power / 4.0))
    return GenerationSeries(slots=tuple(slots))


def test_calibration_reports_no_correction_when_forecast_matches_reality():
    """A correctly-sized forecast yields a correction factor of ~1."""
    calibration = build_array_calibration(
        history=_baked_history(),
        forecast=_forecast_series(TRUE_KWP),
        latitude=SITE_LAT, longitude=SITE_LON,
        now=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )
    assert calibration is not None
    assert calibration.correction_factor == pytest.approx(1.0, rel=0.10)


def test_calibration_detects_an_undersized_forecast_array():
    """A forecast configured with half the real array needs ~2x correction.

    This is the diagnostic the whole module exists for: it is a one-time
    configuration fix in the forecast provider, worth more than any runtime
    bias correction.
    """
    calibration = build_array_calibration(
        history=_baked_history(kwp=TRUE_KWP),
        forecast=_forecast_series(TRUE_KWP / 2),
        latitude=SITE_LAT, longitude=SITE_LON,
        now=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )
    assert calibration is not None
    assert calibration.correction_factor == pytest.approx(2.0, rel=0.15)
    assert calibration.implied_forecast_kwp == pytest.approx(TRUE_KWP / 2, rel=0.15)


def test_calibration_detects_an_oversized_forecast_array():
    """The diagnostic must work in both directions, not just one.

    An over-declared array is the more dangerous case: it makes the planner
    expect solar that never arrives.
    """
    calibration = build_array_calibration(
        history=_baked_history(kwp=TRUE_KWP),
        forecast=_forecast_series(TRUE_KWP * 1.5),
        latitude=SITE_LAT, longitude=SITE_LON,
        now=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )
    assert calibration is not None
    assert calibration.correction_factor == pytest.approx(1 / 1.5, rel=0.15)


def test_calibration_is_none_without_history():
    """No baked history yields no calibration, not a fabricated one."""
    assert build_array_calibration(
        history=BakedObservedHistory(records=()),
        forecast=_forecast_series(TRUE_KWP),
        latitude=SITE_LAT, longitude=SITE_LON,
        now=datetime(2026, 6, 10, tzinfo=UTC),
    ) is None


def test_calibration_ignores_non_generation_sides():
    """Grid import/export records are not solar and must not enter the fit."""
    grid_only = BakedObservedHistory(records=tuple(
        BakedDayRecord(
            date_str=r.date_str, side_id="grid_export",
            counter_total_used=r.counter_total_used, source_kind=r.source_kind,
            baked_slots=r.baked_slots, baked_sum=r.baked_sum, baked_at=r.baked_at,
        )
        for r in _baked_history().records
    ))
    assert build_array_calibration(
        history=grid_only,
        forecast=_forecast_series(TRUE_KWP),
        latitude=SITE_LAT, longitude=SITE_LON,
        now=datetime(2026, 6, 10, tzinfo=UTC),
    ) is None
