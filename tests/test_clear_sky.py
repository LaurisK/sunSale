"""Tests for pipeline/clear_sky.py — pure Python, no HA required.

The clear-sky model is a *relative* one: its absolute constants are absorbed by
the fitted ``kwp``, so what has to be right is its **shape** against solar
elevation and incidence angle. The load-bearing test is therefore
``test_clear_sky_index_is_flat_across_a_cloudless_day`` — on a real cloudless
day, measured output divided by the model must be roughly constant. A model
with the wrong shape produces a visible arc instead.
"""
from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sun_sale.pipeline.clear_sky import (
    MIN_MODELLED_ELEVATION_DEG,
    air_mass,
    clear_sky_dni_wm2,
    clear_sky_index,
    clear_sky_poa_wm2,
    clear_sky_power_kw,
    incidence_cosine,
)
from custom_components.sun_sale.pipeline.solar_geometry import solar_position

SITE_LAT = 54.9110344
SITE_LON = 23.9253644


# ---------------------------------------------------------------------------
# Air mass
# ---------------------------------------------------------------------------


def test_air_mass_is_unity_at_the_zenith():
    """Straight overhead, the beam crosses exactly one atmosphere."""
    assert air_mass(90.0) == pytest.approx(1.0, abs=0.01)


def test_air_mass_grows_toward_the_horizon_and_stays_finite():
    """Kasten–Young must not diverge at the horizon the way 1/sin(h) does."""
    assert air_mass(30.0) == pytest.approx(2.0, abs=0.05)
    horizon = air_mass(0.001)
    assert 30.0 < horizon < 45.0, f"horizon air mass {horizon} is not physical"


def test_air_mass_is_infinite_below_the_horizon():
    """Below the horizon there is no beam path to speak of."""
    assert air_mass(-1.0) == math.inf


def test_air_mass_is_monotonic_in_elevation():
    """Lower sun always means a longer atmospheric path."""
    values = [air_mass(e) for e in range(1, 90)]
    assert all(a > b for a, b in zip(values, values[1:], strict=False))


# ---------------------------------------------------------------------------
# Direct normal irradiance
# ---------------------------------------------------------------------------


def test_dni_at_zenith_is_physically_plausible():
    """Clear-sky DNI overhead sits near ~950 W/m², well under the 1367 constant."""
    assert 850.0 < clear_sky_dni_wm2(90.0) < 1050.0


def test_dni_is_zero_below_the_modelling_floor():
    """Near the horizon the model is abandoned rather than extrapolated."""
    assert clear_sky_dni_wm2(MIN_MODELLED_ELEVATION_DEG - 0.01) == 0.0
    assert clear_sky_dni_wm2(-5.0) == 0.0


def test_dni_falls_off_toward_the_horizon():
    """More atmosphere means less beam reaching the array."""
    assert clear_sky_dni_wm2(60.0) > clear_sky_dni_wm2(30.0) > clear_sky_dni_wm2(10.0)


# ---------------------------------------------------------------------------
# Incidence angle
# ---------------------------------------------------------------------------


def test_incidence_is_unity_when_sun_is_normal_to_the_panel():
    """A panel aimed straight at the sun sees the full beam."""
    # Sun 40° up in the south; a 50° tilt facing south points right at it.
    assert incidence_cosine(40.0, 180.0, 50.0, 180.0) == pytest.approx(1.0, abs=1e-6)


def test_incidence_is_zero_when_sun_is_behind_the_panel():
    """Beam contribution clamps at zero rather than going negative.

    A negative cosine would otherwise *subtract* irradiance and could drive the
    modelled reference below zero.
    """
    assert incidence_cosine(20.0, 0.0, 80.0, 180.0) == 0.0


def test_flat_panel_incidence_equals_sine_of_elevation():
    """With zero tilt the incidence cosine reduces to sin(elevation)."""
    for elevation in (10.0, 35.0, 70.0):
        assert incidence_cosine(elevation, 123.0, 0.0, 180.0) == pytest.approx(
            math.sin(math.radians(elevation)), abs=1e-9
        )


# ---------------------------------------------------------------------------
# Plane-of-array irradiance and power
# ---------------------------------------------------------------------------


def test_poa_is_zero_at_night():
    """No sun, no reference — and therefore no CSI denominator."""
    assert clear_sky_poa_wm2(-10.0, 180.0, 35.0, 180.0) == 0.0


def test_poa_keeps_a_diffuse_floor_when_beam_is_edge_on():
    """Even with the sun behind the array, diffuse light keeps POA positive.

    Without this the reference would collapse to zero mid-morning for an
    east/west split array and produce meaningless CSI values.
    """
    poa = clear_sky_poa_wm2(30.0, 0.0, 80.0, 180.0)
    assert poa > 0.0
    assert incidence_cosine(30.0, 0.0, 80.0, 180.0) == 0.0


def test_power_scales_linearly_with_capacity():
    """Doubling kWp doubles the modelled output — kWp is a pure scale factor."""
    single = clear_sky_power_kw(40.0, 180.0, 5.0, 35.0, 180.0)
    double = clear_sky_power_kw(40.0, 180.0, 10.0, 35.0, 180.0)
    assert double == pytest.approx(2 * single, rel=1e-9)


def test_power_peaks_when_array_faces_the_sun():
    """A south-facing array out-produces an east-facing one at solar noon."""
    south = clear_sky_power_kw(40.0, 180.0, 8.0, 35.0, 180.0)
    east = clear_sky_power_kw(40.0, 180.0, 8.0, 35.0, 90.0)
    assert south > east


# ---------------------------------------------------------------------------
# Clear-sky index
# ---------------------------------------------------------------------------


def test_csi_is_one_when_output_matches_the_reference():
    """The defining case: matching the model means CSI == 1."""
    assert clear_sky_index(4.0, 4.0) == pytest.approx(1.0)


def test_csi_exceeds_one_when_the_array_beats_the_model():
    """CSI > 1 is meaningful signal, not an error to clamp away.

    A persistently over-unity CSI is the signature of a declared capacity that
    is too small — the single thing this axis exists to surface.
    """
    assert clear_sky_index(6.0, 4.0) == pytest.approx(1.5)


def test_csi_is_none_when_the_reference_is_too_small_to_divide_by():
    """Near sunrise/sunset the ratio is dominated by model error, not weather."""
    assert clear_sky_index(0.01, 0.0) is None
    assert clear_sky_index(0.5, 0.001) is None


def test_csi_never_goes_negative():
    """Observed power is clamped at zero before the ratio is taken."""
    assert clear_sky_index(-1.0, 4.0) == 0.0


# ---------------------------------------------------------------------------
# The load-bearing shape check
# ---------------------------------------------------------------------------


# Measured 15-min generation (kWh) from the deployment site on 2026-08-27, a
# cloudless day, starting 04:30 UTC. Captured from the live debug endpoint.
_CLEAR_DAY_START = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)
_CLEAR_DAY_KWH = [
    0.2865, 0.3631, 0.5183, 0.7545, 0.8583, 0.9589, 1.0555, 1.1449, 1.2278,
    1.2975, 1.3615, 1.4278, 1.4898, 1.5343, 1.5737, 1.6074, 1.6312, 1.6546,
    1.6660, 1.6830, 1.6837, 1.6766, 1.6608, 1.6451, 1.6276, 1.6231, 1.5921,
]


def test_clear_sky_index_is_flat_across_a_cloudless_day():
    """On a real cloudless day, CSI must be near-constant across the day.

    This is the model's actual contract. Season and time-of-day are supposed to
    divide out; if the elevation or incidence-angle shape is wrong, CSI traces
    an arc across the day instead of a flat line, and every CSI bucket silently
    becomes a proxy for time-of-day.
    """
    tilt, array_azimuth, kwp = 55.0, 160.0, 7.38   # fitted for this site

    indices = []
    for i, kwh in enumerate(_CLEAR_DAY_KWH):
        midpoint = _CLEAR_DAY_START + timedelta(minutes=15 * i + 7.5)
        position = solar_position(midpoint, SITE_LAT, SITE_LON)
        if position.elevation_deg < 15.0:
            continue  # horizon shading is real here and is not a model defect
        reference = clear_sky_power_kw(
            position.elevation_deg, position.azimuth_deg, kwp, tilt, array_azimuth
        )
        csi = clear_sky_index(kwh * 4, reference)
        assert csi is not None
        indices.append(csi)

    assert len(indices) >= 20, "fixture should cover most of the solar day"
    spread = statistics.pstdev(indices) / statistics.mean(indices)
    assert spread < 0.10, f"CSI varies {spread:.1%} across a clear day — model shape is wrong"
    assert 0.85 < statistics.mean(indices) < 1.15
