"""Tier 4 DAG nodes — consume Tier 1–3 outputs."""
from __future__ import annotations

import logging

from .. import schedule as schedule_module
from ..dag_engine import DagNode, NodeContext
from ...contract.models import (
    BaseLoadProfile,
    BatteryState,
    CalculationResult,
    DegradationCost,
    GenerationSeries,
    InverterModeReading,
    PriceSeries,
    ProfitabilityScore,
    Schedule,
    SchedulePolicy,
)

_LOGGER = logging.getLogger(__name__)


class ScheduleNode(DagNode):
    """DP scheduler over (slot, soc_bucket) → Schedule(StorageMode per slot).

    Consumes the BaseLoadProfile so the DP's per-slot physics accounts for
    household draw (slot_physics.simulate_slot uses it to model
    battery-discharge-for-load and AC deficit/surplus).
    """

    output_type = Schedule
    consumes = [
        PriceSeries,
        CalculationResult,
        GenerationSeries,
        BatteryState,
        DegradationCost,
        BaseLoadProfile,
        SchedulePolicy,
    ]
    # ProfitabilityScore is read via ctx.get (the schedule still runs without
    # it), but it is a *secondary* output of ProfitabilityNode, so it must be
    # declared here to force ScheduleNode's derived tier above its producer.
    # Listing it under consumes would make a failed ProfitabilityNode skip the
    # whole schedule; consumes_optional raises the tier without gating
    # readiness. InverterModeReading is not listed — it is a primary
    # (translator) input with no producer node, so it carries no ordering
    # concern (same pattern as ForecastAccuracyNode / MonthlyBillNode).
    consumes_optional = [ProfitabilityScore]

    async def _compute(self, ctx: NodeContext) -> Schedule:
        """Run the DP scheduler and produce a StorageMode-tagged Schedule."""
        price_series = ctx.require(PriceSeries)
        calc = ctx.require(CalculationResult)
        battery_state = ctx.require(BatteryState)
        deg_cost = ctx.require(DegradationCost)
        base_load_profile = ctx.require(BaseLoadProfile)
        # GenerationSeries is consumed for tier-ordering only.
        ctx.get(GenerationSeries)
        # Optional inputs — schedule still runs without them. ProfitabilityScore
        # is tier-ordered via consumes_optional (above); InverterModeReading is
        # a primary input.
        profit_score = ctx.get(ProfitabilityScore)
        mode_reading = ctx.get(InverterModeReading)
        current_mode = mode_reading.mode if mode_reading is not None else None
        policy = ctx.get(SchedulePolicy) or SchedulePolicy()

        schedule = schedule_module.optimize_schedule(
            price_series=price_series,
            calc=calc,
            battery_config=ctx.config.battery,
            battery_state=battery_state,
            degradation_cost=deg_cost.value_kwh,
            now=ctx.now,
            base_load_profile=base_load_profile,
            local_tz=ctx.config.local_tz,
            profitability_score=profit_score,
            current_mode=current_mode,
            mode_change_penalty=policy.mode_change_penalty_eur_per_kwh,
            use_standby=policy.use_standby,
            allow_grid_charging=policy.allow_grid_charging,
            allow_feed_in=policy.allow_feed_in,
            allow_discharge_to_grid=policy.allow_discharge_to_grid,
            profitability_tilt_alpha=policy.profitability_tilt_alpha,
            terminal_value_discount=policy.terminal_value_discount,
            max_discharge_to_grid_kw=policy.max_discharge_to_grid_kw,
        )

        return schedule
