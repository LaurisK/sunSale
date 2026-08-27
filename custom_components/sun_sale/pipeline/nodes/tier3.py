"""Tier 3 DAG nodes — consume Tier 1–2 outputs."""
from __future__ import annotations

import logging

from ...contract.models import (
    ArrayCalibration,
    BakedObservedHistory,
    BatteryState,
    CalculationResult,
    ForecastAccuracyResult,
    ForecastQualityStore,
    GenerationSeries,
    InverterModeHistory,
    MonthlyBillResult,
    MonthlyBillState,
    ObservedGenerationSeries,
    ObservedGridSeries,
    PriceSeries,
    SolarHealth,
    SunTimes,
)
from .. import array_calibration, calculation, forecast_accuracy, solar_health
from .. import monthly_bill as monthly_bill_module
from ..dag_engine import DagNode, NodeContext

_LOGGER = logging.getLogger(__name__)


class ForecastAccuracyNode(DagNode):
    """Per-slot error series + EMA quality buckets → ForecastAccuracyResult.

    Combines per-cycle slot alignment (MAE/bias/RMSE) with persistent EMA
    quality tracking across four axes: clear-sky index, solar elevation, solar
    azimuth and forecast horizon. ForecastQualityStore, ArrayCalibration and
    InverterModeHistory come from primary and are NOT listed in consumes to
    avoid self-referential DAG wiring (InverterModeHistory is updated
    downstream in the same cycle).

    The site coordinates come from the config rather than an upstream node —
    all three per-slot axes are functions of solar position, so without them
    those buckets stay empty by design.

    Note the calibration read here is the *persisted* one, not this cycle's
    fresh fit: ``NodeContext.get`` gives primary precedence, and the stored
    calibration is seeded into primary every cycle. The fit only changes once
    per local day, so the resulting one-cycle lag is immaterial — and reading
    the persisted value keeps the clear-sky denominator identical across a
    restart, which a mid-cycle recompute would not.
    """

    output_type = ForecastAccuracyResult
    consumes = [GenerationSeries, ObservedGenerationSeries]

    async def _compute(self, ctx: NodeContext) -> ForecastAccuracyResult:
        """Build error series and update EMA quality buckets in one pass."""
        result = forecast_accuracy.build_forecast_accuracy_result(
            forecast=ctx.require(GenerationSeries),
            observed=ctx.require(ObservedGenerationSeries),
            quality_store=ctx.get(ForecastQualityStore),
            local_tz=ctx.config.local_tz,
            now=ctx.now,
            mode_history=ctx.get(InverterModeHistory),
            latitude=ctx.config.latitude,
            longitude=ctx.config.longitude,
            calibration=ctx.get(ArrayCalibration),
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


class ArrayCalibrationNode(DagNode):
    """Fitted array geometry from baked generation history → ArrayCalibration.

    Supplies the clear-sky index its denominator, and the array-sizing
    diagnostic its numbers. The fit grid-searches tilt/azimuth over ~200
    candidates, so it is cached and recomputed at most once per local day
    rather than every coordinator cycle — the input (yesterday's baked history)
    only changes at the daily bake anyway.

    BakedObservedHistory and the previous ArrayCalibration come from primary
    and are NOT listed in consumes: the history is a persisted store with no
    producer node, and listing the node's own output type would be
    self-referential.
    """

    output_type = ArrayCalibration
    consumes = [GenerationSeries]

    async def _compute(self, ctx: NodeContext) -> ArrayCalibration | None:
        """Refit the array geometry, or return the cached fit when still fresh."""
        previous: ArrayCalibration | None = ctx.get(ArrayCalibration)
        latitude, longitude = ctx.config.latitude, ctx.config.longitude
        if latitude is None or longitude is None:
            return previous

        if previous is not None and previous.computed_at is not None:
            same_day = (
                previous.computed_at.astimezone(ctx.config.local_tz).date()
                == ctx.now.astimezone(ctx.config.local_tz).date()
            )
            if same_day:
                return previous

        history: BakedObservedHistory | None = ctx.get(BakedObservedHistory)
        if history is None:
            return previous

        return array_calibration.build_array_calibration(
            history=history,
            forecast=ctx.require(GenerationSeries),
            latitude=latitude,
            longitude=longitude,
            now=ctx.now,
        ) or previous


class SolarHealthNode(DagNode):
    """Array-health verdict from baked history vs. the fitted clear-sky model.

    Declares ArrayCalibration in ``consumes`` purely for tier ordering: the
    stored calibration is seeded into primary, so readiness is never gated on
    it and the value actually read is the persisted one (primary wins in
    ``NodeContext.get``). As with the accuracy node, that is a one-cycle lag on
    a once-daily fit. BakedObservedHistory is likewise a persisted primary with
    no producer node.
    """

    output_type = SolarHealth
    consumes = [ArrayCalibration]

    async def _compute(self, ctx: NodeContext) -> SolarHealth | None:
        """Compare the array's recent achievable output against its own history."""
        return solar_health.build_solar_health(
            history=ctx.get(BakedObservedHistory),
            calibration=ctx.get(ArrayCalibration),
            latitude=ctx.config.latitude,
            longitude=ctx.config.longitude,
            now=ctx.now,
        )
