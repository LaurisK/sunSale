"""Tests for pipeline/forecast_accuracy.py — pure Python, no HA required."""
from datetime import timedelta, timezone

from custom_components.sun_sale.contract.models import (
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
    build_forecast_accuracy_result,
    build_forecast_error_series,
)
from tests.conftest import BASE_DT

NOW = BASE_DT
UTC = timezone.utc


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

def test_censored_slot_skips_group1_bucket():
    """A censored slot must not update any intensity (Group 1) bucket."""
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 0.5)),
        quality_store=ForecastQualityStore(),
        sun_times=None,
        local_tz=UTC,
        now=NOW,
        mode_history=_history((9, StorageMode.NoExport)),
    )
    assert res.error_series.slots[0].censored is True
    assert res.quality.group1 == {}


def test_uncensored_slot_updates_group1_bucket():
    """Control: an export-mode slot still feeds the intensity buckets."""
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(10, 2.0)),
        observed=_observed(_obs_slot(10, 0.5)),
        quality_store=ForecastQualityStore(),
        sun_times=None,
        local_tz=UTC,
        now=NOW,
        mode_history=_history((9, StorageMode.SelfUse)),
    )
    assert res.error_series.slots[0].censored is False
    assert res.quality.group1 != {}


def test_group3_skips_whole_day_with_a_censored_slot():
    """A pending day-ahead total is dropped, not committed, when yesterday
    had any curtailment-suspect slot (its daily total under-reads truth)."""
    # NOW = 2024-01-15 00:00 UTC → yesterday = 2024-01-14. Slot at hour -10
    # (2024-01-14 14:00) ran under NoExport and under-generated → censored.
    store = ForecastQualityStore(group3_pending=[
        {"target_date": "2024-01-14", "horizon": 1, "forecast_kwh": 10.0},
    ])
    observed = ObservedGenerationSeries(
        slots=(_obs_slot(-10, 0.5),), computed_at=NOW, total_yesterday_kwh=5.0,
    )
    res = build_forecast_accuracy_result(
        forecast=_forecast(_fc_slot(-10, 2.0)),
        observed=observed,
        quality_store=store,
        sun_times=None,
        local_tz=UTC,
        now=NOW,
        mode_history=_history((-11, StorageMode.NoExport)),
    )
    # Resolved (removed from pending) but never committed to the horizon bucket.
    assert "1" not in res.quality.group3
    assert all(
        e["target_date"] != "2024-01-14" for e in res.quality.group3_pending
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
