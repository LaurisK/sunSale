"""HTTP diagnostic view for sunSale — exposes a JSON snapshot of all coordinator state."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from ..contract.const import (
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    DEFAULT_EXPORT_LIMIT_W,
    DOMAIN,
)


class SunSaleDebugView(HomeAssistantView):
    """Auth-gated HTTP endpoint serving a JSON snapshot of every sunSale coordinator."""

    url = "/api/sun_sale/debug"
    name = "api:sun_sale:debug"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET /api/sun_sale/debug; return JSON snapshot of all coordinators.

        Args:
            request: Incoming aiohttp request.

        Returns:
            JSON response containing a list of coordinator state dicts.
        """
        hass = request.app["hass"]
        entries = [
            _coordinator_to_dict(entry_id, coordinator)
            for entry_id, coordinator in hass.data.get(DOMAIN, {}).items()
        ]
        return self.json(entries)


def _serialize_consumption_daily_buckets(buckets: Any) -> dict | None:
    """Serialise a ConsumptionDailyBuckets primary into a JSON-safe dict.

    Args:
        buckets: ConsumptionDailyBuckets instance or None.

    Returns:
        Dict with record_count + per-record date / hourly arrays, or None.
    """
    if buckets is None:
        return None
    return {
        "record_count": len(buckets.records),
        "records": [
            {
                "local_date":         r.local_date.isoformat(),
                "finalised_at":       r.finalised_at.isoformat(),
                "hour_kwh":           [round(v, 4) for v in r.hour_kwh],
                "hour_completeness":  [round(v, 3) for v in r.hour_completeness],
            }
            for r in buckets.records
        ],
    }


def _coordinator_to_dict(entry_id: str, coordinator: Any) -> dict:
    """Serialise one coordinator's full state into a JSON-safe dict."""
    data = coordinator.data or {}

    schedule = data.get("schedule")
    battery_state = data.get("battery_state")
    pricing = data.get("pricing")
    forecast = data.get("forecast")
    calculation = data.get("calculation")
    baked_history = data.get("baked_observed_history")
    snapshot_history = data.get("counter_snapshot_history")
    observed_gen = data.get("observed_generation")
    forecast_err = data.get("forecast_error")
    base_load_prof = data.get("base_load_profile")
    batt_status = data.get("battery_status")
    batt_runtime = data.get("battery_runtime")
    profitability = data.get("profitability_score")
    forecast_quality = data.get("forecast_quality")
    array_calibration = data.get("array_calibration")
    solar_health = data.get("solar_health")
    sun_times = data.get("sun_times")
    monthly_bill = data.get("monthly_bill")
    grid_import_power_history = data.get("grid_import_power_history")
    grid_export_power_history = data.get("grid_export_power_history")
    pv_power_history = data.get("pv_power_history")
    derived_power_history = data.get("derived_power_history")
    observed_grid = data.get("observed_grid")
    observed_consumption = data.get("observed_consumption")
    observed_losses = data.get("observed_losses")

    cfg = coordinator._config  # noqa: SLF001
    return {
        "entry_id": entry_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "automation_enabled": coordinator.automation_enabled,
        # Which write path the driver picked for the forced modes (Solis:
        # ``dispatch`` = Remote Dispatch block, ``rc`` = Remote-Control
        # registers). Chosen once at setup from what the install supports, so
        # without this a support question has no way to tell which control path
        # a capture came from.
        "control_path": getattr(coordinator, "control_path", None),
        "mode_override": (
            coordinator.mode_override.value
            if coordinator.mode_override is not None
            else None
        ),
        "config": {
            # Local timezone (IANA name) used for all day-bucketing. Lets the
            # integration check mirror the coordinator's local-date logic for
            # the profitability peak cross-check.
            "time_zone": str(getattr(coordinator._sun_sale_config, "local_tz", UTC)),  # noqa: SLF001
            "nordpool_entity": cfg.get(CONF_NORDPOOL_ENTITY, ""),
            "price_source": getattr(
                coordinator._sun_sale_config, "price_source", "nordpool"  # noqa: SLF001
            ),
            # Display-only currency code; the panel reads it for axis/tooltip
            # labels. Never affects the (currency-neutral) optimisation math.
            "currency": getattr(
                coordinator._sun_sale_config, "currency", "EUR"  # noqa: SLF001
            ),
            "solar_forecast_entity": cfg.get(CONF_SOLAR_FORECAST_ENTITY, ""),
            "solar_forecast_entity_2": cfg.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
            # Device-based forecast selection: chosen forecast-integration
            # config-entry IDs and the base "today" sensors they resolved to.
            "solar_forecast_device_ids": cfg.get(CONF_SOLAR_FORECAST_DEVICE_IDS, []),
            "solar_forecast_base_entities": list(
                getattr(coordinator, "_forecast_base_entities", []) or []
            ),
            # Resolved entity-ID map that feeds InverterController. Useful to
            # confirm whether the Solis auto-detect path actually landed on the
            # expected entities (vs. falling through to a legacy manual mapping).
            "inverter_entity_ids": getattr(coordinator, "_inverter_entity_ids", {}),
            "grid_import_power_entity_id": getattr(coordinator, "_grid_import_power_entity_id", ""),
            "grid_export_power_entity_id": getattr(coordinator, "_grid_export_power_entity_id", ""),
            "pv_power_entity_id": getattr(coordinator, "_pv_power_entity_id", ""),
            "solar_energy_today_entity_id": getattr(coordinator, "_solar_energy_today_entity_id", ""),
            "ac_port_power_entity_id": getattr(coordinator, "_ac_port_power_entity_id", ""),
            "backup_power_entity_id": getattr(coordinator, "_backup_power_entity_id", ""),
        },
        "inputs": {
            "nordpool_prices": [
                {"start": p.start.isoformat(), "end": p.end.isoformat(), "price": p.price_eur_kwh}
                for p in data.get("prices", [])
            ],
            "battery": {
                "soc": battery_state.soc,
                "power_kw": data.get("battery_power_kw"),
                "estimated_capacity_kwh": data.get("estimated_capacity"),
                # Per-observation breakdown of the capacity learner — implied
                # capacity, accept/reject, and weight per stored sample — so the
                # estimate can be audited against the nominal capacity.
                "capacity_estimator": data.get("capacity_observations"),
            } if battery_state is not None else None,
            "grid_power_kw": data.get("grid_power_kw"),
            "grid_import_power_history": {
                "sample_count": len(grid_import_power_history.samples),
                "samples": [
                    {"timestamp": s.timestamp.isoformat(), "power_kw": round(s.power_kw, 4)}
                    for s in grid_import_power_history.samples
                ],
            } if grid_import_power_history is not None else None,
            "grid_export_power_history": {
                "sample_count": len(grid_export_power_history.samples),
                "samples": [
                    {"timestamp": s.timestamp.isoformat(), "power_kw": round(s.power_kw, 4)}
                    for s in grid_export_power_history.samples
                ],
            } if grid_export_power_history is not None else None,
            "pv_power_history": {
                "sample_count": len(pv_power_history.samples),
                "samples": [
                    {"timestamp": s.timestamp.isoformat(), "power_w": round(s.power_w, 2)}
                    for s in pv_power_history.samples
                ],
            } if pv_power_history is not None else None,
            "derived_power_history": {
                "sample_count": len(derived_power_history.samples),
                "samples": [
                    {
                        "timestamp":         s.timestamp.isoformat(),
                        "ac_port_kw_signed": round(s.ac_port_kw_signed, 4),
                        "backup_kw":         round(s.backup_kw, 4),
                        "grid_net_kw_signed": round(s.grid_net_kw_signed, 4),
                        "solar_kw":          round(s.solar_kw, 4),
                        "battery_kw_signed": round(s.battery_kw_signed, 4),
                    }
                    for s in derived_power_history.samples
                ],
            } if derived_power_history is not None else None,
            "tariff_config": (
                dataclasses.asdict(coordinator.tariff_config)
                if coordinator.tariff_config is not None else None
            ),
            "consumption_today_kwh": data.get("consumption_today_kwh"),
            "yesterday_solar": {
                "date": getattr(coordinator, "_yesterday_stored_date", None),
                "entries": [
                    {
                        "start": e.start.isoformat(),
                        "end": e.end.isoformat(),
                        "kwh": e.expected_kwh,
                    }
                    for e in getattr(coordinator, "_yesterday_solar", [])
                ],
            },
            "counter_snapshot_history": {
                "record_count": len(snapshot_history.records),
                "records": [
                    {
                        "side_id":         r.side_id,
                        "captured_at":     r.captured_at.isoformat(),
                        "today_total_kwh": round(r.today_total_kwh, 4),
                    }
                    for r in snapshot_history.records
                ],
            } if snapshot_history is not None else None,
            "consumption_daily_buckets": _serialize_consumption_daily_buckets(
                data.get("consumption_daily_buckets"),
            ),
        },
        "pipeline": {
            "pricing": {
                "slot_count": len(pricing.slots),
                "resolution_s": int(pricing.resolution.total_seconds()),
                "computed_at": pricing.computed_at.isoformat(),
                "negative_sell_count": sum(1 for s in pricing.slots if s.sell_eur_kwh <= 0),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "buy": round(s.buy_eur_kwh, 4),
                        "sell": round(s.sell_eur_kwh, 4),
                        "spot": round(s.spot_eur_kwh, 4),
                        "export": (
                            round(s.export_eur_kwh, 4)
                            if s.export_eur_kwh is not None else None
                        ),
                    }
                    for s in pricing.slots
                ],
            } if pricing is not None else None,
            "forecast": {
                "slot_count": len(forecast.slots),
                **forecast.daily_totals_kwh(4),
                "today_remaining_kwh": round(forecast.today_remaining_kwh, 4),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "expected_kwh": round(s.expected_kwh, 4),
                    }
                    for s in forecast.slots
                ],
            } if forecast is not None else None,
            "calculation": {
                "slot_count": len(calculation.slots),
                "total_negative_sale_kwh": round(calculation.total_negative_sale_kwh, 4),
                "computed_at": calculation.computed_at.isoformat(),
                "feed_in_lockout_windows": [
                    {"start": w[0].isoformat(), "end": w[1].isoformat()}
                    for w in calculation.feed_in_lockout_windows
                ],
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "expected_solar_kwh": round(s.expected_solar_kwh, 4),
                        "expected_solar_negative_sale_kwh": round(s.expected_solar_negative_sale_kwh, 4),
                        "notes": list(s.notes),
                    }
                    for s in calculation.slots
                ],
            } if calculation is not None else None,
            "degradation_cost_per_kwh": data.get("degradation_cost"),
            "observed_generation": {
                "slot_count": len(observed_gen.slots),
                "total_yesterday_kwh": round(observed_gen.total_yesterday_kwh, 4),
                "total_today_so_far_kwh": round(observed_gen.total_today_so_far_kwh, 4),
                "computed_at": observed_gen.computed_at.isoformat(),
                "slots": [
                    {"start": s.start.isoformat(), "generated_kwh": round(s.generated_kwh, 4)}
                    for s in observed_gen.slots
                ],
            } if observed_gen is not None else None,
            "observed_grid": {
                "slot_count": len(observed_grid.slots),
                "total_yesterday_imported_kwh": round(observed_grid.total_yesterday_imported_kwh, 4),
                "total_yesterday_exported_kwh": round(observed_grid.total_yesterday_exported_kwh, 4),
                "total_today_imported_kwh": round(observed_grid.total_today_imported_kwh, 4),
                "total_today_exported_kwh": round(observed_grid.total_today_exported_kwh, 4),
                "computed_at": observed_grid.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "imported_kwh": round(s.imported_kwh, 4),
                        "exported_kwh": round(s.exported_kwh, 4),
                    }
                    for s in observed_grid.slots
                ],
            } if observed_grid is not None else None,
            "observed_consumption": {
                "slot_count": len(observed_consumption.slots),
                "total_yesterday_kwh": round(observed_consumption.total_yesterday_kwh, 4),
                "total_today_so_far_kwh": round(observed_consumption.total_today_so_far_kwh, 4),
                "computed_at": observed_consumption.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "consumed_kwh": round(s.consumed_kwh, 4),
                    }
                    for s in observed_consumption.slots
                ],
            } if observed_consumption is not None else None,
            "observed_losses": {
                "slot_count": len(observed_losses.slots),
                "total_yesterday_kwh": round(observed_losses.total_yesterday_kwh, 4),
                "total_today_so_far_kwh": round(observed_losses.total_today_so_far_kwh, 4),
                "computed_at": observed_losses.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "losses_kwh": round(s.losses_kwh, 4),
                    }
                    for s in observed_losses.slots
                ],
            } if observed_losses is not None else None,
            "baked_observed_history": {
                "record_count": len(baked_history.records),
                "records": [
                    {
                        "date":               r.date_str,
                        "side_id":            r.side_id,
                        "counter_total_used": round(r.counter_total_used, 4),
                        "source_kind":        r.source_kind,
                        "baked_sum":          round(r.baked_sum, 4),
                        "baked_at":           r.baked_at.isoformat(),
                        "slot_count":         len(r.baked_slots),
                        "slots": [
                            {
                                "start": s.start.isoformat(),
                                "end":   s.end.isoformat(),
                                "kwh":   round(s.kwh, 4),
                            }
                            for s in r.baked_slots
                        ],
                    }
                    for r in baked_history.records
                ],
            } if baked_history is not None else None,
            "forecast_error": {
                "slot_count": len(forecast_err.slots),
                "matched_slot_count": forecast_err.matched_slot_count,
                "total_forecast_kwh": round(forecast_err.total_forecast_kwh, 4),
                "total_observed_kwh": round(forecast_err.total_observed_kwh, 4),
                "censored_slot_count": sum(1 for s in forecast_err.slots if s.censored),
                "total_error_kwh": round(forecast_err.total_error_kwh, 4),
                "mean_absolute_error_kwh": round(forecast_err.mean_absolute_error_kwh, 4),
                "bias_kwh": round(forecast_err.bias_kwh, 4),
                "mean_absolute_percentage_error": (
                    round(forecast_err.mean_absolute_percentage_error, 4)
                    if forecast_err.mean_absolute_percentage_error is not None else None
                ),
                "computed_at": forecast_err.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "forecast_kwh": round(s.forecast_kwh, 4),
                        "observed_kwh": round(s.observed_kwh, 4),
                        "error_kwh": round(s.error_kwh, 4),
                        "relative_error": (
                            round(s.relative_error, 4) if s.relative_error is not None else None
                        ),
                        "censored": s.censored,
                    }
                    for s in forecast_err.slots
                ],
            } if forecast_err is not None else None,
            "base_load_profile": {
                "fallback_kw": round(base_load_prof.fallback_kw, 4),
                "overall_p10_kw": round(base_load_prof.overall_p10_kw, 4),
                "overall_median_kw": round(base_load_prof.overall_median_kw, 4),
                "confidence": (
                    round(base_load_prof.confidence, 4)
                    if base_load_prof.confidence is not None else None
                ),
                "sample_count": base_load_prof.sample_count,
                "distinct_days": base_load_prof.distinct_days,
                "computed_at": base_load_prof.computed_at.isoformat(),
                "slots": [
                    {
                        "hour": s.hour,
                        "baseload_kw": round(s.baseload_kw, 4),
                        "sample_count": s.sample_count,
                        "is_fallback": s.is_fallback,
                    }
                    for s in base_load_prof.slots
                ],
            } if base_load_prof is not None else None,
            "battery_status": {
                "total_capacity_kwh": round(batt_status.total_capacity_kwh, 4),
                "max_charge_power_kw": round(batt_status.max_charge_power_kw, 4),
                "max_discharge_power_kw": round(batt_status.max_discharge_power_kw, 4),
                "soc": round(batt_status.soc, 4),
                "remaining_capacity_kwh": round(batt_status.remaining_capacity_kwh, 4),
            } if batt_status is not None else None,
            "battery_runtime": {
                "remaining_kwh_usable": round(batt_runtime.remaining_kwh_usable, 4),
                "avg_drain_kw_next_hour": round(batt_runtime.avg_drain_kw_next_hour, 4),
                "runtime_minutes": (
                    round(batt_runtime.runtime_minutes, 1)
                    if batt_runtime.runtime_minutes is not None else None
                ),
                "until": batt_runtime.until.isoformat() if batt_runtime.until is not None else None,
                "horizon_hours": batt_runtime.horizon_hours,
                "computed_at": batt_runtime.computed_at.isoformat(),
            } if batt_runtime is not None else None,
            "profitability_score": {
                "score": (
                    round(profitability.score, 4)
                    if profitability.score is not None else None
                ),
                "today_peak_eur_kwh": round(profitability.today_peak_eur_kwh, 4),
                "today_class": profitability.today_class.value,
                "class_medians": {
                    k.value: round(v, 4)
                    for k, v in profitability.class_medians.items()
                },
                "window_days": profitability.window_days,
                "computed_at": profitability.computed_at.isoformat(),
            } if profitability is not None else None,
            "solar_health": {
                "status": solar_health.status,
                "recent_ceiling": (
                    round(solar_health.recent_ceiling, 4)
                    if solar_health.recent_ceiling is not None else None
                ),
                "baseline_ceiling": (
                    round(solar_health.baseline_ceiling, 4)
                    if solar_health.baseline_ceiling is not None else None
                ),
                "ratio": (
                    round(solar_health.ratio, 4)
                    if solar_health.ratio is not None else None
                ),
                "recent_days": solar_health.recent_days,
                "baseline_days": solar_health.baseline_days,
                "computed_at": (
                    solar_health.computed_at.isoformat()
                    if solar_health.computed_at else None
                ),
            } if solar_health is not None else None,
            "array_calibration": {
                "kwp_eff": round(array_calibration.kwp_eff, 4),
                "tilt_deg": round(array_calibration.tilt_deg, 2),
                "azimuth_deg": round(array_calibration.azimuth_deg, 2),
                "fit_quality": round(array_calibration.fit_quality, 4),
                "n_days": array_calibration.n_days,
                "n_slots": array_calibration.n_slots,
                "implied_forecast_kwp": (
                    round(array_calibration.implied_forecast_kwp, 4)
                    if array_calibration.implied_forecast_kwp is not None else None
                ),
                "correction_factor": (
                    round(array_calibration.correction_factor, 4)
                    if array_calibration.correction_factor is not None else None
                ),
                "confidence": round(array_calibration.confidence, 4),
                "computed_at": (
                    array_calibration.computed_at.isoformat()
                    if array_calibration.computed_at else None
                ),
            } if array_calibration is not None else None,
            "forecast_quality": {
                "sunrise_utc": sun_times.today_sunrise.isoformat() if (sun_times and sun_times.today_sunrise) else None,
                "sunset_utc": sun_times.today_sunset.isoformat() if (sun_times and sun_times.today_sunset) else None,
                "csi_bins": {k: v.metrics() for k, v in forecast_quality.csi_bins.items()},
                "elevation_bins": {k: v.metrics() for k, v in forecast_quality.elevation_bins.items()},
                "azimuth_bins": {k: v.metrics() for k, v in forecast_quality.azimuth_bins.items()},
                "horizon": {k: v.metrics() for k, v in forecast_quality.horizon.items()},
                "horizon_pending_count": len(forecast_quality.horizon_pending),
            } if forecast_quality is not None else None,
            "monthly_bill": {
                "slot_count": len(monthly_bill.slots),
                "carry_eur": round(monthly_bill.carry_eur, 4),
                "yday_to_now_eur": round(monthly_bill.yday_to_now_eur, 4),
                "total_month_eur": round(monthly_bill.total_month_eur, 4),
                "today_cost_eur": round(monthly_bill.today_cost_eur, 4),
                "yesterday_cost_eur": round(monthly_bill.yesterday_cost_eur, 4),
                "month_str": monthly_bill.month_str,
                "previous_month_str": monthly_bill.previous_month_str,
                "previous_month_eur": round(monthly_bill.previous_month_eur, 4),
                "finalized_days": {
                    ds: round(cost, 6)
                    for ds, cost in monthly_bill.updated_state.finalized_days
                },
                "computed_at": monthly_bill.computed_at.isoformat(),
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "imported_kwh": round(s.imported_kwh, 4),
                        "exported_kwh": round(s.exported_kwh, 4),
                        "buy_eur_kwh": round(s.buy_eur_kwh, 4),
                        "sell_eur_kwh": round(s.sell_eur_kwh, 4),
                        "net_cost_eur": round(s.net_cost_eur, 6),
                    }
                    for s in monthly_bill.slots
                ],
            } if monthly_bill is not None else None,
            "schedule_policy": {
                "use_standby":             bool(coordinator.use_standby),
                "allow_grid_charging":     bool(coordinator.allow_grid_charging),
                "allow_feed_in":           bool(coordinator.allow_feed_in),
                "allow_discharge_to_grid": bool(coordinator.allow_discharge_to_grid),
                "mode_change_penalty_eur_per_kwh": round(
                    float(coordinator.mode_change_penalty_eur_per_kwh), 6,
                ),
                "profitability_tilt_alpha": round(
                    float(coordinator.profitability_tilt_alpha), 4,
                ),
                "terminal_value_discount": round(
                    float(coordinator.terminal_value_discount), 4,
                ),
                "max_discharge_to_grid_kw": coordinator.max_discharge_to_grid_kw,
                "export_limit_kw": (
                    getattr(coordinator, "_export_limit_w", DEFAULT_EXPORT_LIMIT_W)
                    / 1000.0
                ),
                # The caps above are as *configured*; these are what the DP
                # actually received after the driver's live capability reduced
                # them. A gap between the two is a hardware shortfall, not a
                # config error — see docs/capability_seam.md.
                "effective": _effective_caps_block(coordinator),
            },
        },
        "outputs": {
            "schedule": {
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "mode": s.mode.value,
                        "power_kw": s.power_kw,
                        "expected_soc_after": round(s.expected_soc_after, 4),
                        "expected_profit_eur": s.expected_profit_eur,
                        "reason": s.reason,
                    }
                    for s in schedule.slots
                ],
                "total_expected_profit_eur": schedule.total_expected_profit_eur,
            } if schedule is not None else None,
            "inverter_mode": _inverter_mode_block(data, coordinator),
        },
        "last_dispatched_action": coordinator.last_dispatched_action,
        "last_dispatched_at": (
            coordinator.last_dispatched_at.isoformat()
            if coordinator.last_dispatched_at is not None else None
        ),
    }


def _effective_caps_block(coordinator: Any) -> dict | None:
    """Build the ``schedule_policy.effective`` capability sub-block.

    Exposes the driver's live per-leg ceilings alongside the caps the DP was
    actually handed, so the integration check can assert that
    ``effective == min(configured, capable)`` and an operator can see at a glance
    which leg is binding.

    Args:
        coordinator: SunSaleCoordinator instance.

    Returns:
        The capability dict, or ``None`` when no driver is set up yet or its
        capability could not be resolved.
    """
    capability = coordinator._current_capability()
    if capability is None:
        return None
    knobs = coordinator._read_schedule_knobs()
    return {
        "max_battery_charge_kw":  capability.max_battery_charge_kw,
        "max_battery_discharge_kw": capability.max_battery_discharge_kw,
        "max_export_kw":          capability.max_export_kw,
        "max_grid_discharge_kw":  capability.max_grid_discharge_kw,
        "resolved_at":            capability.resolved_at.isoformat(),
        "degraded":               list(capability.degraded),
        # What the DP received, i.e. post-reduction.
        "planned_max_discharge_to_grid_kw": knobs.max_discharge_to_grid_kw,
        "planned_export_limit_kw":          knobs.export_limit_kw,
        # The battery legs are reduced against BatteryConfig inside ScheduleNode,
        # so what the DP saw is min(configured, capable).
        "planned_max_battery_charge_kw": _reduced(
            getattr(coordinator.battery_config, "max_charge_power_kw", None),
            capability.max_battery_charge_kw,
        ),
        "planned_max_battery_discharge_kw": _reduced(
            getattr(coordinator.battery_config, "max_discharge_power_kw", None),
            capability.max_battery_discharge_kw,
        ),
    }


def _as_float(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a real number.

    Guards the diagnostic block against a coordinator attribute that is absent or
    not numeric — the debug view must never raise, since it is the tool used to
    diagnose a broken coordinator.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _reduced(configured: Any, capable: float | None) -> float | None:
    """Return the binding value of a configured limit and a hardware ceiling."""
    conf = _as_float(configured)
    if conf is None:
        return capable
    if capable is None:
        return conf
    return min(conf, capable)


def _inverter_mode_block(data: dict, coordinator: Any) -> dict:
    """Build the ``outputs.inverter_mode`` JSON block.

    Args:
        data: Coordinator's per-cycle data dict.
        coordinator: SunSaleCoordinator instance (for automation flag).

    Returns:
        Dict with the observed history, the current cycle's reading, the
        forward plan derived from Schedule, and the automation flag.
    """
    history = data.get("inverter_mode_history")
    reading = data.get("inverter_mode_reading")
    schedule = data.get("schedule")

    history_block = (
        [
            {
                "t": s.timestamp.isoformat(),
                "mode": s.mode.value,
                "raw_state": s.raw_state,
            }
            for s in history.samples
        ]
        if history is not None
        else []
    )
    plan_block = (
        [
            {
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "mode": s.mode.value,
            }
            for s in schedule.slots
        ]
        if schedule is not None
        else []
    )
    reading_block = (
        {
            "timestamp": reading.timestamp.isoformat(),
            "mode": reading.mode.value,
            "raw_state": reading.raw_state,
            "charge_a": reading.charge_a,
            "discharge_a": reading.discharge_a,
            "rc_setpoint_w": reading.rc_setpoint_w,
            "rc_engaged": reading.rc_engaged,
        }
        if reading is not None
        else None
    )
    return {
        "history": history_block,
        "plan": plan_block,
        "reading": reading_block,
        "automation_enabled": getattr(coordinator, "automation_enabled", False),
    }
