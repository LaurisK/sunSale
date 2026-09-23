"""Tier 1 DAG nodes — consume primary data only."""
from __future__ import annotations

import logging

from ...contract.models import (
    BaseLoadProfile,
    BatteryReading,
    BatteryState,
    BatteryStatus,
    ConsumptionDailyBuckets,
    EstimatedCapacity,
    PriceCurveHistory,
    PriceFeedData,
    PriceSeries,
    YesterdayPrices,
)
from ...inbound import battery as battery_inbound
from ...inbound import pricing as pricing_module
from ...inbound.holiday_calendar import holiday_predicate
from .. import base_load as base_load_module
from .. import price_fill as price_fill_module
from ..dag_engine import DagNode, NodeContext

_LOGGER = logging.getLogger(__name__)


class PricingNode(DagNode):
    """Assemble 72h yesterday→today→tomorrow PriceSeries with tariff applied.

    Slots the feed has no price for are then filled from the price-forecast
    engine's frozen prediction for their day and marked ``forecast=True`` (see
    :mod:`pipeline.price_fill`), so the planner has an estimate to work with
    past the auction edge instead of a fabricated zero.

    ``PriceCurveHistory`` is a persisted primary with no producer node, so it is
    read with ``ctx.get`` and deliberately absent from ``consumes``: the fill is
    an enrichment, and a series without it is still complete. Reading the store
    rather than this cycle's ``PriceForecast`` is also what keeps the DAG
    acyclic — the forecast node consumes ``PriceSeries``. The cost is that a
    prediction frozen this cycle first fills on the next one, which for a
    once-a-day capture is immaterial.

    Filling from the *frozen* prediction rather than the engine's latest view
    is also the point, not a limitation: it is the number the error is measured
    against when the auction publishes, so what the planner optimised over and
    what the forecast is scored on are the same curve.
    """

    output_type = PriceSeries
    consumes = [PriceFeedData, YesterdayPrices]

    async def _compute(self, ctx: NodeContext) -> PriceSeries:
        """Assemble PriceSeries from PriceFeedData + YesterdayPrices with tariff applied."""
        feed = ctx.require(PriceFeedData)
        yesterday = ctx.require(YesterdayPrices)
        is_holiday = holiday_predicate(ctx.config.holiday_country)
        series = pricing_module.build_price_series_72h(
            feed, yesterday, ctx.config.tariff, now=ctx.now,
            local_tz=ctx.config.local_tz, source=ctx.config.price_source,
            is_holiday=is_holiday,
        )
        history: PriceCurveHistory | None = ctx.get(PriceCurveHistory)
        if history is not None and history.predictions:
            series = price_fill_module.fill_from_forecast(
                series,
                {p.day: p.curve for p in history.predictions},
                ctx.config.tariff,
                local_tz=ctx.config.local_tz,
                is_holiday=is_holiday,
            )
        return series


class BatteryStateNode(DagNode):
    """Combine inverter reading + learned capacity → BatteryState."""

    output_type = BatteryState
    consumes = [BatteryReading, EstimatedCapacity]

    async def _compute(self, ctx: NodeContext) -> BatteryState:
        """Combine live SoC reading with learned capacity into BatteryState."""
        reading = ctx.require(BatteryReading)
        cap = ctx.require(EstimatedCapacity)
        return BatteryState(soc=reading.soc, estimated_capacity_kwh=cap.value_kwh)


class BatteryStatusNode(DagNode):
    """Snapshot configured limits + live SoC into BatteryStatus."""

    output_type = BatteryStatus
    consumes = [BatteryReading]

    async def _compute(self, ctx: NodeContext) -> BatteryStatus:
        """Combine live inverter telemetry with configured limits into BatteryStatus."""
        reading = ctx.require(BatteryReading)
        status = battery_inbound.build_battery_status(reading, ctx.config.battery)
        return status


class BaseLoadProfileNode(DagNode):
    """24h hour-of-day P15 baseload profile from per-day consumption rollups."""

    output_type = BaseLoadProfile
    consumes = [ConsumptionDailyBuckets]

    async def _compute(self, ctx: NodeContext) -> BaseLoadProfile:
        """Build 24-bucket P15 profile from the rolling daily-bucket history."""
        buckets = ctx.require(ConsumptionDailyBuckets)
        profile = base_load_module.build_base_load_profile(
            buckets, ctx.config.local_tz, now=ctx.now,
        )
        return profile
