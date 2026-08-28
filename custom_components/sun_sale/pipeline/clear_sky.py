"""Clear-sky reference output and the clear-sky index — pure, no HA.

What this is for
----------------
Accuracy bins keyed on *absolute* generation conflate two unrelated causes: a
slot can be low because the sun is near the horizon, or because it is overcast.
Dividing measured output by what the array *could* have produced under a clear
sky collapses season and time-of-day out of the number, leaving a single
comparable 0–100 % axis — the clear-sky index (CSI).

CSI ≈ 1.0 means "as good as this array gets"; 0.2 means heavy cloud. Crucially,
CSI **above** 1.0 is not noise: it means the array out-produced the clear-sky
model built from its own declared capacity, which is the signature of a
declared capacity that is too small.

Model
-----
Beam irradiance follows the Meinel air-mass attenuation
``DNI = 1367 · 0.7^(AM^0.678)`` with Kasten–Young air mass, projected onto the
array plane by the cosine of the angle of incidence, plus an isotropic diffuse
term. This is a *relative* model: the absolute constants are absorbed into the
fitted ``kwp`` (see ``fit_array_geometry``), so what has to be right is the
model's **shape** against elevation and incidence angle, not its calibration.

Deliberately not modelled: temperature derating, soiling, inverter clipping,
horizon shading. Those are precisely the residuals the CSI buckets exist to
expose — folding them into the reference would hide what we are looking for.
"""
from __future__ import annotations

import math

# Solar constant (W/m²) at the top of the atmosphere.
_SOLAR_CONSTANT_WM2 = 1367.0
# Broadband clear-sky atmospheric transmittance at air mass 1.
_TRANSMITTANCE = 0.7
# Meinel exponent for the air-mass dependence of beam attenuation.
_AIR_MASS_EXPONENT = 0.678
# Isotropic diffuse contribution as a fraction of clear-sky GHI. Real clear-sky
# diffuse is ~10-15% of global; it matters only because it keeps the reference
# non-zero when the beam is edge-on to the array.
_DIFFUSE_FRACTION = 0.10

# Below this elevation the air-mass model degrades badly and output is a
# rounding error anyway — the reference is reported as zero.
MIN_MODELLED_ELEVATION_DEG = 1.0


def air_mass(elevation_deg: float) -> float:
    """Return relative optical air mass at a solar elevation (Kasten–Young).

    The plain ``1/sin(h)`` form diverges at the horizon; Kasten–Young stays
    finite and is accurate to below 1 % down to the horizon.

    Args:
        elevation_deg: Apparent solar elevation in degrees.

    Returns:
        Relative air mass (1.0 at the zenith), or ``math.inf`` below the horizon.
    """
    if elevation_deg <= 0.0:
        return math.inf
    h = elevation_deg
    return float(1.0 / (math.sin(math.radians(h)) + 0.50572 * (h + 6.07995) ** -1.6364))


def clear_sky_dni_wm2(elevation_deg: float) -> float:
    """Return clear-sky direct normal irradiance at a solar elevation.

    Args:
        elevation_deg: Apparent solar elevation in degrees.

    Returns:
        Beam irradiance on a surface normal to the sun (W/m²); 0 below
        ``MIN_MODELLED_ELEVATION_DEG``.
    """
    if elevation_deg < MIN_MODELLED_ELEVATION_DEG:
        return 0.0
    return float(
        _SOLAR_CONSTANT_WM2 * _TRANSMITTANCE ** (air_mass(elevation_deg) ** _AIR_MASS_EXPONENT)
    )


def incidence_cosine(
    solar_elevation_deg: float,
    solar_azimuth_deg: float,
    tilt_deg: float,
    array_azimuth_deg: float,
) -> float:
    """Return the cosine of the angle between the sun and the array normal.

    Args:
        solar_elevation_deg: Apparent solar elevation in degrees.
        solar_azimuth_deg: Solar azimuth, clockwise from true north.
        tilt_deg: Array tilt from horizontal (0 = flat, 90 = vertical).
        array_azimuth_deg: Array facing, clockwise from true north (180 = south).

    Returns:
        cos(AOI), clamped to ≥ 0 — a negative value means the sun is behind the
        panel, which contributes no beam irradiance rather than negative.
    """
    zenith = math.radians(90.0 - solar_elevation_deg)
    tilt = math.radians(tilt_deg)
    delta_az = math.radians(solar_azimuth_deg - array_azimuth_deg)
    cos_aoi = (
        math.cos(zenith) * math.cos(tilt)
        + math.sin(zenith) * math.sin(tilt) * math.cos(delta_az)
    )
    return max(0.0, cos_aoi)


def clear_sky_poa_wm2(
    solar_elevation_deg: float,
    solar_azimuth_deg: float,
    tilt_deg: float,
    array_azimuth_deg: float,
) -> float:
    """Return clear-sky plane-of-array irradiance for a fixed array.

    Args:
        solar_elevation_deg: Apparent solar elevation in degrees.
        solar_azimuth_deg: Solar azimuth, clockwise from true north.
        tilt_deg: Array tilt from horizontal.
        array_azimuth_deg: Array facing, clockwise from true north.

    Returns:
        Irradiance in the array plane (W/m²); 0 when the sun is effectively down.
    """
    if solar_elevation_deg < MIN_MODELLED_ELEVATION_DEG:
        return 0.0
    dni = clear_sky_dni_wm2(solar_elevation_deg)
    beam = dni * incidence_cosine(
        solar_elevation_deg, solar_azimuth_deg, tilt_deg, array_azimuth_deg
    )
    # Isotropic diffuse: a share of clear-sky global, scaled by the fraction of
    # sky the tilted plane still sees.
    ghi = dni * math.sin(math.radians(solar_elevation_deg))
    sky_view = (1.0 + math.cos(math.radians(tilt_deg))) / 2.0
    return beam + _DIFFUSE_FRACTION * ghi * sky_view


def clear_sky_power_kw(
    solar_elevation_deg: float,
    solar_azimuth_deg: float,
    kwp: float,
    tilt_deg: float,
    array_azimuth_deg: float,
) -> float:
    """Return the clear-sky AC output an array of ``kwp`` would produce.

    Args:
        solar_elevation_deg: Apparent solar elevation in degrees.
        solar_azimuth_deg: Solar azimuth, clockwise from true north.
        kwp: Array peak capacity in kW at 1000 W/m² (the fitted effective value,
            which absorbs module efficiency and system losses).
        tilt_deg: Array tilt from horizontal.
        array_azimuth_deg: Array facing, clockwise from true north.

    Returns:
        Expected clear-sky power in kW (≥ 0).
    """
    return kwp * clear_sky_poa_wm2(
        solar_elevation_deg, solar_azimuth_deg, tilt_deg, array_azimuth_deg
    ) / 1000.0


def clear_sky_index(observed_kw: float, clear_sky_kw: float) -> float | None:
    """Return measured output as a fraction of its clear-sky potential.

    Args:
        observed_kw: Measured AC power in kW.
        clear_sky_kw: Modelled clear-sky power in kW for the same instant.

    Returns:
        CSI ≥ 0, where ~1.0 is a cloudless slot and > 1.0 means the array beat
        the model. ``None`` when the reference is too small to divide by — near
        sunrise/sunset the ratio is dominated by model error, and admitting
        those slots would swamp every bucket with noise.
    """
    if clear_sky_kw <= _MIN_REFERENCE_KW:
        return None
    return max(0.0, observed_kw / clear_sky_kw)


# Below this modelled output the CSI denominator is too small to be meaningful.
_MIN_REFERENCE_KW = 0.05


# ---------------------------------------------------------------------------
# Array geometry fitting
# ---------------------------------------------------------------------------

# Coarse grid searched for the array's orientation. Finer steps buy nothing:
# the residual is dominated by weather selection, not by grid resolution.
_TILT_CANDIDATES_DEG = tuple(range(5, 66, 5))
_AZIMUTH_CANDIDATES_DEG = tuple(range(90, 271, 10))

# Only slots with the sun well up are fitted. Low-sun output is where horizon
# clutter, air-mass error and shading all live — fitting through them would
# drag the geometry toward whatever obstructs the site rather than the panels.
_FIT_MIN_ELEVATION_DEG = 20.0

# Clear-sky reference floor below which the ratio is numerically unstable.
_FIT_MIN_REFERENCE_WM2 = 100.0

# Fraction of days, ranked by clearness, admitted to the fit. Fitting on all
# days would let overcast slots pull kwp_eff far below the true capacity.
_CLEAR_DAY_QUANTILE = 0.25

# Minimum evidence before a fit is reported as usable at all.
_MIN_FIT_DAYS = 4
_MIN_FIT_SLOTS = 60

# n_days at which the sample-size half of `confidence` saturates.
_CONFIDENCE_SATURATION_DAYS = 20.0


def _median(values: list[float]) -> float:
    """Return the median of a non-empty list.

    Args:
        values: Values to summarise; must not be empty.

    Returns:
        The median value.
    """
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _clearness_score(day_slots: list[tuple[float, float, float]]) -> float:
    """Return a geometry-independent clearness proxy for one day.

    Ranks days without needing the array orientation the fit is still trying to
    determine: measured energy over the day divided by the summed sine of solar
    elevation, which is proportional to clear-sky global irradiance on a
    horizontal plane. Cloudy days score low regardless of season.

    Args:
        day_slots: Tuples of (elevation_deg, azimuth_deg, power_kw) for the day.

    Returns:
        Clearness proxy; 0.0 when the day has no usable daylight.
    """
    denominator = sum(
        math.sin(math.radians(e)) for e, _a, _p in day_slots if e > 0.0
    )
    if denominator <= 0.0:
        return 0.0
    return sum(p for _e, _a, p in day_slots) / denominator


def fit_array_geometry(
    days: dict[str, list[tuple[float, float, float]]],
) -> tuple[float, float, float, float, int, int] | None:
    """Fit effective capacity, tilt and azimuth from measured clear-day output.

    Grid-searches orientation and, for each candidate, forms the ratio of
    measured power to modelled plane-of-array irradiance across the clear-day
    slots. For the *correct* orientation that ratio is the array's effective
    capacity and is near-constant all day; for a wrong one it drifts with the
    sun, so the spread of the ratio is the objective to minimise.

    Only the clearest ``_CLEAR_DAY_QUANTILE`` of days are fitted: the model
    describes a cloudless sky, so cloudy slots are not evidence about geometry
    and would drag the capacity estimate down.

    Args:
        days: Mapping of local ISO date → list of (elevation_deg, azimuth_deg,
            power_kw) tuples for that day's slots.

    Returns:
        Tuple of (kwp_eff, tilt_deg, azimuth_deg, fit_quality, n_days, n_slots),
        or ``None`` when there is not enough clear-weather evidence to fit.
        ``fit_quality`` is ``1 - spread`` and may be negative for a hopeless fit.
    """
    if not days:
        return None

    ranked = sorted(days.items(), key=lambda kv: _clearness_score(kv[1]), reverse=True)
    keep = max(_MIN_FIT_DAYS, int(len(ranked) * _CLEAR_DAY_QUANTILE))
    clear_days = ranked[:keep]
    if len(clear_days) < _MIN_FIT_DAYS:
        return None

    samples = [
        (elevation, azimuth, power)
        for _date, slots in clear_days
        for elevation, azimuth, power in slots
        if elevation >= _FIT_MIN_ELEVATION_DEG and power > 0.0
    ]
    if len(samples) < _MIN_FIT_SLOTS:
        return None

    best: tuple[float, float, float, float] | None = None   # (spread, kwp, tilt, az)
    for tilt in _TILT_CANDIDATES_DEG:
        for array_azimuth in _AZIMUTH_CANDIDATES_DEG:
            ratios = []
            for elevation, azimuth, power in samples:
                poa = clear_sky_poa_wm2(elevation, azimuth, float(tilt), float(array_azimuth))
                if poa >= _FIT_MIN_REFERENCE_WM2:
                    ratios.append(power / (poa / 1000.0))
            if len(ratios) < _MIN_FIT_SLOTS:
                continue
            centre = _median(ratios)
            if centre <= 0.0:
                continue
            spread = sum(abs(r - centre) for r in ratios) / len(ratios) / centre
            if best is None or spread < best[0]:
                best = (spread, centre, float(tilt), float(array_azimuth))

    if best is None:
        return None

    spread, kwp_eff, tilt_deg, azimuth_deg = best
    return kwp_eff, tilt_deg, azimuth_deg, 1.0 - spread, len(clear_days), len(samples)


def fit_confidence(fit_quality: float, n_days: int) -> float:
    """Return a 0–1 readiness score for acting on a calibration.

    Combines how well the geometry explains the data with how much data there
    is. Both matter independently: a tight fit over three days is luck, and
    a year of loose fit is a wrong model measured patiently.

    Args:
        fit_quality: ``1 - relative spread`` from ``fit_array_geometry``.
        n_days: Number of clear days contributing to the fit.

    Returns:
        Confidence in [0, 1].
    """
    quality = max(0.0, min(1.0, fit_quality))
    coverage = min(1.0, max(0, n_days) / _CONFIDENCE_SATURATION_DAYS)
    return round(quality * coverage, 4)
