"""Tests for pipeline/forecast_accuracy.py — pure Python, no HA required."""
from datetime import UTC, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    AccuracyBucketState,
    ArrayCalibration,
    ForecastQualityStore,
    GenerationSeries,
    GenerationSlot,
    InverterModeChange,
    InverterModeHistory,
    ObservedGenerationSeries,
    ObservedGenerationSlot,
    StorageMode,
)
from custom_components.sun_sale.pipeline.forecast_accuracy import (
    _CSI_OVERFLOW_KEY,
    STORE_VERSION,
    _azimuth_bin_key,
    _csi_bin_key,
    _elevation_bin_key,
    build_forecast_accuracy_result,
    build_forecast_error_series,
    forecast_reserve_soc,
    store_from_dict,
    store_to_dict,
)
from custom_components.sun_sale.pipeline.solar_geometry import solar_position
from tests.conftest import BASE_DT

NOW = BASE_DT
UTC = UTC

# Real deployment site. BASE_DT is 2024-01-15, when the sun at this latitude
# peaks around 14 deg — above both the clear-sky floor and the azimuth cutoff,
# so the hour-10/11 fixture slots land in real buckets.
SITE_LAT = 54.9110344
SITE_LON = 23.9253644


def _history(*changes: tuple[int, StorageMode]) -> InverterModeHistory:
    """Build a mode history from (hour-offset-from-NOW, mode) pairs."""
    return InverterModeHistory(samples=tuple(
        InverterModeChange(timestamp=NOW + timedelta(hours=h), mode=m, raw_state=0)
        for h, m in changes
    ))


def _fc_slot(hour: int, kwh: float) -> GenerationSlot:
    start = NOW + timedelta(hours=hour)
    return GenerationSlot(
        start=start,
        end=start + timedelta(hours=1),
        expected_kwh=kwh,
    )


def _obs_slot(hour: int, kwh: float) -> ObservedGenerationSlot:
    start = NOW + timedelta(hours=hour)
    return ObservedGenerationSlot(
        start=start,
        end=start + timedelta(hours=1),
        generated_kwh=kwh,
        source="inverter",
    )


def _forecast(*slots: GenerationSlot) -> GenerationSeries:
    return GenerationSeries(slots=tuple(slots))


def _observed(*slots: ObservedGenerationSlot) -> ObservedGenerationSeries:
    return ObservedGenerationSeries(slots=tuple(slots), computed_at=NOW)


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------

def test_empty_inputs_yield_empty_series():
    result = build_forecast_error_series(_forecast(), _observed(), now=NOW)
    assert result.slots == ()
    assert result.total_forecast_kwh == 0.0
    assert result.total_observed_kwh == 0.0
    assert result.mean_absolute_error_kwh == 0.0
    assert result.bias_kwh == 0.0
    assert result.mean_absolute_percentage_error is None


def test_empty_observed_yields_pending_sentinels():
    """Forecast present but observation history not yet recovered: one -1
    sentinel slot per forecast slot, totals also -1, MAPE remains None."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)), _observed(), now=NOW,
    )
    assert len(result.slots) == 1
    s = result.slots[0]
    assert s.forecast_kwh == 2.0
    assert s.observed_kwh == -1.0
    assert s.error_kwh == -1.0
    assert s.relative_error is None
    assert result.total_forecast_kwh == 2.0
    assert result.total_observed_kwh == -1.0
    assert result.total_error_kwh == -1.0
    assert result.mean_absolute_error_kwh == -1.0
    assert result.bias_kwh == -1.0
    assert result.mean_absolute_percentage_error is None


def test_no_overlap_yields_pending_sentinels():
    """Forecast covers hour 10, observed covers hour 5 — no matching slots.
    Per-slot contract: every forecast slot emits, observed-side fields = -1."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(5, 1.0)),
        now=NOW,
    )
    assert len(result.slots) == 1
    s = result.slots[0]
    assert s.forecast_kwh == 2.0
    assert s.observed_kwh == -1.0
    assert s.error_kwh == -1.0
    assert s.relative_error is None
    # No matches → totals collapse to sentinels (kept distinct from "all zero").
    assert result.total_forecast_kwh == 2.0
    assert result.total_observed_kwh == -1.0
    assert result.total_error_kwh == -1.0


# ---------------------------------------------------------------------------
# Error math
# ---------------------------------------------------------------------------

def test_single_slot_perfect_forecast():
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 2.0)),
        now=NOW,
    )
    assert len(result.slots) == 1
    s = result.slots[0]
    assert s.forecast_kwh == 2.0
    assert s.observed_kwh == 2.0
    assert s.error_kwh == 0.0
    assert s.relative_error == 0.0
    assert result.mean_absolute_error_kwh == 0.0
    assert result.bias_kwh == 0.0
    assert result.mean_absolute_percentage_error == 0.0


def test_under_forecast_yields_positive_error():
    """Observed (3.0) > forecast (2.0) → error = +1.0."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 3.0)),
        now=NOW,
    )
    s = result.slots[0]
    assert s.error_kwh == 1.0
    assert s.relative_error == 0.5
    assert result.bias_kwh == 1.0
    assert result.total_error_kwh == 1.0


def test_over_forecast_yields_negative_error():
    """Observed (0.5) < forecast (2.0) → error = -1.5."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 0.5)),
        now=NOW,
    )
    s = result.slots[0]
    assert s.error_kwh == -1.5
    assert s.relative_error == -0.75
    assert result.bias_kwh == -1.5


def test_mae_bias_mape_across_multiple_slots():
    """Forecast 2,2,2 vs observed 3,1,2 → errors +1,-1,0.

    MAE = (1+1+0)/3 = 2/3
    Bias = (1-1+0)/3 = 0
    MAPE = (1+1+0)/6 = 1/3
    """
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0), _fc_slot(11, 2.0), _fc_slot(12, 2.0)),
        _observed(_obs_slot(10, 3.0), _obs_slot(11, 1.0), _obs_slot(12, 2.0)),
        now=NOW,
    )
    assert len(result.slots) == 3
    assert result.total_forecast_kwh == 6.0
    assert result.total_observed_kwh == 6.0
    assert result.total_error_kwh == 0.0
    assert abs(result.mean_absolute_error_kwh - 2 / 3) < 1e-6
    assert result.bias_kwh == 0.0
    assert abs(result.mean_absolute_percentage_error - 1 / 3) < 1e-6


def test_partial_overlap_emits_sentinel_for_unmatched_slots():
    """Forecast 10,11,12; observed 10,11 only. Hour 12 emits the -1 sentinel;
    hours 10/11 have real errors. Totals aggregate matched slots only."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0), _fc_slot(11, 2.0), _fc_slot(12, 2.0)),
        _observed(_obs_slot(10, 2.0), _obs_slot(11, 1.0)),
        now=NOW,
    )
    assert len(result.slots) == 3
    assert result.slots[2].observed_kwh == -1.0
    assert result.slots[2].error_kwh == -1.0
    assert result.slots[2].relative_error is None
    # Totals reflect only the matched (real) slots.
    assert result.total_forecast_kwh == 4.0
    assert result.total_observed_kwh == 3.0
    assert result.total_error_kwh == -1.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_zero_forecast_relative_error_is_none():
    """Avoid division by zero when forecast is exactly 0."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 0.0)),
        _observed(_obs_slot(10, 1.5)),
        now=NOW,
    )
    s = result.slots[0]
    assert s.error_kwh == 1.5
    assert s.relative_error is None
    # MAPE is also None when total_forecast == 0
    assert result.mean_absolute_percentage_error is None


# ---------------------------------------------------------------------------
# Curtailment censoring
# ---------------------------------------------------------------------------

def test_no_export_undergeneration_is_censored():
    """NoExport active + observed << forecast → slot censored, dropped from stats."""
    # NoExport switched on at hour 9, still active over the hour-10 slot.
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 0.5)),
        now=NOW,
        mode_history=_history((9, StorageMode.NoExport)),
    )
    s = result.slots[0]
    assert s.censored is True
    assert s.observed_kwh == 0.5      # kept for the chart
    assert s.error_kwh == -1.5
    # Excluded from aggregate stats → matched==0 sentinels.
    assert result.total_observed_kwh == -1.0
    assert result.mean_absolute_error_kwh == -1.0


def test_no_export_overgeneration_not_censored():
    """Curtailment cannot manufacture energy: a positive error stays trusted."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 3.0)),
        now=NOW,
        mode_history=_history((9, StorageMode.NoExport)),
    )
    s = result.slots[0]
    assert s.censored is False
    assert result.bias_kwh == 1.0


def test_export_mode_undergeneration_not_censored():
    """Under-generation while exporting (SelfUse) is a real forecast miss."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 0.5)),
        now=NOW,
        mode_history=_history((9, StorageMode.SelfUse)),
    )
    assert result.slots[0].censored is False
    assert result.bias_kwh == -1.5


def test_small_undergeneration_below_noise_not_censored():
    """A tiny shortfall in a no-export slot is forecast noise, not clipping."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 1.98)),   # -0.02 kWh, under the 0.05 floor
        now=NOW,
        mode_history=_history((9, StorageMode.NoExport)),
    )
    assert result.slots[0].censored is False


def test_mode_change_mid_slot_is_conservative():
    """A no-export window covering only part of the slot still censors it."""
    # SelfUse at start, flips to NoExport 30 min into the hour-10 slot.
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 0.5)),
        now=NOW,
        mode_history=_history(
            (9, StorageMode.SelfUse),
            (10.5, StorageMode.NoExport),
        ),
    )
    assert result.slots[0].censored is True


def test_no_history_disables_censoring():
    """Without mode history, behaviour is unchanged (no censoring)."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 2.0)),
        _observed(_obs_slot(10, 0.5)),
        now=NOW,
    )
    assert result.slots[0].censored is False
    assert result.bias_kwh == -1.5


# ---------------------------------------------------------------------------
# Censoring propagates into the EMA quality buckets
# ---------------------------------------------------------------------------

def test_censored_slot_skips_per_slot_buckets():
    """A censored slot must not update any per-slot quality bucket."""
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 0.5)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.NoExport)),
    )
    assert res.error_series.slots[0].censored is True
    assert res.quality.elevation_bins == {}
    assert res.quality.azimuth_bins == {}


def test_uncensored_slot_updates_per_slot_buckets():
    """Control: an export-mode slot still feeds the per-slot buckets."""
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 0.5)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.SelfUse)),
    )
    assert res.error_series.slots[0].censored is False
    assert res.quality.elevation_bins != {}


def test_horizon_skips_whole_day_with_a_censored_slot():
    """A pending day-ahead total is dropped, not committed, when yesterday
    had any curtailment-suspect slot (its daily total under-reads truth)."""
    # NOW = 2024-01-15 00:00 UTC → yesterday = 2024-01-14. Slot at hour -10
    # (2024-01-14 14:00) ran under NoExport and under-generated → censored.
    store = ForecastQualityStore(horizon_pending=[
        {"target_date": "2024-01-14", "horizon": 1, "forecast_kwh": 10.0},
    ])
    observed = ObservedGenerationSeries(
        slots=(_obs_slot(-10, 0.5),), computed_at=NOW, total_yesterday_kwh=5.0,
    )
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(-10, 2.0)),
        observed=observed,
        quality_store=store,
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((-11, StorageMode.NoExport)),
    )
    # Resolved (removed from pending) but never committed to the horizon bucket.
    assert "1" not in res.quality.horizon
    assert all(
        e["target_date"] != "2024-01-14" for e in res.quality.horizon_pending
    )


def test_zero_forecast_mixed_with_nonzero():
    """Slots with zero forecast contribute to MAE but not to MAPE denominator
    in a way that triggers division-by-zero — MAPE uses total_forecast."""
    result = build_forecast_error_series(
        _forecast(_fc_slot(10, 0.0), _fc_slot(11, 2.0)),
        _observed(_obs_slot(10, 0.5), _obs_slot(11, 1.0)),
        now=NOW,
    )
    # MAE = (0.5 + 1.0) / 2 = 0.75
    assert abs(result.mean_absolute_error_kwh - 0.75) < 1e-6
    # MAPE = (0.5 + 1.0) / 2.0 = 0.75
    assert abs(result.mean_absolute_percentage_error - 0.75) < 1e-6


# ---------------------------------------------------------------------------
# Ingestion watermark (store v1)
# ---------------------------------------------------------------------------


def _accuracy(store: ForecastQualityStore, *, now=NOW):
    """Run one accuracy cycle over a fixed single-slot window against `store`."""
    return build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 1.5)),
        quality_store=store,
        local_tz=UTC,
        now=now,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.SelfUse)),
    )


def test_repeated_cycles_ingest_each_slot_once():
    """The same slot re-presented on later cycles must not re-enter its bucket.

    The DAG rebuilds the ~3-day error window every coordinator tick, so without
    a watermark a slot is absorbed ~288×/day and the EMA's effective memory
    collapses from months to minutes.
    """
    store = ForecastQualityStore()
    first = _accuracy(store)
    counts_after_first = {k: b.n for k, b in first.quality.elevation_bins.items()}
    assert counts_after_first, "control: the slot must land in a bucket at all"

    for _ in range(5):
        store = _accuracy(store).quality

    assert {k: b.n for k, b in store.elevation_bins.items()} == counts_after_first


def test_watermark_advances_to_newest_matched_slot():
    """The watermark records the newest ingested slot so later cycles can skip it."""
    res = _accuracy(ForecastQualityStore())
    assert res.quality.last_ingested_slot_utc == (NOW + timedelta(hours=10)).isoformat()


def test_new_slot_past_watermark_is_still_ingested():
    """A genuinely new slot must be absorbed even though older ones are skipped."""
    store = _accuracy(ForecastQualityStore()).quality
    before = sum(b.n for b in store.elevation_bins.values())

    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0), _fc_slot(11, 2.0)),
        observed=_observed(_obs_slot(10, 1.5), _obs_slot(11, 1.5)),
        quality_store=store,
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.SelfUse)),
    )

    assert sum(b.n for b in res.quality.elevation_bins.values()) == before + 1
    assert res.quality.last_ingested_slot_utc == (NOW + timedelta(hours=11)).isoformat()


def test_censored_slot_still_advances_the_watermark():
    """A censored slot is a deliberate skip, not a deferral — it must not be
    re-examined every subsequent cycle."""
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 0.5)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.NoExport)),
    )
    assert res.error_series.slots[0].censored is True
    assert res.quality.elevation_bins == {}
    assert res.quality.azimuth_bins == {}
    assert res.quality.last_ingested_slot_utc == (NOW + timedelta(hours=10)).isoformat()


def test_pending_sentinel_slots_do_not_move_the_watermark():
    """Unmatched (-1 sentinel) slots carry no observation, so they must leave
    the watermark alone — otherwise future slots are skipped once they arrive."""
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0), _fc_slot(11, 2.0)),
        observed=_observed(_obs_slot(10, 1.5)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.SelfUse)),
    )
    assert res.error_series.slots[1].observed_kwh == -1.0
    assert res.quality.last_ingested_slot_utc == (NOW + timedelta(hours=10)).isoformat()


# ---------------------------------------------------------------------------
# Store round-trip and legacy migration
# ---------------------------------------------------------------------------


def _legacy_payload() -> dict:
    """A pre-v2 persisted payload: legacy axes, no version, no watermark."""
    bucket = {"ema_error": 0.3, "ema_abs_error": 0.3, "ema_sq_error": 0.09,
              "ema_rel_error": 0.15, "ema_obs": 1.5, "ema_obs_sq": 2.25,
              "n": 1259298}
    return {
        "group1": {"0": dict(bucket)},
        "group2": {"1": dict(bucket)},
        "group3": {"0": dict(bucket, n=53)},
        # Legacy key name — the migration must still find the pending records.
        "group3_pending": [{"target_date": "2026-08-28", "horizon": 1,
                            "forecast_kwh": 47.16}],
    }


def test_legacy_store_drops_per_slot_axes_but_keeps_horizon():
    """Legacy per-slot buckets are dropped; the horizon buckets survive.

    Two independent reasons make the old per-slot state unusable: it was
    accumulated without an ingestion watermark (so it describes minutes, not
    months), and it was keyed on absolute forecast kWh / slot position rather
    than on the physical axes used now — different quantities, so it cannot be
    re-keyed. The horizon buckets commit once per day under both schemas and
    carry the same meaning, so they must be preserved across the migration.
    """
    store = store_from_dict(_legacy_payload())

    assert store.csi_bins == {}
    assert store.elevation_bins == {}
    assert store.azimuth_bins == {}
    assert store.horizon["0"].n == 53
    assert store.horizon_pending == [
        {"target_date": "2026-08-28", "horizon": 1, "forecast_kwh": 47.16}
    ]
    assert store.last_ingested_slot_utc is None
    assert store.version == STORE_VERSION


def test_current_store_round_trips_without_loss():
    """A v1 payload keeps every group and its watermark across save/load."""
    original = _accuracy(ForecastQualityStore()).quality
    restored = store_from_dict(store_to_dict(original))

    assert {k: b.n for k, b in restored.elevation_bins.items()} == \
           {k: b.n for k, b in original.elevation_bins.items()}
    assert restored.last_ingested_slot_utc == original.last_ingested_slot_utc
    assert restored.version == STORE_VERSION


def test_reloaded_store_does_not_re_ingest_old_slots():
    """The watermark must survive persistence — an HA restart is otherwise a
    free pass to re-absorb the whole window."""
    store = store_from_dict(store_to_dict(_accuracy(ForecastQualityStore()).quality))
    counts = {k: b.n for k, b in store.elevation_bins.items()}

    reloaded = _accuracy(store).quality

    assert {k: b.n for k, b in reloaded.elevation_bins.items()} == counts


# ---------------------------------------------------------------------------
# Physical bucket axes
# ---------------------------------------------------------------------------


def test_csi_bin_keys_are_ten_percent_bands():
    """CSI bins step in whole 10% bands keyed by their lower edge."""
    assert _csi_bin_key(0.0) == "0"
    assert _csi_bin_key(0.05) == "0"
    assert _csi_bin_key(0.10) == "10"
    assert _csi_bin_key(0.55) == "50"
    assert _csi_bin_key(0.999) == "90"


def test_csi_overflow_bucket_collects_everything_at_or_above_full_sun():
    """CSI ≥ 100% all lands in one overflow bucket.

    The difference between 130% and 180% is model error, but "over 100% at all"
    is the signature of an under-declared array capacity — so it is counted as
    one signal rather than smeared across bins.
    """
    assert _csi_bin_key(1.0) == _CSI_OVERFLOW_KEY
    assert _csi_bin_key(1.3) == _CSI_OVERFLOW_KEY
    assert _csi_bin_key(9.9) == _CSI_OVERFLOW_KEY


def test_elevation_bin_keys_are_five_degree_bands():
    """Elevation bins step in 5° bands keyed by their lower edge."""
    assert _elevation_bin_key(0.0) == "0"
    assert _elevation_bin_key(4.9) == "0"
    assert _elevation_bin_key(5.0) == "5"
    assert _elevation_bin_key(47.2) == "45"


def test_azimuth_bin_keys_are_fifteen_degree_bands_and_wrap():
    """Azimuth bins step in 15° bands and wrap at due north."""
    assert _azimuth_bin_key(0.0) == "0"
    assert _azimuth_bin_key(14.9) == "0"
    assert _azimuth_bin_key(180.0) == "180"
    assert _azimuth_bin_key(359.9) == "345"
    assert _azimuth_bin_key(360.0) == "0"
    assert _azimuth_bin_key(-1.0) == "345"


def test_no_site_coordinates_leaves_every_per_slot_axis_empty():
    """Without lat/lon there is no solar position, so no per-slot axis is valid.

    Filling buckets from a guessed location would be worse than leaving them
    empty — the values would look authoritative and be wrong.
    """
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 1.5)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=None,
        longitude=None,
        mode_history=_history((9, StorageMode.SelfUse)),
    )
    assert res.quality.csi_bins == {}
    assert res.quality.elevation_bins == {}
    assert res.quality.azimuth_bins == {}


def test_csi_buckets_stay_empty_without_a_calibration():
    """The CSI axis needs a fitted array for its denominator.

    The elevation axis still fills — it needs only solar position — which is
    what makes the CSI buckets' emptiness diagnostic rather than ambiguous.
    """
    res = _accuracy(ForecastQualityStore())
    assert res.quality.csi_bins == {}
    assert res.quality.elevation_bins != {}


def test_csi_buckets_fill_once_a_calibration_is_supplied():
    """With a calibration present the clear-sky axis starts accumulating."""
    calibration = ArrayCalibration(
        kwp_eff=7.4, tilt_deg=35.0, azimuth_deg=180.0,
        fit_quality=0.9, n_days=10, n_slots=400, confidence=0.9,
    )
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 1.5)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((9, StorageMode.SelfUse)),
        calibration=calibration,
    )
    assert res.quality.csi_bins != {}


def test_azimuth_axis_ignores_low_sun_but_elevation_axis_does_not():
    """Below the azimuth cutoff a slot feeds elevation only.

    Bearing-specific shading cannot be told apart from ordinary low-sun losses
    near the horizon, so admitting those slots would manufacture phantom
    obstructions in the dawn and dusk bearings.
    """
    # 2024-01-15 08:00 UTC at this site: sun is up but only a few degrees.
    low_sun = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(8, 0.4)),
        observed=_observed(_obs_slot(8, 0.3)),
        quality_store=ForecastQualityStore(),
        local_tz=UTC,
        now=NOW,
        latitude=SITE_LAT,
        longitude=SITE_LON,
        mode_history=_history((7, StorageMode.SelfUse)),
    )
    position = solar_position(NOW + timedelta(hours=8, minutes=30), SITE_LAT, SITE_LON)
    assert 0 < position.elevation_deg < 10.0, "fixture must sit below the azimuth cutoff"
    assert low_sun.quality.elevation_bins != {}
    assert low_sun.quality.azimuth_bins == {}


# ---------------------------------------------------------------------------
# Forecast uncertainty → planning reserve
# ---------------------------------------------------------------------------


def _horizon_store(**rmse_by_horizon_and_n) -> ForecastQualityStore:
    """Build a store whose horizon buckets have given RMSE (kWh) and sample count."""
    horizon = {}
    for key, (rmse_kwh, n) in rmse_by_horizon_and_n.items():
        horizon[key.lstrip("d")] = AccuracyBucketState(ema_sq_error=rmse_kwh ** 2, n=n)
    return ForecastQualityStore(horizon=horizon)


def test_reserve_is_none_without_committed_history():
    """No horizon samples means no measured spread to size a reserve from."""
    assert forecast_reserve_soc(ForecastQualityStore(), 26.0) is None
    assert forecast_reserve_soc(None, 26.0) is None


def test_reserve_is_none_below_the_sample_floor():
    """A handful of days is an accident, not a standard deviation."""
    assert forecast_reserve_soc(_horizon_store(d0=(5.0, 3)), 26.0) is None


def test_reserve_scales_with_measured_spread():
    """A worse forecast reserves more of the battery."""
    tight = forecast_reserve_soc(_horizon_store(d0=(1.0, 40)), 26.0)
    loose = forecast_reserve_soc(_horizon_store(d0=(3.0, 40)), 26.0)
    assert tight is not None and loose is not None
    assert loose > tight
    assert tight == pytest.approx(1.0 / 26.0, rel=1e-6)


def test_reserve_is_capped_so_the_planner_can_still_trade():
    """A forecast as bad as the reference install's must not freeze the battery.

    Measured d0 RMSE there is ~25 kWh against a ~26 kWh pack — uncapped, the
    reserve would be the entire battery and the DP would never trade again.
    """
    reserve = forecast_reserve_soc(_horizon_store(d0=(25.0, 53)), 26.0)
    assert reserve == pytest.approx(0.25)


def test_reserve_uses_the_worst_actionable_horizon():
    """d0 and d1 both matter; the reserve is sized off whichever is worse."""
    reserve = forecast_reserve_soc(_horizon_store(d0=(1.0, 40), d1=(2.0, 40)), 26.0)
    assert reserve == pytest.approx(2.0 / 26.0, rel=1e-6)


def test_reserve_ignores_horizons_the_planner_cannot_reach():
    """d2–d6 are beyond the ~32 h price horizon, so they cannot size a hedge."""
    assert forecast_reserve_soc(_horizon_store(d3=(9.0, 40), d6=(9.0, 40)), 26.0) is None


def test_reserve_is_none_for_a_degenerate_capacity():
    """A zero/unknown capacity must not divide by zero or reserve everything."""
    assert forecast_reserve_soc(_horizon_store(d0=(2.0, 40)), 0.0) is None
