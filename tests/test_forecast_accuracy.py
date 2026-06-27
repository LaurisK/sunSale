"""Tests for pipeline/forecast_accuracy.py — pure Python, no HA required."""
from datetime import timedelta

from custom_components.sun_sale.contract.models import (
    GenerationSeries,
    GenerationSlot,
    ObservedGenerationSeries,
    ObservedGenerationSlot,
)
from custom_components.sun_sale.pipeline.forecast_accuracy import (
    build_forecast_error_series,
)
from tests.conftest import BASE_DT

NOW = BASE_DT


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
