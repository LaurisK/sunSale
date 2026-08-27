"""Fit the PV array's effective geometry, and compare it against the forecast.

Two questions are answered here, both from data the install already has:

1. **What does this array actually do under a clear sky?** Needed as the
   denominator of the clear-sky index — without it the CSI buckets have nothing
   to divide by.

2. **Is the forecast provider configured with the right array?** The forecast's
   own output, run through the *same* fitted geometry, implies a capacity. If
   that differs materially from the measured one, the array declared to the
   forecast provider is the wrong size, and no runtime bias correction will fix
   that as cleanly as editing the provider's configuration once.

Both sides are deliberately expressed as an *effective* kW: the clear-sky model
is relative, so its absolute constants cancel when the same model is applied to
measured and forecast output alike. That cancellation is what makes
``correction_factor`` trustworthy even though neither capacity is a nameplate
rating.
"""
from __future__ import annotations

from datetime import datetime

from ..contract.models import (
    ArrayCalibration,
    BakedObservedHistory,
    GenerationSeries,
)
from .clear_sky import clear_sky_poa_wm2, fit_array_geometry, fit_confidence
from .solar_geometry import solar_position

# Generation is the only side with a clear-sky model; grid flows are not solar.
_GENERATION_SIDE_ID = "generation"

# Slots below this elevation are excluded from the forecast-capacity estimate
# for the same reason they are excluded from the fit: low-sun output is
# dominated by horizon effects rather than by array capacity.
_MIN_ELEVATION_DEG = 20.0
_MIN_REFERENCE_WM2 = 100.0

# The forecast includes cloud, so its *typical* ratio understates the capacity
# it was configured with. A high percentile approximates the clear-sky days in
# the window, which is the comparable quantity.
_FORECAST_CAPACITY_PERCENTILE = 0.90
_MIN_FORECAST_SLOTS = 12


def _slots_by_day(
    history: BakedObservedHistory,
    latitude: float,
    longitude: float,
) -> dict[str, list[tuple[float, float, float]]]:
    """Group baked generation slots by local date, tagged with solar position.

    Args:
        history: Persisted baked day records across all sides.
        latitude: Site latitude in degrees.
        longitude: Site longitude in degrees.

    Returns:
        Mapping of local ISO date → list of (elevation_deg, azimuth_deg,
        power_kw) for that day's daylight slots.
    """
    days: dict[str, list[tuple[float, float, float]]] = {}
    for record in history.records:
        if record.side_id != _GENERATION_SIDE_ID:
            continue
        samples: list[tuple[float, float, float]] = []
        for slot in record.baked_slots:
            hours = (slot.end - slot.start).total_seconds() / 3600.0
            if hours <= 0:
                continue
            midpoint = slot.start + (slot.end - slot.start) / 2
            position = solar_position(midpoint, latitude, longitude)
            if position.elevation_deg > 0.0:
                samples.append(
                    (position.elevation_deg, position.azimuth_deg, slot.kwh / hours)
                )
        if samples:
            days[record.date_str] = samples
    return days


def _implied_forecast_kwp(
    forecast: GenerationSeries,
    latitude: float,
    longitude: float,
    tilt_deg: float,
    azimuth_deg: float,
) -> float | None:
    """Return the array capacity implied by the forecast's own output.

    Runs the forecast through the *same* clear-sky model and geometry as the
    measured fit, so the two capacities are directly comparable and the model's
    absolute calibration cancels between them.

    Args:
        forecast: Forecast generation series (covers ~yesterday to d6).
        latitude: Site latitude in degrees.
        longitude: Site longitude in degrees.
        tilt_deg: Fitted array tilt.
        azimuth_deg: Fitted array azimuth.

    Returns:
        Implied capacity in kW, or ``None`` when the window holds too few
        high-sun slots to estimate from.
    """
    ratios: list[float] = []
    for slot in forecast.slots:
        hours = (slot.end - slot.start).total_seconds() / 3600.0
        if hours <= 0 or slot.expected_kwh <= 0:
            continue
        midpoint = slot.start + (slot.end - slot.start) / 2
        position = solar_position(midpoint, latitude, longitude)
        if position.elevation_deg < _MIN_ELEVATION_DEG:
            continue
        poa = clear_sky_poa_wm2(
            position.elevation_deg, position.azimuth_deg, tilt_deg, azimuth_deg
        )
        if poa < _MIN_REFERENCE_WM2:
            continue
        ratios.append((slot.expected_kwh / hours) / (poa / 1000.0))

    if len(ratios) < _MIN_FORECAST_SLOTS:
        return None
    ratios.sort()
    index = min(len(ratios) - 1, int(len(ratios) * _FORECAST_CAPACITY_PERCENTILE))
    return ratios[index]


def build_array_calibration(
    history: BakedObservedHistory,
    forecast: GenerationSeries,
    latitude: float,
    longitude: float,
    now: datetime,
) -> ArrayCalibration | None:
    """Fit array geometry from baked history and compare it to the forecast.

    Args:
        history: Persisted baked generation history (the measured evidence).
        forecast: Current forecast series, used only to derive the capacity the
            forecast provider appears to be configured with.
        latitude: Site latitude in degrees.
        longitude: Site longitude in degrees.
        now: Cycle timestamp, stamped onto the result.

    Returns:
        The fitted ArrayCalibration, or ``None`` when there is not yet enough
        clear-weather history to fit one.
    """
    days = _slots_by_day(history, latitude, longitude)
    fit = fit_array_geometry(days)
    if fit is None:
        return None

    kwp_eff, tilt_deg, azimuth_deg, fit_quality, n_days, n_slots = fit
    implied = _implied_forecast_kwp(forecast, latitude, longitude, tilt_deg, azimuth_deg)
    correction = (kwp_eff / implied) if implied else None

    return ArrayCalibration(
        kwp_eff=kwp_eff,
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        fit_quality=fit_quality,
        n_days=n_days,
        n_slots=n_slots,
        implied_forecast_kwp=implied,
        correction_factor=correction,
        confidence=fit_confidence(fit_quality, n_days),
        computed_at=now,
    )
