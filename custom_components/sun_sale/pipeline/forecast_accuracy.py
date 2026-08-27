"""Forecast accuracy and EMA-based quality tracking.

Per-slot delta between forecast and observed generation
------------------------------------------------------
Aligns `GenerationSeries` (forecast) with `ObservedGenerationSeries` (measured
from the inverter today-total counter) on identical `(start, end)` slots and
emits a `ForecastErrorSeries`. Both inputs are already resampled onto the
PriceSeries grid, so the alignment is a direct slot-by-slot match.

Sign convention: `error_kwh = observed - forecast` — positive means the
forecast under-predicted, negative means it over-predicted.

EMA quality metrics
-------------------
Four axes of quality buckets. The three per-slot axes are keyed on *physical*
coordinates, not on raw magnitude or slot index: binning by absolute forecast
kWh conflates "low because it is dawn" with "low because it is overcast", which
are unrelated failure modes with unrelated fixes.

  Clear-sky index:  measured output as a fraction of what the array could have
                    produced under a cloudless sky (see ``clear_sky.py``), in
                    10% bins. Season and time-of-day divide out, leaving one
                    comparable 0-100% axis. The top bin is an **overflow** for
                    everything at or above 100%: a well-populated overflow means
                    the array beat the clear-sky model built from its own
                    declared capacity — the signature of an under-declared kWp.
                    Requires an ``ArrayCalibration`` for the denominator.

  Solar elevation:  5-degree bands. Isolates air-mass and low-sun model error.

  Solar azimuth:    15-degree bands, only above ``_AZIMUTH_MIN_ELEVATION_DEG``.
                    A fixed obstruction — tree, chimney, neighbouring roof —
                    shows up as a persistent deficit in specific bearings.

  Horizon:          day-ahead accuracy by horizon (d0-d6). Each day's forecast
                    daily totals are saved as pending; when the target day ends
                    the error is committed. Note the DP plans only ~32 h ahead
                    (bounded by the price series), so only d0/d1 can influence a
                    decision — d2-d6 are informational.

All metrics use an alpha=0.1 EMA and persist across restarts via the
STORAGE_KEY_FORECAST_QUALITY store. That only accumulates a long-run statistic
because each slot is ingested **once**: the DAG rebuilds the same ~3-day error
window every coordinator cycle, so ``last_ingested_slot_utc`` gates the per-slot
axes. Without it a slot is absorbed ~288x/day and the EMA's effective memory
collapses from months to minutes. The horizon axis needs no watermark — it
commits once per (target_date, horizon) by construction.

MAPE is deliberately not tracked; see ``AccuracyBucketState.metrics``.

Curtailment censoring
---------------------
Observed generation is read from the inverter's daily counter, which reads
*below* the panels' potential whenever PV is clipped — and clipping happens in
no-export regimes (``_NO_EXPORT_MODES``) when surplus can't be exported and the
battery has no headroom. That shortfall is irreducible noise: it depends on
instantaneous load/SoC the generation forecast never models. So a slot that ran
under a no-export mode (per the persisted ``InverterModeHistory``) *and*
under-generated past ``_CURTAILMENT_NOISE_KWH`` is marked ``censored`` — kept in
the per-slot series for the chart but excluded from the aggregate statistics and
from every per-slot EMA bucket. A whole day with any censored slot is dropped
from the horizon buckets, since one clipped slot depresses the daily total.
Positive errors are never censored — curtailment cannot manufacture energy.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import tzinfo  # pragma: no cover

from .clear_sky import (
    MIN_MODELLED_ELEVATION_DEG,
    clear_sky_index,
    clear_sky_power_kw,
)
from .solar_geometry import solar_position
from ..contract.models import (
    FORECAST_QUALITY_STORE_VERSION,
    AccuracyBucketState,
    ForecastAccuracyResult,
    ForecastErrorSeries,
    ForecastErrorSlot,
    ForecastQualityStore,
    GenerationSeries,
    ArrayCalibration,
    InverterModeHistory,
    ObservedGenerationSeries,
    StorageMode,
)

_EMA_ALPHA = 0.1

# Re-exported for callers/tests that reason about the persisted schema; the
# constant itself lives with the dataclass it versions.
STORE_VERSION = FORECAST_QUALITY_STORE_VERSION

# --- Curtailment censoring ---
# Modes in which the inverter cannot shed surplus PV to the grid: when solar
# exceeds local load and the battery has no headroom, the panels are throttled
# (clipped), so the generation counter reads below the physical potential. A
# slot that ran under one of these regimes *and* under-generated is treated as
# curtailment-suspect and censored from the accuracy estimate — the shortfall
# is almost certainly clipping, not forecast error, and clipping depends on
# instantaneous load/SoC the generation forecast never models.
_NO_EXPORT_MODES: frozenset[StorageMode] = frozenset(
    {StorageMode.NoExport, StorageMode.StandBy}
)

# Only censor when the slot under-generated by more than this floor. Small
# negative errors are ordinary forecast noise, not clipping; a positive error
# is never clipping (you cannot curtail your way to *more* energy).
_CURTAILMENT_NOISE_KWH = 0.05

# --- Physical bucket axes ---
# Clear-sky-index bins, in whole percent. The top bin is an overflow that
# collects everything at or above 100%: slots where the array beat the
# clear-sky model built from its own declared capacity.
_CSI_BIN_PCT = 10
_CSI_OVERFLOW_KEY = "100"

# Solar-elevation band width. 5 deg is fine enough to separate the low-sun
# regime (where air mass dominates) from the plateau around noon.
_ELEVATION_BIN_DEG = 5

# Solar-azimuth band width. 15 deg ~ 1 hour of the sun's apparent travel, which
# is about the angular size of the obstructions this axis exists to reveal.
_AZIMUTH_BIN_DEG = 15

# Azimuth buckets are only meaningful once the sun is clear of the horizon:
# below this, refraction, horizon clutter and model error swamp any real
# bearing-specific shading signal.
_AZIMUTH_MIN_ELEVATION_DEG = 10.0

_RES_15MIN_S = 900
_RES_1H_S    = 3600


# ---------------------------------------------------------------------------
# Curtailment / no-export regime lookup
# ---------------------------------------------------------------------------


def _no_export_active_during(
    history: InverterModeHistory | None,
    start: datetime,
    end: datetime,
) -> bool:
    """Return whether a no-export inverter regime was active during a slot.

    The history is a sparse stream of mode-change events (sorted ascending).
    A slot is flagged when the mode active at its ``start`` is a no-export
    mode, or when any change *within* the slot switches into one — being
    conservative, since even a partial no-export window clips the slot total.

    Args:
        history: Persisted mode-change history, or ``None`` when unavailable.
        start: UTC slot start.
        end: UTC slot end.

    Returns:
        True if a no-export mode covered any part of ``[start, end)``.
    """
    if history is None or not history.samples:
        return False

    active = None  # last mode change at or before `start`
    for change in history.samples:
        if change.timestamp <= start:
            active = change.mode
        elif change.timestamp < end:
            if change.mode in _NO_EXPORT_MODES:
                return True
        else:
            break
    return active in _NO_EXPORT_MODES


# ---------------------------------------------------------------------------
# Error series
# ---------------------------------------------------------------------------


def build_forecast_error_series(
    forecast: GenerationSeries,
    observed: ObservedGenerationSeries,
    now: datetime | None = None,
    mode_history: InverterModeHistory | None = None,
) -> ForecastErrorSeries:
    """Align forecast and observed slots by (start, end) and compute error statistics.

    Unmatched forecast slots (no corresponding observation) receive sentinel
    values of -1.0 so the chart can render "no data yet" for future slots.

    Slots that ran under a no-export regime and under-generated are marked
    ``censored`` (see ``_NO_EXPORT_MODES``): their observed/error values are
    kept for the chart but excluded from the aggregate statistics, because the
    shortfall is almost certainly PV curtailment rather than forecast error.

    Args:
        forecast: GenerationSeries from the forecast pipeline stage.
        observed: ObservedGenerationSeries built from inverter today-total samples.
        now: Cycle timestamp; defaults to UTC now.
        mode_history: Persisted inverter mode-change history covering the
            graded window (yesterday 00:00 → now); ``None`` disables censoring.

    Returns:
        ForecastErrorSeries with per-slot deltas and aggregate MAE/bias/MAPE.
        Returns an all-zero series when forecast is empty.
    """
    if now is None:
        now = datetime.now(UTC)

    if not forecast.slots:
        return _empty(now)

    observed_by_key = {(s.start, s.end): s.generated_kwh for s in observed.slots}

    slots: list[ForecastErrorSlot] = []
    total_forecast = 0.0
    total_observed = 0.0
    abs_error_sum = 0.0
    matched = 0
    for fc in forecast.slots:
        obs_kwh = observed_by_key.get((fc.start, fc.end))
        if obs_kwh is None:
            slots.append(ForecastErrorSlot(
                start=fc.start,
                end=fc.end,
                forecast_kwh=round(fc.expected_kwh, 6),
                observed_kwh=-1.0,
                error_kwh=-1.0,
                relative_error=None,
            ))
            continue
        err = obs_kwh - fc.expected_kwh
        rel = err / fc.expected_kwh if fc.expected_kwh != 0.0 else None
        censored = (
            err < -_CURTAILMENT_NOISE_KWH
            and _no_export_active_during(mode_history, fc.start, fc.end)
        )
        slots.append(ForecastErrorSlot(
            start=fc.start,
            end=fc.end,
            forecast_kwh=round(fc.expected_kwh, 6),
            observed_kwh=round(obs_kwh, 6),
            error_kwh=round(err, 6),
            relative_error=round(rel, 6) if rel is not None else None,
            censored=censored,
        ))
        if censored:
            continue  # untrustworthy: keep for display, drop from statistics
        total_forecast += fc.expected_kwh
        total_observed += obs_kwh
        abs_error_sum += abs(err)
        matched += 1

    if matched == 0:
        return ForecastErrorSeries(
            slots=tuple(slots),
            total_forecast_kwh=round(sum(s.expected_kwh for s in forecast.slots), 4),
            total_observed_kwh=-1.0,
            total_error_kwh=-1.0,
            mean_absolute_error_kwh=-1.0,
            bias_kwh=-1.0,
            mean_absolute_percentage_error=None,
            computed_at=now,
            matched_slot_count=0,
        )

    total_error = total_observed - total_forecast
    mae = abs_error_sum / matched
    bias = total_error / matched
    mape = abs_error_sum / total_forecast if total_forecast > 0 else None

    return ForecastErrorSeries(
        slots=tuple(slots),
        total_forecast_kwh=round(total_forecast, 4),
        total_observed_kwh=round(total_observed, 4),
        total_error_kwh=round(total_error, 4),
        mean_absolute_error_kwh=round(mae, 6),
        bias_kwh=round(bias, 6),
        mean_absolute_percentage_error=round(mape, 6) if mape is not None else None,
        computed_at=now,
        matched_slot_count=matched,
    )


def _empty(now: datetime) -> ForecastErrorSeries:
    """Return an all-zero ForecastErrorSeries sentinel for when no forecast exists.

    Args:
        now: Cycle timestamp stamped into computed_at.

    Returns:
        ForecastErrorSeries with empty slot tuple and all numeric fields zero.
    """
    return ForecastErrorSeries(
        slots=(),
        total_forecast_kwh=0.0,
        total_observed_kwh=0.0,
        total_error_kwh=0.0,
        mean_absolute_error_kwh=0.0,
        bias_kwh=0.0,
        mean_absolute_percentage_error=None,
        computed_at=now,
    )


# ---------------------------------------------------------------------------
# Resolution detection
# ---------------------------------------------------------------------------


def _resolution_s(series: ForecastErrorSeries) -> int:
    """Detect slot resolution in seconds from the first error slot with data.

    Args:
        series: Error series whose slots are inspected.

    Returns:
        900 for 15-min slots, 3600 for 1-h slots; defaults to 900.
    """
    for s in series.slots:
        if s.observed_kwh >= 0:
            dur = int((s.end - s.start).total_seconds())
            return _RES_15MIN_S if abs(dur - _RES_15MIN_S) < 60 else _RES_1H_S
    return _RES_15MIN_S


# ---------------------------------------------------------------------------
# Physical bucket axes
# ---------------------------------------------------------------------------


def _slot_midpoint(slot: ForecastErrorSlot) -> datetime:
    """Return the UTC midpoint of a slot.

    The sun moves ~3.75 deg of azimuth across a 15-min slot, so classifying by
    the slot *start* would bias every bearing bucket half a slot early.

    Args:
        slot: The error slot to locate in time.

    Returns:
        Timezone-aware UTC midpoint.
    """
    start = slot.start.astimezone(UTC)
    return start + (slot.end.astimezone(UTC) - start) / 2


def _csi_bin_key(csi: float) -> str:
    """Return the clear-sky-index bucket key for a slot, in whole percent.

    Args:
        csi: Clear-sky index (1.0 = matched the clear-sky reference).

    Returns:
        Bin start in percent as a string. Everything at or above 100% lands in
        the single overflow bucket: the distinction between 130% and 180% is
        model error, but "above 100% at all" is the signal worth counting.
    """
    pct = int(max(0.0, csi) * 100)
    if pct >= 100:
        return _CSI_OVERFLOW_KEY
    return str((pct // _CSI_BIN_PCT) * _CSI_BIN_PCT)


def _elevation_bin_key(elevation_deg: float) -> str:
    """Return the solar-elevation bucket key, as the band start in degrees.

    Args:
        elevation_deg: Apparent solar elevation.

    Returns:
        Band start in whole degrees as a string.
    """
    band = int(max(0.0, elevation_deg) // _ELEVATION_BIN_DEG) * _ELEVATION_BIN_DEG
    return str(band)


def _azimuth_bin_key(azimuth_deg: float) -> str:
    """Return the solar-azimuth bucket key, as the band start in degrees.

    Args:
        azimuth_deg: Solar azimuth, clockwise from true north.

    Returns:
        Band start in whole degrees as a string (0-345 in 15 deg steps).
    """
    band = int(azimuth_deg % 360.0 // _AZIMUTH_BIN_DEG) * _AZIMUTH_BIN_DEG
    return str(band)


# ---------------------------------------------------------------------------
# EMA update
# ---------------------------------------------------------------------------


def _update_bucket(
    bucket: AccuracyBucketState,
    error_kwh: float,
    observed_kwh: float,
) -> None:
    """Update bucket EMA state with one new (forecast, observed) observation.

    Uses α=0.1: new = old×0.9 + sample×0.1. First sample initialises
    directly so the EMA doesn't start at 0 for long.

    Args:
        bucket: Mutable state object to update in-place.
        error_kwh: observed_kwh - forecast_kwh.
        observed_kwh: Measured generation (kWh).
    """
    a = _EMA_ALPHA

    if bucket.n == 0:
        bucket.ema_error     = error_kwh
        bucket.ema_abs_error = abs(error_kwh)
        bucket.ema_sq_error  = error_kwh ** 2
        bucket.ema_obs       = observed_kwh
        bucket.ema_obs_sq    = observed_kwh ** 2
    else:
        bucket.ema_error     = bucket.ema_error     * (1 - a) + error_kwh * a
        bucket.ema_abs_error = bucket.ema_abs_error * (1 - a) + abs(error_kwh) * a
        bucket.ema_sq_error  = bucket.ema_sq_error  * (1 - a) + error_kwh ** 2 * a
        bucket.ema_obs       = bucket.ema_obs       * (1 - a) + observed_kwh * a
        bucket.ema_obs_sq    = bucket.ema_obs_sq    * (1 - a) + observed_kwh ** 2 * a

    bucket.n += 1


def _get_or_create(buckets: dict[str, AccuracyBucketState], key: str) -> AccuracyBucketState:
    """Return the bucket for key, creating a fresh one if absent.

    Args:
        buckets: The bucket dict to look up or insert into.
        key: String key for the bucket.

    Returns:
        Existing or newly created AccuracyBucketState.
    """
    if key not in buckets:
        buckets[key] = AccuracyBucketState()
    return buckets[key]


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _bucket_to_dict(b: AccuracyBucketState) -> dict:
    """Serialise an AccuracyBucketState to a JSON-safe dict.

    Args:
        b: Bucket state to serialise.

    Returns:
        Dict with all EMA fields and sample count.
    """
    return {
        "ema_error":     round(b.ema_error,     8),
        "ema_abs_error": round(b.ema_abs_error,  8),
        "ema_sq_error":  round(b.ema_sq_error,   8),
        "ema_obs":       round(b.ema_obs,         8),
        "ema_obs_sq":    round(b.ema_obs_sq,      8),
        "n":             b.n,
    }


def _bucket_from_dict(d: dict) -> AccuracyBucketState:
    """Deserialise an AccuracyBucketState from a stored dict.

    Args:
        d: Stored dict (from async_load or equivalent).

    Returns:
        Populated AccuracyBucketState; unknown keys are ignored.
    """
    return AccuracyBucketState(
        ema_error     = float(d.get("ema_error",     0.0)),
        ema_abs_error = float(d.get("ema_abs_error",  0.0)),
        ema_sq_error  = float(d.get("ema_sq_error",   0.0)),
        ema_obs       = float(d.get("ema_obs",         0.0)),
        ema_obs_sq    = float(d.get("ema_obs_sq",      0.0)),
        n             = int(d.get("n", 0)),
    )


def store_to_dict(store: ForecastQualityStore) -> dict:
    """Serialise a ForecastQualityStore to a JSON-safe dict for persistence.

    Args:
        store: Quality store to serialise.

    Returns:
        Dict suitable for HA's Store.async_save().
    """
    return {
        "csi_bins":        {k: _bucket_to_dict(v) for k, v in store.csi_bins.items()},
        "elevation_bins":  {k: _bucket_to_dict(v) for k, v in store.elevation_bins.items()},
        "azimuth_bins":    {k: _bucket_to_dict(v) for k, v in store.azimuth_bins.items()},
        "horizon":         {k: _bucket_to_dict(v) for k, v in store.horizon.items()},
        "horizon_pending": list(store.horizon_pending),
        "last_ingested_slot_utc": store.last_ingested_slot_utc,
        "version":         STORE_VERSION,
    }


def store_from_dict(d: dict) -> ForecastQualityStore:
    """Deserialise a ForecastQualityStore from a stored dict.

    Payloads below ``STORE_VERSION`` are migrated lossily. Two independent
    reasons force this, and neither is recoverable after the fact:

    * The pre-watermark per-slot buckets absorbed every slot once per
      coordinator cycle (~288x/day) instead of once, so their EMAs describe the
      last few minutes rather than the long run.
    * Those buckets were keyed on absolute forecast kWh and slot-within-day
      position. The current axes are clear-sky index, solar elevation and solar
      azimuth — different quantities, so the old counts cannot be re-keyed.

    The horizon buckets are preserved across every version: they commit once
    per (target_date, horizon) regardless, and their day-ahead totals mean the
    same thing under both schemas. Legacy payloads carry them under "group3".

    Args:
        d: Dict from HA's Store.async_load().

    Returns:
        Populated ForecastQualityStore; missing sections default to empty.
    """
    stored_version = int(d.get("version", 0) or 0)
    legacy = stored_version < STORE_VERSION
    horizon_raw = d.get("horizon") if not legacy else d.get("group3", {})
    pending_raw = d.get("horizon_pending") if not legacy else d.get("group3_pending", [])

    def _buckets(key: str) -> dict[str, AccuracyBucketState]:
        """Load one bucket group, or nothing at all when migrating from legacy."""
        if legacy:
            return {}
        return {k: _bucket_from_dict(v) for k, v in (d.get(key) or {}).items()}

    return ForecastQualityStore(
        csi_bins       = _buckets("csi_bins"),
        elevation_bins = _buckets("elevation_bins"),
        azimuth_bins   = _buckets("azimuth_bins"),
        horizon        = {k: _bucket_from_dict(v) for k, v in (horizon_raw or {}).items()},
        horizon_pending = list(pending_raw or []),
        last_ingested_slot_utc = None if legacy else d.get("last_ingested_slot_utc"),
        version        = STORE_VERSION,
    )


# ---------------------------------------------------------------------------
# Quality update
# ---------------------------------------------------------------------------


def _update_quality(
    error_series: ForecastErrorSeries,
    generation: GenerationSeries,
    observed: ObservedGenerationSeries,
    store: ForecastQualityStore,
    local_tz: tzinfo,
    now: datetime,
    latitude: float | None,
    longitude: float | None,
    calibration: ArrayCalibration | None,
) -> ForecastQualityStore:
    """Update all three quality groups from this cycle's pipeline data.

    Mutates store in-place and returns it.

    Args:
        error_series: Per-slot forecast vs. observed error data.
        generation: Forecast series with daily totals for d0–d6.
        observed: Observed generation series with yesterday/today totals.
        store: Mutable quality store to update.
        local_tz: HA local timezone for date boundary calculations.
        now: Current cycle UTC timestamp.
        latitude: Site latitude; ``None`` disables every per-slot axis, since
            all three are functions of solar position.
        longitude: Site longitude; see ``latitude``.
        calibration: Fitted array geometry supplying the clear-sky denominator.
            ``None`` leaves the CSI buckets empty rather than filling them
            against a guessed capacity.

    Returns:
        The mutated store (same object).
    """
    if not error_series.slots:
        return store

    local_now       = now.astimezone(local_tz)
    local_today     = local_now.date()
    local_yesterday = local_today - timedelta(days=1)

    # Local dates that contained a curtailment-suspect slot — used to drop the
    # whole day from the day-ahead horizon totals (a single clipped slot
    # depresses the daily total, which has no clean per-slot censoring).
    censored_dates: set[date] = {
        s.start.astimezone(local_tz).date()
        for s in error_series.slots
        if s.censored
    }

    # --- Per-slot axes: clear-sky index, solar elevation, solar azimuth ---
    # The error window spans ~3 days and is rebuilt every coordinator cycle, so
    # only slots strictly newer than the watermark may be ingested — otherwise
    # the same slot lands in its bucket once per cycle (~288x/day) and the EMA
    # stops being a long-run statistic. The horizon buckets are exempt: they
    # commit once per (target_date, horizon) via the pending list below.
    watermark = store.last_ingested_slot_utc
    newest_ingested = watermark
    have_site = latitude is not None and longitude is not None

    for slot in error_series.slots:
        if slot.observed_kwh < 0:
            continue  # -1 sentinel = no observed data yet
        slot_key = slot.start.astimezone(UTC).isoformat()
        if watermark is not None and slot_key <= watermark:
            continue  # already absorbed on an earlier cycle
        # Advance the mark even for censored slots: they are a deliberate skip,
        # not a deferral, and leaving them behind the mark would re-test them
        # (and re-admit them if the mode history later changes) every cycle.
        if newest_ingested is None or slot_key > newest_ingested:
            newest_ingested = slot_key
        if slot.censored:
            continue  # curtailment-suspect = untrustworthy potential-generation
        if not have_site:
            continue  # every remaining axis is a function of solar position

        err = slot.error_kwh
        obs = slot.observed_kwh

        position = solar_position(_slot_midpoint(slot), latitude, longitude)
        if position.elevation_deg < MIN_MODELLED_ELEVATION_DEG:
            continue  # night: no clear-sky reference, nothing to say about it

        _update_bucket(
            _get_or_create(store.elevation_bins, _elevation_bin_key(position.elevation_deg)),
            err, obs,
        )

        # Azimuth is about fixed obstructions, which are only distinguishable
        # from ordinary low-sun losses once the sun is well clear of the horizon.
        if position.elevation_deg >= _AZIMUTH_MIN_ELEVATION_DEG:
            _update_bucket(
                _get_or_create(store.azimuth_bins, _azimuth_bin_key(position.azimuth_deg)),
                err, obs,
            )

        # The clear-sky axis needs a calibrated array to divide by. Without one
        # there is no denominator, so those buckets simply stay empty rather
        # than being filled with a guessed capacity.
        if calibration is not None:
            slot_hours = (slot.end - slot.start).total_seconds() / 3600.0
            if slot_hours > 0:
                reference_kw = clear_sky_power_kw(
                    position.elevation_deg, position.azimuth_deg,
                    calibration.kwp_eff, calibration.tilt_deg, calibration.azimuth_deg,
                )
                csi = clear_sky_index(obs / slot_hours, reference_kw)
                if csi is not None:
                    _update_bucket(
                        _get_or_create(store.csi_bins, _csi_bin_key(csi)), err, obs,
                    )

    store.last_ingested_slot_utc = newest_ingested

    # --- Horizon: upsert today's day-ahead forecasts into pending ---
    day_totals: dict[int, float] = {
        0: generation.total_today_kwh,
        1: generation.total_tomorrow_kwh,
        2: generation.total_d2_kwh,
        3: generation.total_d3_kwh,
        4: generation.total_d4_kwh,
        5: generation.total_d5_kwh,
        6: generation.total_d6_kwh,
    }
    pending_index: dict[tuple[str, int], int] = {
        (e.get("target_date", ""), e.get("horizon", -1)): i
        for i, e in enumerate(store.horizon_pending)
    }
    for horizon, fc_total in day_totals.items():
        target_date = (local_today + timedelta(days=horizon)).isoformat()
        idx = pending_index.get((target_date, horizon))
        if idx is not None:
            store.horizon_pending[idx]["forecast_kwh"] = round(fc_total, 4)
        else:
            store.horizon_pending.append({
                "target_date":   target_date,
                "horizon":       horizon,
                "forecast_kwh":  round(fc_total, 4),
            })

    # --- Horizon: resolve pending entries whose target day is now yesterday ---
    yesterday_actual = observed.total_yesterday_kwh
    remaining: list[dict] = []
    for entry in store.horizon_pending:
        td = entry.get("target_date", "")
        h  = entry.get("horizon")
        fc_total = float(entry.get("forecast_kwh", 0.0))

        if td == local_yesterday.isoformat() and h is not None:
            # Drop the entry either way; only commit it to the horizon bucket
            # when yesterday had no curtailment-suspect slot, otherwise its
            # daily total under-reads true generation and would poison d0–d6.
            if local_yesterday not in censored_dates:
                err = yesterday_actual - fc_total
                _update_bucket(_get_or_create(store.horizon, str(h)), err, yesterday_actual)
        else:
            remaining.append(entry)

    store.horizon_pending = remaining
    return store


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def build_forecast_accuracy_result(
    forecast: GenerationSeries,
    observed: ObservedGenerationSeries,
    quality_store: ForecastQualityStore | None,
    local_tz: tzinfo,
    now: datetime | None = None,
    mode_history: InverterModeHistory | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    calibration: ArrayCalibration | None = None,
) -> ForecastAccuracyResult:
    """Build per-slot error series and update EMA quality buckets in one pass.

    Args:
        forecast: GenerationSeries from the forecast pipeline stage.
        observed: ObservedGenerationSeries built from inverter today-total samples.
        quality_store: Persistent EMA store from primary; a fresh store is used
            when None (first cycle after install).
        local_tz: HA local timezone for date boundary calculations.
        now: Cycle timestamp; defaults to UTC now.
        mode_history: Persisted inverter mode-change history; lets the error
            series censor curtailment-suspect no-export slots so they don't
            poison the quality buckets. ``None`` disables censoring.
        latitude: Site latitude, from ``hass.config``. ``None`` leaves every
            per-slot bucket empty — all three axes derive from solar position.
        longitude: Site longitude; see ``latitude``.
        calibration: Fitted array geometry supplying the clear-sky denominator.

    Returns:
        ForecastAccuracyResult containing the error series and the updated
        quality store (same object as quality_store when provided).
    """
    if now is None:
        now = datetime.now(UTC)

    error_series = build_forecast_error_series(forecast, observed, now, mode_history)
    store = quality_store if quality_store is not None else ForecastQualityStore()
    _update_quality(
        error_series, forecast, observed, store, local_tz, now,
        latitude, longitude, calibration,
    )
    return ForecastAccuracyResult(error_series=error_series, quality=store)


# ---------------------------------------------------------------------------
# Forecast uncertainty → planning reserve
# ---------------------------------------------------------------------------

# Horizons the DP can actually act on. The price series caps planning at ~32 h,
# so a reserve sized from d2+ would be hedging a forecast that never reaches
# the planner.
_RESERVE_HORIZONS = ("0", "1")

# Minimum committed days before a horizon bucket's spread is treated as a
# statistic rather than an accident.
_RESERVE_MIN_SAMPLES = 10

# Multiple of the day-ahead RMSE held back. 1.0 covers a typical bad day
# without permanently sterilising a large share of the battery.
_RESERVE_SIGMA_MULTIPLE = 1.0

# Hard ceiling on the reserve as a fraction of usable capacity. Without it a
# forecast this poor (RMSE ~25 kWh against a ~26 kWh pack) would reserve the
# entire battery and the planner would never trade at all.
_RESERVE_MAX_FRACTION = 0.25


def forecast_reserve_soc(
    store: ForecastQualityStore | None,
    usable_capacity_kwh: float,
) -> float | None:
    """Return the SoC fraction to hold back against day-ahead forecast error.

    The DP discharges the battery *now* on the strength of solar it expects
    *later*. When that solar does not arrive the battery is empty and the
    shortfall is bought at peak. Reserving roughly one standard deviation of
    measured day-ahead error bounds that exposure without pretending the
    forecast is better than it is.

    Sized from the *committed* horizon buckets (one sample per day) rather than
    the per-slot axes: the exposure being hedged is a whole day's generation
    coming in low, which is exactly what those buckets measure.

    Args:
        store: Persistent quality store holding the horizon buckets.
        usable_capacity_kwh: Battery capacity the reserve is expressed against.

    Returns:
        Reserve as a SoC fraction in [0, ``_RESERVE_MAX_FRACTION``], or ``None``
        when there is not yet enough committed history to size one.
    """
    if store is None or usable_capacity_kwh <= 0:
        return None

    spreads = [
        bucket.metrics()["rmse_wh"] / 1000.0
        for key in _RESERVE_HORIZONS
        if (bucket := store.horizon.get(key)) is not None
        and bucket.n >= _RESERVE_MIN_SAMPLES
        and bucket.metrics()["rmse_wh"] is not None
    ]
    if not spreads:
        return None

    reserve_kwh = max(spreads) * _RESERVE_SIGMA_MULTIPLE
    return min(_RESERVE_MAX_FRACTION, reserve_kwh / usable_capacity_kwh)
