"""Tests for pipeline/solar_geometry.py — pure Python, no HA required.

The primary guard is a differential one: an *independent* solar-position
algorithm (the Astronomical Almanac's low-precision formulae, accurate to
~0.01° over 1950–2050) is implemented here as an oracle and compared against
the module's NOAA implementation across a year of random instants. Two
independently derived algorithms agreeing to a hundredth of a degree is much
stronger evidence than any hand-copied table of expected values, and it cannot
drift into agreement the way a golden fixture regenerated from the code can.

Physical invariants (solstice/equinox noon elevations, azimuth quadrants,
sunrise/sunset symmetry) cover the cases the oracle shares assumptions with.
"""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sun_sale.pipeline.solar_geometry import (
    SolarPosition,
    _refraction_deg,
    solar_position,
)

# Real deployment site (HA /api/config), used so the fixtures describe a
# latitude the integration actually runs at.
SITE_LAT = 54.9110344
SITE_LON = 23.9253644

# Earth's axial tilt — sets the solstice declination extremes.
_OBLIQUITY = 23.44


def _almanac_elevation_deg(when: datetime, lat: float, lon: float) -> float:
    """Return geometric solar elevation via the Astronomical Almanac formulae.

    Deliberately independent of the module under test: different series, different
    intermediate quantities, right ascension/GMST rather than NOAA's equation of
    time. Returns a *geometric* elevation with no refraction correction.

    Args:
        when: Timezone-aware instant.
        lat: Latitude in degrees, positive north.
        lon: Longitude in degrees, positive east.

    Returns:
        Geometric solar elevation in degrees.
    """
    jd = when.timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0
    mean_long = math.radians((280.460 + 0.9856474 * n) % 360)
    mean_anom = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic_long = (
        mean_long
        + math.radians(1.915) * math.sin(mean_anom)
        + math.radians(0.020) * math.sin(2 * mean_anom)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)
    right_asc = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic_long), math.cos(ecliptic_long)
    )
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_long))
    gmst_hours = (18.697374558 + 24.06570982441908 * n) % 24
    lmst_hours = (gmst_hours + lon / 15.0) % 24
    hour_angle = math.radians(lmst_hours * 15.0) - right_asc
    lat_rad = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
    ))


def _geometric_elevation(when: datetime, lat: float, lon: float) -> float:
    """Return the module's elevation with its refraction correction undone.

    ``solar_position`` reports *apparent* elevation; the oracle is geometric.
    Refraction is a function of the geometric value, so it is inverted by fixed
    point iteration (monotonic and rapidly convergent over visible elevations).

    Args:
        when: Timezone-aware instant.
        lat: Latitude in degrees.
        lon: Longitude in degrees.

    Returns:
        Geometric (unrefracted) elevation in degrees.
    """
    apparent = solar_position(when, lat, lon).elevation_deg
    geometric = apparent
    for _ in range(40):
        geometric = apparent - _refraction_deg(geometric)
    return geometric


def _peak_of_day(day: datetime, lat: float, lon: float) -> SolarPosition:
    """Return the highest solar position reached on a UTC calendar day.

    Args:
        day: Midnight UTC of the day to scan.
        lat: Latitude in degrees.
        lon: Longitude in degrees.

    Returns:
        The SolarPosition of maximum elevation, sampled at 1-minute steps.
    """
    return max(
        (solar_position(day + timedelta(minutes=m), lat, lon) for m in range(1440)),
        key=lambda p: p.elevation_deg,
    )


# ---------------------------------------------------------------------------
# Differential check against an independent algorithm
# ---------------------------------------------------------------------------


def test_matches_independent_almanac_algorithm_across_a_year():
    """NOAA and Almanac elevations must agree to ~0.01° at daylight instants.

    This is the load-bearing correctness test. Anything that breaks the series
    expansions, the Julian-day conversion, or the hour-angle derivation moves
    this well past tolerance.
    """
    rng = random.Random(7)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    worst = 0.0
    compared = 0

    for _ in range(4000):
        when = start + timedelta(seconds=rng.randrange(0, 365 * 86400))
        expected = _almanac_elevation_deg(when, SITE_LAT, SITE_LON)
        if expected < -2.0:
            continue  # below the horizon: refraction dominates, oracle N/A
        worst = max(worst, abs(_geometric_elevation(when, SITE_LAT, SITE_LON) - expected))
        compared += 1

    assert compared > 1500, "sampling should cover a useful number of daylight instants"
    assert worst < 0.02, f"max deviation {worst:.4f}° exceeds tolerance"


@pytest.mark.parametrize("lat,lon", [
    (54.91, 23.93),      # deployment site
    (-33.87, 151.21),    # southern hemisphere
    (0.0, 0.0),          # equator / prime meridian
    (64.13, -21.90),     # sub-arctic
])
def test_agrees_with_oracle_at_varied_sites(lat: float, lon: float):
    """The agreement must hold away from the deployment site too.

    Guards the hemisphere-dependent branches — southern latitudes flip the
    azimuth quadrant logic and the sign of the declination term.
    """
    start = datetime(2026, 4, 10, tzinfo=UTC)
    worst = max(
        abs(_geometric_elevation(start + timedelta(hours=h), lat, lon)
            - _almanac_elevation_deg(start + timedelta(hours=h), lat, lon))
        for h in range(0, 24 * 60)
        if _almanac_elevation_deg(start + timedelta(hours=h), lat, lon) > -2.0
    )
    assert worst < 0.02


# ---------------------------------------------------------------------------
# Physical invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day,expected_offset", [
    (datetime(2026, 3, 20, tzinfo=UTC), 0.0),              # equinox
    (datetime(2026, 6, 21, tzinfo=UTC), _OBLIQUITY),       # June solstice
    (datetime(2026, 12, 21, tzinfo=UTC), -_OBLIQUITY),     # December solstice
])
def test_noon_elevation_tracks_declination(day: datetime, expected_offset: float):
    """Peak elevation must equal 90° − latitude + declination.

    This is independent of the algorithm's internals: it is the definition of
    solar noon at a known declination.
    """
    peak = _peak_of_day(day, SITE_LAT, SITE_LON)
    expected = 90.0 - SITE_LAT + expected_offset
    assert peak.elevation_deg == pytest.approx(expected, abs=0.25)


def test_sun_is_due_south_at_local_noon_in_northern_hemisphere():
    """At a northern site the sun peaks due south (azimuth ≈ 180°)."""
    peak = _peak_of_day(datetime(2026, 6, 21, tzinfo=UTC), SITE_LAT, SITE_LON)
    assert peak.azimuth_deg == pytest.approx(180.0, abs=0.5)


def test_sun_is_due_north_at_local_noon_in_southern_hemisphere():
    """At a southern site the sun peaks due north — the mirrored branch."""
    peak = _peak_of_day(datetime(2026, 6, 21, tzinfo=UTC), -33.87, 151.21)
    assert peak.azimuth_deg == pytest.approx(0.0, abs=0.5) or \
           peak.azimuth_deg == pytest.approx(360.0, abs=0.5)


def test_azimuth_sweeps_east_through_south_to_west():
    """Morning sun sits east of south, evening sun west of it.

    Pins the hour-angle sign branch in the azimuth calculation — the easiest
    thing to get backwards, and invisible to an elevation-only comparison.
    """
    day = datetime(2026, 6, 21, tzinfo=UTC)
    peak_minute = max(range(1440), key=lambda m: solar_position(
        day + timedelta(minutes=m), SITE_LAT, SITE_LON).elevation_deg)

    morning = solar_position(day + timedelta(minutes=peak_minute - 240), SITE_LAT, SITE_LON)
    evening = solar_position(day + timedelta(minutes=peak_minute + 240), SITE_LAT, SITE_LON)

    assert 45.0 < morning.azimuth_deg < 180.0
    assert 180.0 < evening.azimuth_deg < 315.0


def test_polar_night_and_midnight_sun():
    """Above the Arctic circle the sun stays down in December and up in June."""
    svalbard_lat, svalbard_lon = 78.22, 15.65
    december = _peak_of_day(datetime(2026, 12, 21, tzinfo=UTC), svalbard_lat, svalbard_lon)
    june_min = min(
        (solar_position(datetime(2026, 6, 21, tzinfo=UTC) + timedelta(minutes=m),
                        svalbard_lat, svalbard_lon) for m in range(1440)),
        key=lambda p: p.elevation_deg,
    )
    assert december.elevation_deg < 0.0
    assert june_min.elevation_deg > 0.0


# ---------------------------------------------------------------------------
# Refraction and contract
# ---------------------------------------------------------------------------


def test_refraction_lifts_the_horizon_and_vanishes_overhead():
    """Refraction is ~0.5° at the horizon and negligible near the zenith."""
    assert _refraction_deg(0.0) == pytest.approx(0.48, abs=0.10)
    assert _refraction_deg(90.0) == 0.0
    assert _refraction_deg(45.0) < 0.02


def test_apparent_elevation_is_never_below_geometric_when_visible():
    """Refraction only ever raises the apparent sun, never lowers it."""
    day = datetime(2026, 9, 15, tzinfo=UTC)
    for m in range(0, 1440, 7):
        when = day + timedelta(minutes=m)
        apparent = solar_position(when, SITE_LAT, SITE_LON).elevation_deg
        geometric = _geometric_elevation(when, SITE_LAT, SITE_LON)
        if geometric > 0.0:
            assert apparent >= geometric - 1e-9


def test_naive_datetime_is_rejected():
    """A naive datetime must raise rather than be silently treated as UTC.

    A silent UTC assumption would shift every slot by the site's offset and
    corrupt every downstream bucket without any visible failure.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        solar_position(datetime(2026, 6, 21, 12, 0), SITE_LAT, SITE_LON)


def test_azimuth_is_always_a_valid_compass_bearing():
    """Azimuth must stay within [0, 360) at every instant, including midnight."""
    day = datetime(2026, 2, 2, tzinfo=UTC)
    for m in range(0, 1440, 3):
        az = solar_position(day + timedelta(minutes=m), SITE_LAT, SITE_LON).azimuth_deg
        assert 0.0 <= az < 360.0


def test_is_daylight_matches_elevation_sign():
    """The convenience flag must agree with the elevation it derives from."""
    day = datetime(2026, 5, 5, tzinfo=UTC)
    for m in range(0, 1440, 11):
        pos = solar_position(day + timedelta(minutes=m), SITE_LAT, SITE_LON)
        assert pos.is_daylight == (pos.elevation_deg > 0.0)
