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
Three groups of quality buckets, updated each DAG cycle:

  Group 1 (intensity):  every matched slot bucketed by its forecasted kWh
                        magnitude (100 Wh bins for 15-min, 500 Wh for 1-h).
                        Answers "how accurate are we when we predict X Wh?"

  Group 2 (position):   first and last N slots of each solar day (dawn/dusk
                        transitions). 20 positional buckets for 15-min slots,
                        6 for 1-h. Bucket #1 = sunrise slot, #20 = sunset slot.
                        Answers "how does accuracy vary across the solar day?"

  Group 3 (horizon):    day-ahead forecast accuracy by horizon (d0–d6).
                        Each day, today's forecasted daily totals are saved as
                        pending; when the target day ends (observed data
                        available) the error is committed to the bucket.
                        Answers "does accuracy degrade with forecast horizon?"

All metrics use α=0.1 EMA so quality accumulates over months without
re-reading historical data, and persists across HA restarts via the
STORAGE_KEY_FORECAST_QUALITY store.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import tzinfo  # pragma: no cover

from ..contract.models import (
    AccuracyBucketState,
    ForecastAccuracyResult,
    ForecastErrorSeries,
    ForecastErrorSlot,
    ForecastQualityStore,
    GenerationSeries,
    ObservedGenerationSeries,
    SunTimes,
)

_EMA_ALPHA = 0.1

# Group 1 bin widths in kWh
_G1_BIN_15MIN_KWH = 0.1   # 100 Wh
_G1_BIN_1H_KWH    = 0.5   # 500 Wh

# Group 2 number of dawn/dusk slots per side
_G2_N_15MIN = 10
_G2_N_1H    = 3

_RES_15MIN_S = 900
_RES_1H_S    = 3600


# ---------------------------------------------------------------------------
# Error series
# ---------------------------------------------------------------------------


def build_forecast_error_series(
    forecast: GenerationSeries,
    observed: ObservedGenerationSeries,
    now: datetime | None = None,
) -> ForecastErrorSeries:
    """Align forecast and observed slots by (start, end) and compute error statistics.

    Unmatched forecast slots (no corresponding observation) receive sentinel
    values of -1.0 so the chart can render "no data yet" for future slots.

    Args:
        forecast: GenerationSeries from the forecast pipeline stage.
        observed: ObservedGenerationSeries built from inverter today-total samples.
        now: Cycle timestamp; defaults to UTC now.

    Returns:
        ForecastErrorSeries with per-slot deltas and aggregate MAE/bias/MAPE.
        Returns an all-zero series when forecast is empty.
    """
    if now is None:
        now = datetime.now(timezone.utc)

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
        slots.append(ForecastErrorSlot(
            start=fc.start,
            end=fc.end,
            forecast_kwh=round(fc.expected_kwh, 6),
            observed_kwh=round(obs_kwh, 6),
            error_kwh=round(err, 6),
            relative_error=round(rel, 6) if rel is not None else None,
        ))
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
# Group 1 helpers
# ---------------------------------------------------------------------------


def _g1_bin_key(forecast_kwh: float, res_s: int) -> str:
    """Return the bin key (bin start in whole Wh) for a forecast value.

    Args:
        forecast_kwh: Forecasted slot generation.
        res_s: Slot resolution in seconds (determines bin width).

    Returns:
        String key representing the bin start in Wh (e.g. "500" for 501–600 Wh
        with 15-min resolution).
    """
    bin_kwh = _G1_BIN_15MIN_KWH if res_s <= _RES_15MIN_S else _G1_BIN_1H_KWH
    bin_start_kwh = int(forecast_kwh / bin_kwh) * bin_kwh
    return str(round(bin_start_kwh * 1000))


# ---------------------------------------------------------------------------
# Group 2 helpers
# ---------------------------------------------------------------------------


def _floor_to_slot(dt: datetime, res_s: int) -> datetime:
    """Floor dt to the nearest preceding slot boundary.

    Args:
        dt: Datetime to floor.
        res_s: Slot resolution in seconds.

    Returns:
        UTC datetime aligned to the slot boundary.
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    ts = int(aware.timestamp())
    slot_ts = (ts // res_s) * res_s
    return datetime.fromtimestamp(slot_ts, tz=timezone.utc)


def _g2_position(
    slot_start: datetime,
    sunrise: datetime,
    sunset: datetime,
    res_s: int,
) -> int | None:
    """Return the Group 2 positional bucket (1-based) for a slot, or None.

    Positions #1..#N_DAWN are dawn (sunrise slot first).
    Positions #(N_DAWN+1)..#(N_DAWN+N_DUSK) are dusk (sunset slot last).

    Args:
        slot_start: UTC start of the slot being classified.
        sunrise: Approximate UTC sunrise for that day.
        sunset: Approximate UTC sunset for that day.
        res_s: Slot resolution in seconds.

    Returns:
        Integer position 1–20 (15 min) or 1–6 (1 h), or None if outside windows.
    """
    n = _G2_N_15MIN if res_s <= _RES_15MIN_S else _G2_N_1H

    sunrise_slot = _floor_to_slot(sunrise, res_s)
    dawn_offset = int((slot_start.astimezone(timezone.utc) - sunrise_slot).total_seconds() / res_s)
    if 0 <= dawn_offset < n:
        return dawn_offset + 1

    sunset_slot = _floor_to_slot(sunset, res_s)
    dusk_offset = int((sunset_slot - slot_start.astimezone(timezone.utc)).total_seconds() / res_s)
    if 0 <= dusk_offset < n:
        # sunset slot → position n*2, one before → n*2-1, ..., (n-1)th before → n+1
        return n * 2 - dusk_offset

    return None


# ---------------------------------------------------------------------------
# EMA update
# ---------------------------------------------------------------------------


def _update_bucket(
    bucket: AccuracyBucketState,
    error_kwh: float,
    forecast_kwh: float,
    observed_kwh: float,
) -> None:
    """Update bucket EMA state with one new (forecast, observed) observation.

    Uses α=0.1: new = old×0.9 + sample×0.1. First sample initialises
    directly so the EMA doesn't start at 0 for long.

    Args:
        bucket: Mutable state object to update in-place.
        error_kwh: observed_kwh - forecast_kwh.
        forecast_kwh: Forecasted generation (kWh).
        observed_kwh: Measured generation (kWh).
    """
    a = _EMA_ALPHA
    rel = abs(error_kwh) / forecast_kwh if forecast_kwh > 1e-9 else 0.0

    if bucket.n == 0:
        bucket.ema_error     = error_kwh
        bucket.ema_abs_error = abs(error_kwh)
        bucket.ema_sq_error  = error_kwh ** 2
        bucket.ema_rel_error = rel
        bucket.ema_obs       = observed_kwh
        bucket.ema_obs_sq    = observed_kwh ** 2
    else:
        bucket.ema_error     = bucket.ema_error     * (1 - a) + error_kwh * a
        bucket.ema_abs_error = bucket.ema_abs_error * (1 - a) + abs(error_kwh) * a
        bucket.ema_sq_error  = bucket.ema_sq_error  * (1 - a) + error_kwh ** 2 * a
        bucket.ema_rel_error = bucket.ema_rel_error * (1 - a) + rel * a
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
        "ema_rel_error": round(b.ema_rel_error,  8),
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
        ema_rel_error = float(d.get("ema_rel_error",  0.0)),
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
        "group1":          {k: _bucket_to_dict(v) for k, v in store.group1.items()},
        "group2":          {k: _bucket_to_dict(v) for k, v in store.group2.items()},
        "group3":          {k: _bucket_to_dict(v) for k, v in store.group3.items()},
        "group3_pending":  list(store.group3_pending),
    }


def store_from_dict(d: dict) -> ForecastQualityStore:
    """Deserialise a ForecastQualityStore from a stored dict.

    Args:
        d: Dict from HA's Store.async_load().

    Returns:
        Populated ForecastQualityStore; missing sections default to empty.
    """
    return ForecastQualityStore(
        group1         = {k: _bucket_from_dict(v) for k, v in d.get("group1", {}).items()},
        group2         = {k: _bucket_from_dict(v) for k, v in d.get("group2", {}).items()},
        group3         = {k: _bucket_from_dict(v) for k, v in d.get("group3", {}).items()},
        group3_pending = list(d.get("group3_pending", [])),
    )


# ---------------------------------------------------------------------------
# Quality update
# ---------------------------------------------------------------------------


def _update_quality(
    error_series: ForecastErrorSeries,
    generation: GenerationSeries,
    observed: ObservedGenerationSeries,
    sun_times: SunTimes | None,
    store: ForecastQualityStore,
    local_tz: tzinfo,
    now: datetime,
) -> ForecastQualityStore:
    """Update all three quality groups from this cycle's pipeline data.

    Mutates store in-place and returns it.

    Args:
        error_series: Per-slot forecast vs. observed error data.
        generation: Forecast series with daily totals for d0–d6.
        observed: Observed generation series with yesterday/today totals.
        sun_times: Approximate sunrise/sunset for today; may be None.
        store: Mutable quality store to update.
        local_tz: HA local timezone for date boundary calculations.
        now: Current cycle UTC timestamp.

    Returns:
        The mutated store (same object).
    """
    if not error_series.slots:
        return store

    res_s = _resolution_s(error_series)
    local_now       = now.astimezone(local_tz)
    local_today     = local_now.date()
    local_yesterday = local_today - timedelta(days=1)

    today_sr = sun_times.today_sunrise if sun_times else None
    today_ss = sun_times.today_sunset  if sun_times else None
    yest_sr  = (today_sr - timedelta(days=1)) if today_sr else None
    yest_ss  = (today_ss - timedelta(days=1)) if today_ss else None

    # --- Groups 1 & 2: process each matched slot ---
    for slot in error_series.slots:
        if slot.observed_kwh < 0:
            continue  # -1 sentinel = no observed data yet

        err = slot.error_kwh
        fc  = slot.forecast_kwh
        obs = slot.observed_kwh

        _update_bucket(_get_or_create(store.group1, _g1_bin_key(fc, res_s)), err, fc, obs)

        slot_date = slot.start.astimezone(local_tz).date()
        if slot_date == local_today:
            sr, ss = today_sr, today_ss
        elif slot_date == local_yesterday:
            sr, ss = yest_sr, yest_ss
        else:
            sr = ss = None

        if sr is not None and ss is not None:
            pos = _g2_position(slot.start, sr, ss, res_s)
            if pos is not None:
                _update_bucket(_get_or_create(store.group2, str(pos)), err, fc, obs)

    # --- Group 3: upsert today's day-ahead forecasts into pending ---
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
        for i, e in enumerate(store.group3_pending)
    }
    for horizon, fc_total in day_totals.items():
        target_date = (local_today + timedelta(days=horizon)).isoformat()
        idx = pending_index.get((target_date, horizon))
        if idx is not None:
            store.group3_pending[idx]["forecast_kwh"] = round(fc_total, 4)
        else:
            store.group3_pending.append({
                "target_date":   target_date,
                "horizon":       horizon,
                "forecast_kwh":  round(fc_total, 4),
            })

    # --- Group 3: resolve pending entries whose target day is now yesterday ---
    yesterday_actual = observed.total_yesterday_kwh
    remaining: list[dict] = []
    for entry in store.group3_pending:
        td = entry.get("target_date", "")
        h  = entry.get("horizon")
        fc_total = float(entry.get("forecast_kwh", 0.0))

        if td == local_yesterday.isoformat() and h is not None:
            err = yesterday_actual - fc_total
            _update_bucket(_get_or_create(store.group3, str(h)), err, fc_total, yesterday_actual)
        else:
            remaining.append(entry)

    store.group3_pending = remaining
    return store


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def build_forecast_accuracy_result(
    forecast: GenerationSeries,
    observed: ObservedGenerationSeries,
    quality_store: ForecastQualityStore | None,
    sun_times: SunTimes | None,
    local_tz: tzinfo,
    now: datetime | None = None,
) -> ForecastAccuracyResult:
    """Build per-slot error series and update EMA quality buckets in one pass.

    Args:
        forecast: GenerationSeries from the forecast pipeline stage.
        observed: ObservedGenerationSeries built from inverter today-total samples.
        quality_store: Persistent EMA store from primary; a fresh store is used
            when None (first cycle after install).
        sun_times: Today's approximate sunrise/sunset for Group 2 bucketing.
        local_tz: HA local timezone for date boundary calculations.
        now: Cycle timestamp; defaults to UTC now.

    Returns:
        ForecastAccuracyResult containing the error series and the updated
        quality store (same object as quality_store when provided).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    error_series = build_forecast_error_series(forecast, observed, now)
    store = quality_store if quality_store is not None else ForecastQualityStore()
    _update_quality(error_series, forecast, observed, sun_times, store, local_tz, now)
    return ForecastAccuracyResult(error_series=error_series, quality=store)
