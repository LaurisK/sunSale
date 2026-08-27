"""Solar position for a site — pure geometry, no HA and no dependencies.

Implements the NOAA Solar Position Algorithm: given a UTC instant and a site's
latitude/longitude, returns the sun's apparent elevation and azimuth. Accurate
to well under a tenth of a degree over the years this integration cares about,
which is far tighter than the bucket widths that consume it.

Why this exists
---------------
The forecast-quality buckets used to bin errors by *absolute* forecast kWh and
by *slot index* within the solar day. Both axes conflate unrelated causes: a
low-output slot may be low because the sun is near the horizon or because it is
overcast, and those are different failure modes with different fixes. Solar
position is the physical coordinate that separates them — it lets
``clear_sky.py`` express every slot as a fraction of its clear-sky potential,
and lets accuracy be binned by elevation (air-mass / model error) and azimuth
(fixed obstructions such as trees or a chimney).

All angles are degrees. Azimuth is measured clockwise from true north, so
0° = N, 90° = E, 180° = S, 270° = W.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

# Julian day number of the Unix epoch (1970-01-01T00:00:00Z).
_JD_UNIX_EPOCH = 2440587.5
# Julian day number of J2000.0 (2000-01-01T12:00:00 TT), the algorithm's origin.
_JD_J2000 = 2451545.0
_DAYS_PER_JULIAN_CENTURY = 36525.0

_SECONDS_PER_DAY = 86400.0
_MINUTES_PER_DAY = 1440.0
# Earth turns 1° of longitude every 4 minutes — converts longitude to a time offset.
_MINUTES_PER_DEGREE_LONGITUDE = 4.0


@dataclass(frozen=True)
class SolarPosition:
    """Apparent position of the sun as seen from a site.

    Attributes:
        elevation_deg: Apparent altitude above the horizon, refraction-corrected.
            Negative when the sun is below the horizon.
        azimuth_deg: Compass bearing clockwise from true north (0–360).
    """
    elevation_deg: float
    azimuth_deg: float

    @property
    def is_daylight(self) -> bool:
        """Return whether the sun is above the horizon."""
        return self.elevation_deg > 0.0


def _julian_century(when_utc: datetime) -> float:
    """Return the Julian century of a UTC instant, counted from J2000.0.

    Args:
        when_utc: Timezone-aware instant. Converted to UTC before use.

    Returns:
        Julian centuries since J2000.0 (negative before 2000).
    """
    julian_day = when_utc.timestamp() / _SECONDS_PER_DAY + _JD_UNIX_EPOCH
    return (julian_day - _JD_J2000) / _DAYS_PER_JULIAN_CENTURY


def _sun_declination_and_eq_time(jc: float) -> tuple[float, float]:
    """Return the sun's declination (deg) and the equation of time (minutes).

    The equation of time is the offset between apparent and mean solar time,
    caused by Earth's orbital eccentricity and axial tilt; it reaches ±16 min
    and must be applied before an hour angle means anything.

    Args:
        jc: Julian centuries since J2000.0.

    Returns:
        Tuple of (declination_deg, equation_of_time_minutes).
    """
    # Geometric mean longitude and anomaly of the sun (deg).
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    eccentricity = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)

    # Equation of centre — the elliptical orbit's departure from a circle.
    m_rad = math.radians(mean_anom)
    centre = (
        math.sin(m_rad) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * m_rad) * 0.000289
    )

    true_long = mean_long + centre
    # Nutation/aberration correction to get the *apparent* longitude.
    omega = 125.04 - 1934.136 * jc
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    mean_obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq_corr = mean_obliq + 0.00256 * math.cos(math.radians(omega))

    declination = math.degrees(math.asin(
        math.sin(math.radians(obliq_corr)) * math.sin(math.radians(app_long))
    ))

    var_y = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    l0_rad = math.radians(mean_long)
    eq_time = 4.0 * math.degrees(
        var_y * math.sin(2 * l0_rad)
        - 2 * eccentricity * math.sin(m_rad)
        + 4 * eccentricity * var_y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * var_y * var_y * math.sin(4 * l0_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m_rad)
    )
    return declination, eq_time


def _refraction_deg(true_elevation_deg: float) -> float:
    """Return atmospheric refraction to add to a geometric elevation (deg).

    The atmosphere bends light downward, so the sun appears higher than it
    geometrically is — by ~0.5° at the horizon, which is the difference between
    a dawn slot reading as daylight or as night. Negligible above ~85°.

    Args:
        true_elevation_deg: Geometric (unrefracted) elevation in degrees.

    Returns:
        Refraction correction in degrees, always ≥ 0 for visible elevations.
    """
    if true_elevation_deg > 85.0:
        return 0.0
    tan_e = math.tan(math.radians(true_elevation_deg))
    if true_elevation_deg > 5.0:
        arcsec = 58.1 / tan_e - 0.07 / tan_e ** 3 + 0.000086 / tan_e ** 5
    elif true_elevation_deg > -0.575:
        e = true_elevation_deg
        arcsec = 1735.0 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
    else:
        arcsec = -20.772 / tan_e
    return arcsec / 3600.0


def solar_position(when_utc: datetime, latitude: float, longitude: float) -> SolarPosition:
    """Return the sun's apparent elevation and azimuth at a site.

    Args:
        when_utc: Timezone-aware instant to evaluate. Naive datetimes are
            rejected — a silent UTC assumption here would shift every slot by
            the site's offset and quietly corrupt every downstream bucket.
        latitude: Site latitude in degrees, positive north.
        longitude: Site longitude in degrees, positive east.

    Returns:
        SolarPosition with refraction-corrected elevation and compass azimuth.

    Raises:
        ValueError: If ``when_utc`` is naive (no tzinfo).
    """
    if when_utc.tzinfo is None:
        raise ValueError("solar_position requires a timezone-aware datetime")

    jc = _julian_century(when_utc)
    declination, eq_time = _sun_declination_and_eq_time(jc)

    utc_minutes = (
        when_utc.hour * 60.0
        + when_utc.minute
        + when_utc.second / 60.0
        + when_utc.microsecond / 60_000_000.0
    )
    true_solar_minutes = (
        utc_minutes + eq_time + _MINUTES_PER_DEGREE_LONGITUDE * longitude
    ) % _MINUTES_PER_DAY

    # Hour angle: 0° at solar noon, negative before, positive after.
    hour_angle = true_solar_minutes / 4.0 - 180.0

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    ha_rad = math.radians(hour_angle)

    # Clamped: rounding can push this a hair outside [-1, 1] at the poles.
    cos_zenith = _clamp(
        math.sin(lat_rad) * math.sin(dec_rad)
        + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad),
        -1.0, 1.0,
    )
    zenith = math.degrees(math.acos(cos_zenith))
    true_elevation = 90.0 - zenith

    sin_zenith = math.sin(math.radians(zenith))
    if abs(sin_zenith) < 1e-9 or abs(math.cos(lat_rad)) < 1e-9:
        # Sun at the exact zenith, or the site is at a pole — azimuth is
        # undefined rather than wrong. Pick due south as a stable placeholder.
        azimuth = 180.0
    else:
        cos_az = _clamp(
            (math.sin(lat_rad) * cos_zenith - math.sin(dec_rad))
            / (math.cos(lat_rad) * sin_zenith),
            -1.0, 1.0,
        )
        azimuth_from_south = math.degrees(math.acos(cos_az))
        azimuth = (
            (azimuth_from_south + 180.0) % 360.0
            if hour_angle > 0
            else (540.0 - azimuth_from_south) % 360.0
        )

    return SolarPosition(
        elevation_deg=true_elevation + _refraction_deg(true_elevation),
        azimuth_deg=azimuth,
    )


def _clamp(value: float, low: float, high: float) -> float:
    """Return ``value`` constrained to the inclusive [low, high] range."""
    return max(low, min(high, value))
