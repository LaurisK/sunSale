"""Tier 3 DAG nodes — consume Tier 1–2 outputs."""
from __future__ import annotations

import logging

from .. import calculation
from .. import forecast_accuracy
from .. import monthly_bill as monthly_bill_module
from ..dag_engine import DagNode, NodeContext
from ...contract.models import (
    BatteryState,
    CalculationResult,
    ForecastAccuracyResult,
    ForecastQualityStore,
    GenerationSeries,
    MonthlyBillResult,
    MonthlyBillState,
    ObservedGenerationSeries,
    ObservedGridSeries,
    PriceSeries,
    SunTimes,
)

_LOGGER = logging.getLogger(__name__)


class ForecastAccuracyNode(DagNode):
    """Per-slot error series + EMA quality buckets → ForecastAccuracyResult.

    Combines per-cycle slot alignment (MAE/bias/MAPE) with persistent EMA
    quality tracking across three bucket groups (intensity, solar-day position,
    forecast horizon). ForecastQualityStore and SunTimes come from primary and
    are NOT listed in consumes to avoid self-referential DAG wiring.
    """

    output_type = ForecastAccuracyResult
    consumes = [GenerationSeries, ObservedGenerationSeries]

    async def _compute(self, ctx: NodeContext) -> ForecastAccuracyResult:
        """Build error series and update EMA quality buckets in one pass."""
        result = forecast_accuracy.build_forecast_accuracy_result(
            forecast=ctx.require(GenerationSeries),
            observed=ctx.require(ObservedGenerationSeries),
            quality_store=ctx.get(ForecastQualityStore),
            sun_times=ctx.get(SunTimes),
            local_tz=ctx.config.local_tz,
            now=ctx.now,
        )
        return result


class MonthlyBillNode(DagNode):
    """Accumulate per-slot electricity bill from yday 00:00 to now → MonthlyBillResult.

    Consumes ObservedGridSeries (per-slot gross import/export kWh) and PriceSeries
    (buy/sell prices) to compute net cost per slot. A carry persists the bill from
    month_start to yday_start; it advances at day rollover and resets at month rollover.
    MonthlyBillState comes from primary (loaded by coordinator) and is NOT listed in
    consumes to match the ForecastQualityStore pattern for optional persistent state.
    Tier 3 because it depends on ObservedGridSeries (T2).
    """

    output_type = MonthlyBillResult
    consumes = [PriceSeries, ObservedGridSeries]

    async def _compute(self, ctx: NodeContext) -> MonthlyBillResult:
        """Compute monthly electricity bill: carry + per-slot yday-to-now costs."""
        result = monthly_bill_module.build_monthly_bill_result(
            grid_series=ctx.require(ObservedGridSeries),
            price_series=ctx.require(PriceSeries),
            stored_state=ctx.get(MonthlyBillState),
            local_tz=ctx.config.local_tz,
            now=ctx.now,
        )
        return result


class LockoutNode(DagNode):
    """Detect feed-in lockout windows and per-slot flags → CalculationResult."""

    output_type = CalculationResult
    consumes = [PriceSeries, GenerationSeries, BatteryState]

    async def _compute(self, ctx: NodeContext) -> CalculationResult:
        """Detect feed-in lockout windows and per-slot solar attribution."""
        price_series = ctx.require(PriceSeries)
        generation = ctx.require(GenerationSeries)
        battery_state = ctx.require(BatteryState)

        result = calculation.calculate(
            prices=price_series,
            generation=generation,
            battery_state=battery_state,
            now=ctx.now,
        )
        return result
