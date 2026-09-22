"""Sensor entities for sunSale."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .contract.const import (
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    DOMAIN,
)
from .contract.models import (
    PRICE_STAT_SOURCE_ACTUAL,
    ArrayCalibration,
    BakedObservedHistory,
    BaseLoadProfile,
    BatteryRuntimeEstimate,
    CalculationResult,
    DerivedPowerHistory,
    ForecastErrorSeries,
    ForecastQualityStore,
    GenerationSeries,
    InverterModeHistory,
    InverterModeReading,
    MonthlyBillResult,
    ObservedConsumptionSeries,
    ObservedGenerationSeries,
    ObservedGridSeries,
    ObservedLossesSeries,
    PriceForecast,
    PriceLevel,
    PriceLevelSeries,
    PriceSeries,
    PriceSlot,
    Schedule,
    SolarHealth,
    StorageMode,
    SunTimes,
)
from .currency import currency_icon, per_kwh_unit, resolve_currency
from .orchestration.coordinator import SunSaleCoordinator
from .pipeline.price_level import price_level_status

# Price slots start on the quarter hour; re-evaluate price-level state then
# instead of waiting for the next (~5-min) coordinator refresh.
SLOT_BOUNDARY_MINUTES = (0, 15, 30, 45)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and register all sunSale sensor entities for a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry providing the coordinator reference.
        async_add_entities: HA callback to register the entity list.
    """
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CurrentActionSensor(coordinator, entry),
        NextActionSensor(coordinator, entry),
        NextActionTimeSensor(coordinator, entry),
        ExpectedProfitSensor(coordinator, entry),
        DegradationCostSensor(coordinator, entry),
        EstimatedCapacitySensor(coordinator, entry),
        CurrentBuyPriceSensor(coordinator, entry),
        CurrentSellPriceSensor(coordinator, entry),
        PriceLevelSensor(coordinator, entry),
        ScheduleSensor(coordinator, entry),
        DashboardSensor(coordinator, entry),
        InverterModeSensor(coordinator, entry),
        ObservedInverterModeSensor(coordinator, entry),
        PricingPipelineSensor(coordinator, entry),
        ForecastPipelineSensor(coordinator, entry),
        CalculationPipelineSensor(coordinator, entry),
        PriceForecastSensor(coordinator, entry),
        CurrentBaseloadSensor(coordinator, entry),
        BatteryRuntimeMinutesSensor(coordinator, entry),
        BatteryDrainUntilSensor(coordinator, entry),
        BaseloadConfidenceSensor(coordinator, entry),
        MonthlyBillSensor(coordinator, entry),
        DailyCostSensor(coordinator, entry),
        YesterdayCostSensor(coordinator, entry),
        TodayGenerationLiveSensor(coordinator, entry),
        TodayImportedLiveSensor(coordinator, entry),
        TodayExportedLiveSensor(coordinator, entry),
        TodayGenerationSlotSumSensor(coordinator, entry),
        TodayImportedSlotSumSensor(coordinator, entry),
        TodayExportedSlotSumSensor(coordinator, entry),
        YesterdayGenerationBakedSensor(coordinator, entry),
        YesterdayImportedBakedSensor(coordinator, entry),
        YesterdayExportedBakedSensor(coordinator, entry),
    ])


def _serialize_forecast_slots(series: GenerationSeries | None) -> list[dict]:
    """Convert GenerationSeries.slots to [{t, forecast_kwh, forecast_w}] for the frontend.

    Args:
        series: GenerationSeries produced by the forecast pipeline stage, or None.

    Returns:
        List of dicts with epoch-ms timestamp, kWh, and watts per slot.
    """
    if series is None:
        return []
    result = []
    for slot in series.slots:
        slot_h = (slot.end - slot.start).total_seconds() / 3600.0
        w = slot.expected_kwh / slot_h * 1000.0 if slot_h > 0 else 0.0
        result.append({
            "t": int(slot.start.timestamp() * 1000),
            "forecast_kwh": round(slot.expected_kwh, 4),
            "forecast_w": round(w),
        })
    return result


def _serialize_price_estimates(forecast: PriceForecast | None) -> dict[str, dict]:
    """Return the modelled daily price bands keyed like ``forecast_daily_kwh``.

    Only days the day-ahead auction has not settled are included, so
    tomorrow's estimate drops out as soon as its actual prices are published
    and the price chart takes over.

    Args:
        forecast: Week-ahead price forecast, or None.

    Returns:
        Dict mapping ``tomorrow`` / ``d2`` … ``d6`` to that day's cheapest and
        dearest 1 h and 4 h spot price, the forecast source and its confidence.
        The 4 h pair is None until that band has enough history to estimate.
    """
    if forecast is None:
        return {}
    out: dict[str, dict] = {}
    for stat in forecast.days:
        if stat.horizon_days < 1 or stat.source == PRICE_STAT_SOURCE_ACTUAL:
            continue
        key = "tomorrow" if stat.horizon_days == 1 else f"d{stat.horizon_days}"
        out[key] = {
            "trough_1h": round(stat.trough_1h_eur_kwh, 4),
            "peak_1h": round(stat.peak_1h_eur_kwh, 4),
            "trough_4h": (
                round(stat.trough_4h_eur_kwh, 4) if stat.trough_4h_eur_kwh is not None else None
            ),
            "peak_4h": (
                round(stat.peak_4h_eur_kwh, 4) if stat.peak_4h_eur_kwh is not None else None
            ),
            "source": stat.source,
            "confidence": round(stat.confidence, 3),
        }
    return out


def _slot_kw(kwh: float, start: datetime, end: datetime) -> float:
    """Convert a slot's energy to its average power over the slot.

    Args:
        kwh: Energy delivered/consumed across the slot, in kWh.
        start: Slot start instant.
        end: Slot end instant.

    Returns:
        Average power across the slot in kW, or 0.0 for a non-positive duration.
    """
    hours = (end - start).total_seconds() / 3600.0
    return kwh / hours if hours > 0 else 0.0


def _serialize_observed_power(
    consumption: ObservedConsumptionSeries | None,
    grid: ObservedGridSeries | None,
    losses: ObservedLossesSeries | None,
) -> dict[str, list[dict]]:
    """Serialize observed power flows into per-slot kW arrays for the panel chart.

    Each slot's energy is divided by its own duration to yield the average power
    (kW) across the slot, giving the panel an intuitive load curve. Grid is
    split into two non-negative magnitudes — ``grid_import`` and ``grid_export``
    — matching the gross flows the grid observer already accumulates per slot,
    so the panel can draw import and export as independent lines. The panel
    filters these to its display window, so all available slots are passed
    through here.

    Args:
        consumption: Derived household-consumption series, or None.
        grid: Observed grid import/export series, or None.
        losses: Derived inverter-losses series, or None.

    Returns:
        Dict with ``consumption``, ``grid_import``, ``grid_export``, and
        ``losses`` lists. Each entry is ``{"t": epoch_ms, "kw": float}``; the
        two grid magnitudes are both non-negative average power across the slot.
    """
    def _kw_slots(slots, energy_attr: str) -> list[dict]:
        """Map slots to ``{t, kw}`` using ``energy_attr`` as the kWh source."""
        return [
            {"t": int(s.start.timestamp() * 1000),
             "kw": round(_slot_kw(getattr(s, energy_attr), s.start, s.end), 4)}
            for s in slots
        ]

    grid_slots = grid.slots if grid else ()
    return {
        "consumption": _kw_slots(consumption.slots if consumption else (), "consumed_kwh"),
        "grid_import": _kw_slots(grid_slots, "imported_kwh"),
        "grid_export": _kw_slots(grid_slots, "exported_kwh"),
        "losses": _kw_slots(losses.slots if losses else (), "losses_kwh"),
    }


class _BaseSensor(CoordinatorEntity, SensorEntity):
    """Shared base for all sunSale sensor entities."""

    def __init__(
        self, coordinator: SunSaleCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        """Initialise with coordinator, config entry, and a unique key suffix.

        Args:
            coordinator: sunSale coordinator providing data updates.
            entry: Config entry; its entry_id scopes the unique_id.
            key: Suffix appended to entry_id to form the entity unique_id.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._entry = entry
        self._localise_currency(resolve_currency(entry))

    def _localise_currency(self, code: str) -> None:
        """Rewrite the EUR-placeholder unit/icon class defaults to ``code``.

        Subclasses declare money units as the historical ``"EUR"`` / ``"EUR/kWh"``
        defaults (and a ``mdi:currency-eur`` icon); this rewrites those markers to
        the configured display currency per-instance, so the optimisation math
        (currency-neutral) is untouched while the UI labels match the market.
        The resolved code is also stashed on ``self._currency`` for sensors (e.g.
        the dashboard) that surface it to the panel.

        Args:
            code: The resolved ISO-4217-style currency code.
        """
        self._currency = code
        unit = getattr(self, "_attr_native_unit_of_measurement", None)
        if unit == "EUR":
            self._attr_native_unit_of_measurement = code
        elif unit == "EUR/kWh":
            self._attr_native_unit_of_measurement = per_kwh_unit(code)
        if getattr(self, "_attr_icon", None) == "mdi:currency-eur":
            self._attr_icon = currency_icon(code)

    @property
    def device_info(self) -> dict:
        """Return device info grouping all sunSale entities under one device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def _schedule(self) -> Schedule | None:
        """Return the current Schedule from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("schedule")

    def _current_slot(self):
        """Return the schedule slot active at the current time, or slots[0] as fallback."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return None
        now = datetime.now(UTC)
        return next(
            (s for s in schedule.slots if s.start <= now < s.end),
            schedule.slots[0],
        )

    def _current_price_slot(self) -> PriceSlot | None:
        """Return the PriceSlot covering the current time, or slots[0] as fallback."""
        if self.coordinator.data is None:
            return None
        pricing: PriceSeries | None = self.coordinator.data.get("pricing")
        if not pricing or not pricing.slots:
            return None
        now = datetime.now(UTC)
        return pricing.slot_at(now) or pricing.slots[0]


class _PanelPayloadSensor(_BaseSensor):
    """Base for sensors whose attributes are a whole per-cycle pipeline bundle.

    These carry the panel's data (full slot arrays), which runs to tens of kB —
    past the recorder's 16 kB attribute ceiling, so every cycle it logged
    "State attributes ... exceed maximum size" and dropped them anyway. None of
    it is meaningful as history: the panel reads live state and the check
    harness reads the debug API. Excluding the attributes keeps the *state*
    recorded (so long-term statistics still work) and takes the churn off the
    database.
    """

    _unrecorded_attributes = frozenset({MATCH_ALL})


class CurrentActionSensor(_BaseSensor):
    """Sensor reporting the StorageMode scheduled for the current hour."""

    _attr_name = "sunSale Current Action"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, entry):
        """Initialise current-action sensor."""
        super().__init__(coordinator, entry, "current_action")

    @property
    def native_value(self) -> str:
        """Return the current schedule slot's StorageMode string."""
        slot = self._current_slot()
        return slot.mode.value if slot else StorageMode.SelfUse.value


class NextActionSensor(_BaseSensor):
    """Sensor reporting the next scheduled StorageMode that differs from the current one."""

    _attr_name = "sunSale Next Action"
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, coordinator, entry):
        """Initialise next-action sensor."""
        super().__init__(coordinator, entry, "next_action")

    @property
    def native_value(self) -> str:
        """Return the next schedule mode that differs from the current slot."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return StorageMode.SelfUse.value
        now = datetime.now(UTC)
        current = self._current_slot()
        if current is None:
            return StorageMode.SelfUse.value
        for slot in schedule.slots:
            if slot.start > now and slot.mode != current.mode:
                return slot.mode.value
        return current.mode.value


class NextActionTimeSensor(_BaseSensor):
    """Sensor reporting the start time of the next StorageMode change."""

    _attr_name = "sunSale Next Action Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        """Initialise next-action-time sensor."""
        super().__init__(coordinator, entry, "next_action_time")

    @property
    def native_value(self) -> datetime | None:
        """Return the start time of the next mode change."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return None
        now = datetime.now(UTC)
        current = self._current_slot()
        if current is None:
            return None
        for slot in schedule.slots:
            if slot.start > now and slot.mode != current.mode:
                return slot.start
        return None


class ExpectedProfitSensor(_BaseSensor):
    """Sensor reporting the total expected profit for today from the schedule."""

    _attr_name = "sunSale Expected Profit Today"
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-plus"

    def __init__(self, coordinator, entry):
        """Initialise expected-profit sensor."""
        super().__init__(coordinator, entry, "expected_profit")

    @property
    def native_value(self) -> float:
        """Return the sum of expected_profit_eur across all today's schedule slots."""
        schedule = self._schedule
        if not schedule:
            return 0.0
        today = datetime.now(UTC).date()
        return round(
            sum(s.expected_profit_eur for s in schedule.slots if s.start.date() == today),
            4,
        )


class DegradationCostSensor(_BaseSensor):
    """Sensor reporting the current battery wear cost per kWh cycled."""

    _attr_name = "sunSale Degradation Cost"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-minus"

    def __init__(self, coordinator, entry):
        """Initialise degradation-cost sensor."""
        super().__init__(coordinator, entry, "degradation_cost")

    @property
    def native_value(self) -> float | None:
        """Return the degradation cost in EUR/kWh."""
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("degradation_cost", 0.0), 6)


class EstimatedCapacitySensor(_BaseSensor):
    """Sensor reporting the learned usable battery capacity in kWh."""

    _attr_name = "sunSale Estimated Battery Capacity"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        """Initialise estimated-capacity sensor."""
        super().__init__(coordinator, entry, "estimated_capacity")

    @property
    def native_value(self) -> float | None:
        """Return the CapacityEstimator's current best-estimate in kWh."""
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("estimated_capacity", 0.0), 2)


class CurrentBuyPriceSensor(_BaseSensor):
    """Sensor reporting the effective buy price for the current pricing slot."""

    _attr_name = "sunSale Current Buy Price"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator, entry):
        """Initialise current-buy-price sensor."""
        super().__init__(coordinator, entry, "current_buy_price")

    @property
    def native_value(self) -> float | None:
        """Return the current slot's buy_eur_kwh."""
        slot = self._current_price_slot()
        return round(slot.buy_eur_kwh, 4) if slot else None


class CurrentSellPriceSensor(_BaseSensor):
    """Sensor reporting the effective sell price for the current pricing slot."""

    _attr_name = "sunSale Current Sell Price"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator, entry):
        """Initialise current-sell-price sensor."""
        super().__init__(coordinator, entry, "current_sell_price")

    @property
    def native_value(self) -> float | None:
        """Return the current slot's sell_eur_kwh."""
        slot = self._current_price_slot()
        return round(slot.sell_eur_kwh, 4) if slot else None


class PriceLevelSensor(_BaseSensor):
    """Whether the current price is cheap, normal or expensive — for automations.

    sunSale acts on nothing with this; it is published so automations and other
    integrations can (heat extra hot water while cheap, eco mode while
    expensive). Attributes carry the buy price, its rank within the day, the
    day's thresholds and when the level next changes.
    """

    _attr_name = "sunSale Price Level"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [level.value for level in PriceLevel]
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator, entry):
        """Initialise the price-level sensor."""
        super().__init__(coordinator, entry, "price_level")

    async def async_added_to_hass(self) -> None:
        """Also re-evaluate on every slot boundary, not only on coordinator refresh."""
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_change(
            self.hass, self._on_slot_boundary, minute=SLOT_BOUNDARY_MINUTES, second=1,
        ))

    @callback
    def _on_slot_boundary(self, _now: datetime) -> None:
        """Write the state for the slot that just started."""
        self.async_write_ha_state()

    def _status(self) -> dict[str, Any] | None:
        """Return the current price status, or None when no prices are known."""
        series: PriceLevelSeries | None = (self.coordinator.data or {}).get("price_level")
        return price_level_status(series, datetime.now(UTC)) if series else None

    @property
    def native_value(self) -> str | None:
        """Return ``cheap`` / ``normal`` / ``expensive`` for the current slot."""
        status = self._status()
        return status["level"] if status else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return buy price, day rank, thresholds and the next level change."""
        status = self._status()
        return {k: v for k, v in status.items() if k != "level"} if status else {}


class ScheduleSensor(_PanelPayloadSensor):
    """Sensor exposing the full optimized battery schedule as extra attributes."""

    _attr_name = "sunSale Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, entry):
        """Initialise schedule sensor."""
        super().__init__(coordinator, entry, "schedule")

    @property
    def native_value(self) -> str:
        """Return the current schedule slot's StorageMode string."""
        schedule = self._schedule
        if not schedule or not schedule.slots:
            return StorageMode.SelfUse.value
        slot = self._current_slot()
        return slot.mode.value if slot else StorageMode.SelfUse.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full schedule list and profit summary as attributes."""
        schedule = self._schedule
        if not schedule:
            return {}
        return {
            "schedule": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "mode": s.mode.value,
                    "power_kw": s.power_kw,
                    "expected_soc_after": round(s.expected_soc_after, 3),
                    "expected_profit_eur": round(s.expected_profit_eur, 4),
                    "reason": s.reason,
                }
                for s in schedule.slots
            ],
            "total_expected_profit_eur": round(schedule.total_expected_profit_eur, 4),
            "degradation_cost_per_kwh": round(schedule.degradation_cost_per_kwh, 6),
            "computed_at": schedule.computed_at.isoformat(),
        }


class InverterModeSensor(_BaseSensor):
    """Current inverter operating mode derived from Schedule and solar forecast.

    Recorded by HA for history so the dashboard's past mode band uses real data.
    """

    _attr_name = "sunSale Inverter Mode"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise inverter-mode sensor."""
        super().__init__(coordinator, entry, "inverter_mode")

    @property
    def native_value(self) -> str:
        """Derive inverter mode for the current 15-min slot from Schedule and forecast.

        Returns:
            Mode string: charge_from_grid, sell_discharge, charge_solar,
            self_use_sell, or self_use.
        """
        data = self.coordinator.data
        if not data:
            return "idle"
        schedule: Schedule | None = data.get("schedule")
        forecast: GenerationSeries | None = data.get("forecast")
        load_kw: float = data.get("household_load_kw") or 0.0

        now = datetime.now(UTC)
        slot_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        slot_end = slot_start + timedelta(minutes=15)

        cur_slot = None
        if schedule:
            cur_slot = next((s for s in schedule.slots if s.start <= now < s.end), None)

        solar_kwh = forecast.energy_between(slot_start, slot_end) if forecast else 0.0
        solar_w = solar_kwh / 0.25 * 1000.0
        load_w = load_kw * 1000.0

        if cur_slot is None:
            return "self_use_sell" if solar_w > load_w else "self_use"
        if cur_slot.mode == StorageMode.GridCharge:
            return "charge_from_grid"
        if cur_slot.mode in (StorageMode.Discharge, StorageMode.FeedIn):
            return "sell_discharge"
        if cur_slot.mode in (StorageMode.SelfUse, StorageMode.NoExport):
            return "charge_solar"
        return "self_use_sell" if solar_w > load_w else "self_use"


class ObservedInverterModeSensor(_BaseSensor):
    """Diagnostic readout of the inverter's actual current StorageMode.

    Decoded from the cycle's ``InverterModeReading`` (the driver's raw mode
    state plus ancillary currents / RC setpoint / export cap). State is always
    rendered as ``mode(raw=…, chg_a=…, dis_a=…, rc_w=…, backflow_w=…)`` — the
    same raw values regardless of whether decoding produced a known mode or
    ``unknown`` — so the raw state + ancillary signals are visible without
    having to crack open the debug endpoint.

    Pair with ``select.sunsale_mode_override`` to confirm whether a manually
    dispatched mode actually takes effect on the inverter — the override
    drives what's *commanded*; this sensor reports what's *observed*.
    """

    _attr_name = "sunSale Observed Inverter Mode"
    _attr_icon = "mdi:eye-circle-outline"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the observed-mode diagnostic sensor."""
        super().__init__(coordinator, entry, "observed_inverter_mode")

    @property
    def native_value(self) -> str:
        """Return ``<mode>(raw=…, chg_a=…, dis_a=…, rc_w=…, rc_on=…, backflow_w=…)``."""
        reading = self._reading()
        if reading is None:
            return "unavailable"
        return (
            f"{reading.mode.value}("
            f"raw={_fmt(reading.raw_state)}, "
            f"chg_a={_fmt(reading.charge_a)}, "
            f"dis_a={_fmt(reading.discharge_a)}, "
            f"rc_w={_fmt(reading.rc_setpoint_w)}, "
            f"rc_on={_fmt(reading.rc_engaged)}, "
            f"backflow_w={_fmt(reading.backflow_power_w)})"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface raw register readbacks alongside the decoded mode.

        Includes Phase 0 dispatch-engagement attributes (``last_dispatch_*``,
        ``automation_enabled_at_dispatch``) so an operator can tell whether a
        UI mode change actually reached ``apply_mode`` or was blocked by the
        ``automation_enabled`` gate / lack of a schedule slot.
        """
        reading = self._reading()
        if reading is None:
            return {}
        override = self.coordinator.mode_override
        target = self.coordinator.last_dispatched_action
        dispatched_at = self.coordinator.last_dispatched_at
        tick_at = self.coordinator.last_dispatch_tick_at
        commanded_at = self.coordinator.last_commanded_at
        verify_at = self.coordinator.last_verify_at
        return {
            "raw_state": reading.raw_state,
            "charge_a": reading.charge_a,
            "discharge_a": reading.discharge_a,
            "rc_setpoint_w": reading.rc_setpoint_w,
            "backflow_power_w": reading.backflow_power_w,
            "rc_engaged": reading.rc_engaged,
            "last_dispatched_action": target,
            "last_dispatched_at": dispatched_at.isoformat() if dispatched_at else None,
            "mode_override": override.value if override is not None else None,
            "control_path": self.coordinator.control_path,
            "last_dispatch_outcome": self.coordinator.last_dispatch_outcome,
            "last_dispatch_target": self.coordinator.last_dispatch_target,
            "last_dispatch_tick_at": tick_at.isoformat() if tick_at else None,
            "automation_enabled_at_dispatch": (
                self.coordinator.automation_enabled_at_dispatch
            ),
            # Phase 2 verify loop: "commanded" is what we asked the inverter
            # to do (our truth); "verify_state" answers "did it actually
            # take effect" by re-reading the inverter's registers ~30s after
            # the write.
            "last_commanded_mode": self.coordinator.last_commanded_mode,
            "last_commanded_at": commanded_at.isoformat() if commanded_at else None,
            "verify_state": self.coordinator.verify_state,
            "last_verify_at": verify_at.isoformat() if verify_at else None,
            "last_verify_observed_reg": self.coordinator.last_verify_observed_reg,
            # Per-register desired-vs-observed comparison for the last-commanded
            # mode — the panel colours each row green / amber / red from its
            # ``match`` flag combined with ``verify_state``.
            "register_status": self.coordinator.register_status,
            # ``observed_mode`` is decoded live from the same readbacks as
            # ``register_panel`` (independent of the 5-min ``state`` string), so
            # the panel's observed-mode label and register values stay in sync.
            # ``register_panel`` is the always-populated canonical comparison
            # (intended target vs live observed) the panel's 3-column readout
            # renders — never empty, so register values show even when no mode
            # is commanded or the observed bitmask maps to no named mode.
            "observed_mode": self.coordinator.observed_mode,
            "register_panel": self.coordinator.register_panel,
            # Monitor-only watchers (docs/battery_export_guard.md): battery
            # export under a passive mode, and discharge at/below min_soc.
            "export_guard": self.coordinator.export_guard_status,
            "soc_floor_guard": self.coordinator.soc_floor_status,
        }

    def _reading(self) -> InverterModeReading | None:
        """Return this cycle's InverterModeReading, or None when unavailable."""
        data = self.coordinator.data
        if not data:
            return None
        return data.get("inverter_mode_reading")


def _fmt(value: Any) -> str:
    """Render a value as ``None`` when missing, else its short string form."""
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class DashboardSensor(_PanelPayloadSensor):
    """Aggregates pipeline outputs into a single sensor attribute bundle for the panel."""

    _attr_name = "sunSale Dashboard"
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise dashboard sensor."""
        super().__init__(coordinator, entry, "dashboard")

    @property
    def native_value(self) -> str:
        """Return "ok" when data is available, otherwise "unavailable"."""
        return "ok" if self.coordinator.data else "unavailable"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return serialized pipeline outputs and battery summary for the panel."""
        if self.coordinator.data is None:
            return {}
        now = datetime.now(UTC)
        bc = self.coordinator.battery_config
        config = {**self._entry.data, **self._entry.options}
        err: ForecastErrorSeries | None = self.coordinator.data.get("forecast_error")
        forecast_error_slots = [
            {
                "t": int(s.start.timestamp() * 1000),
                "forecast_kwh": round(s.forecast_kwh, 4),
                "observed_kwh": round(s.observed_kwh, 4),
                "error_kwh": round(s.error_kwh, 4),
            }
            for s in (err.slots if err else ())
        ]

        status = self.coordinator.data.get("battery_status")
        estimated_capacity = self.coordinator.data.get("estimated_capacity")
        # The dashboard's total/remaining use the *learned* (estimated) usable
        # capacity, matching the scheduler (which plans against estimated_capacity);
        # the configured nameplate ("set") capacity is provided alongside for
        # reference. Falls back to nominal when no estimate is available yet.
        set_capacity_kwh = round(bc.nominal_capacity_kwh, 2) if bc else None
        capacity_kwh = (
            round(estimated_capacity, 2) if estimated_capacity is not None
            else set_capacity_kwh
        )
        soc_pct = round(status.soc * 100.0, 1) if status else None
        if status is not None and estimated_capacity is not None:
            remaining_kwh = round(status.soc * estimated_capacity, 2)
        elif status is not None:
            remaining_kwh = round(status.remaining_capacity_kwh, 2)
        else:
            remaining_kwh = None
        power_kw = self.coordinator.data.get("battery_power_kw", 0.0)
        if power_kw is None or abs(power_kw) < 0.05:
            battery_state = "idle"
        elif power_kw > 0:
            battery_state = "charging"
        else:
            battery_state = "discharging"

        live_flows = self._build_live_flows(soc_pct)

        quality: ForecastQualityStore | None = self.coordinator.data.get("forecast_quality")
        sun_times: SunTimes | None = self.coordinator.data.get("sun_times")
        forecast_quality_data = None
        if quality is not None:
            forecast_quality_data = {
                "sunrise_utc": sun_times.today_sunrise.isoformat() if (sun_times and sun_times.today_sunrise) else None,
                "sunset_utc": sun_times.today_sunset.isoformat() if (sun_times and sun_times.today_sunset) else None,
                "csi_bins": {k: v.metrics() for k, v in quality.csi_bins.items()},
                "elevation_bins": {k: v.metrics() for k, v in quality.elevation_bins.items()},
                "azimuth_bins": {k: v.metrics() for k, v in quality.azimuth_bins.items()},
                "horizon": {k: v.metrics() for k, v in quality.horizon.items()},
                "horizon_pending_count": len(quality.horizon_pending),
            }

        calibration: ArrayCalibration | None = self.coordinator.data.get("array_calibration")
        array_calibration_data = None
        if calibration is not None:
            array_calibration_data = {
                "kwp_eff": round(calibration.kwp_eff, 3),
                "tilt_deg": round(calibration.tilt_deg, 1),
                "azimuth_deg": round(calibration.azimuth_deg, 1),
                "implied_forecast_kwp": (
                    round(calibration.implied_forecast_kwp, 3)
                    if calibration.implied_forecast_kwp is not None else None
                ),
                "correction_factor": (
                    round(calibration.correction_factor, 3)
                    if calibration.correction_factor is not None else None
                ),
                "confidence": round(calibration.confidence, 3),
                "n_days": calibration.n_days,
            }

        health: SolarHealth | None = self.coordinator.data.get("solar_health")
        solar_health_data = None
        if health is not None:
            solar_health_data = {
                "status": health.status,
                "ratio": round(health.ratio, 3) if health.ratio is not None else None,
                "recent_days": health.recent_days,
                "baseline_days": health.baseline_days,
            }

        gen: GenerationSeries | None = self.coordinator.data.get("forecast")
        observed: ObservedGenerationSeries | None = self.coordinator.data.get("observed_generation")
        forecast_daily_kwh: dict[str, float] | None = None
        if gen is not None:
            forecast_daily_kwh = {
                "yesterday": round(gen.total_yesterday_kwh, 3),
                "today":     round(gen.total_today_kwh, 3),
                "tomorrow":  round(gen.total_tomorrow_kwh, 3),
                "d2":        round(gen.total_d2_kwh, 3),
                "d3":        round(gen.total_d3_kwh, 3),
                "d4":        round(gen.total_d4_kwh, 3),
                "d5":        round(gen.total_d5_kwh, 3),
                "d6":        round(gen.total_d6_kwh, 3),
            }

        schedule_obj: Schedule | None = self.coordinator.data.get("schedule")
        mode_history: InverterModeHistory | None = self.coordinator.data.get(
            "inverter_mode_history"
        )
        mode_reading: InverterModeReading | None = self.coordinator.data.get(
            "inverter_mode_reading"
        )
        inverter_mode_plan = (
            [
                {
                    "t": int(s.start.timestamp() * 1000),
                    "end_t": int(s.end.timestamp() * 1000),
                    "mode": s.mode.value,
                    "expected_soc_after": round(s.expected_soc_after, 3),
                }
                for s in schedule_obj.slots
            ]
            if schedule_obj is not None
            else []
        )
        inverter_mode_history = (
            [
                {"t": int(s.timestamp.timestamp() * 1000), "mode": s.mode.value}
                for s in mode_history.samples
            ]
            if mode_history is not None
            else []
        )
        # Resolve "now" target from the schedule slot covering this instant.
        target_mode = None
        if schedule_obj is not None:
            cur = next(
                (s for s in schedule_obj.slots if s.start <= now < s.end), None,
            )
            if cur is not None:
                target_mode = cur.mode.value
        inverter_mode_now = {
            "observed": mode_reading.mode.value if mode_reading is not None else None,
            "target": target_mode,
            "automation_enabled": self.coordinator.automation_enabled,
        }

        return {
            "generated_at": now.isoformat(),
            "now_ts": int(now.timestamp() * 1000),
            "currency": self._currency,
            "forecast_slots": _serialize_forecast_slots(self.coordinator.data.get("forecast")),
            "forecast_error_slots": forecast_error_slots,
            "observed_power_slots": _serialize_observed_power(
                self.coordinator.data.get("observed_consumption"),
                self.coordinator.data.get("observed_grid"),
                self.coordinator.data.get("observed_losses"),
            ),
            "battery_capacity_kwh": capacity_kwh,
            "battery_set_capacity_kwh": set_capacity_kwh,
            "battery_soc_pct": soc_pct,
            "battery_remaining_kwh": remaining_kwh,
            "battery_power_kw": round(power_kw, 3) if power_kw is not None else None,
            "battery_state": battery_state,
            "solar_energy_entity_id": config.get(CONF_INVERTER_ENTITY_SOLAR_ENERGY, ""),
            # Resolved role ID, not raw config: platform auto-detect (e.g. Solis)
            # never writes CONF_INVERTER_ENTITY_BATTERY_SOC back into the config
            # entry, so config.get() here would always be empty for those installs.
            "battery_soc_entity_id": self.coordinator._inverter_entity_ids.get(  # noqa: SLF001
                "battery_soc", "",
            ) or config.get(CONF_INVERTER_ENTITY_BATTERY_SOC, ""),
            "forecast_quality": forecast_quality_data,
            "array_calibration": array_calibration_data,
            "solar_health": solar_health_data,
            "forecast_daily_kwh": forecast_daily_kwh,
            "forecast_daily_price": _serialize_price_estimates(
                self.coordinator.data.get("price_forecast")
            ),
            "actual_yesterday_kwh": round(observed.total_yesterday_kwh, 3) if observed else None,
            "actual_today_kwh": round(observed.total_today_so_far_kwh, 3) if observed else None,
            "inverter_mode_plan": inverter_mode_plan,
            "inverter_mode_history": inverter_mode_history,
            "inverter_mode_now": inverter_mode_now,
            "live_flows": live_flows,
            "live_flow_sources": self.coordinator.data.get("live_flow_sources"),
            "export_limit_kw": self.coordinator.data.get("export_limit_kw"),
        }

    def _build_live_flows(self, soc_pct: float | None) -> dict[str, Any] | None:
        """Return the latest instantaneous power flows for the panel diagram.

        Sources the most recent synchronised ``DerivedPowerSample`` (only
        appended on cycles where all five primaries were present), so the
        five flow legs share one instant. ``home_kw`` and ``losses_kw`` apply
        the same clamped formulas as the derived observers
        (``inbound/observer/derived.py``). Signs follow codebase convention:
        ``battery_kw`` positive = charging, ``grid_kw`` positive = import.

        Args:
            soc_pct: Battery state-of-charge percentage to surface on the
                battery node, or None when unknown.

        Returns:
            Dict of node values (kW) plus the sample timestamp in epoch ms,
            or None when no derived sample has been captured yet.
        """
        hist: DerivedPowerHistory | None = self.coordinator.data.get(
            "derived_power_history"
        )
        if hist is None or not hist.samples:
            return None
        s = hist.samples[-1]
        home_kw = max(0.0, s.backup_kw + s.ac_port_kw_signed + s.grid_net_kw_signed)
        losses_kw = max(
            0.0,
            s.solar_kw - s.battery_kw_signed - s.ac_port_kw_signed - s.backup_kw,
        )
        return {
            "ts": int(s.timestamp.timestamp() * 1000),
            "solar_kw": round(s.solar_kw, 3),
            "battery_kw": round(s.battery_kw_signed, 3),
            "grid_kw": round(s.grid_net_kw_signed, 3),
            "home_kw": round(home_kw, 3),
            "losses_kw": round(losses_kw, 3),
            "battery_soc_pct": soc_pct,
        }


class PricingPipelineSensor(_PanelPayloadSensor):
    """Diagnostic sensor — exposes full PriceSeries for chart rendering."""

    _attr_name = "sunSale Pricing"
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise pricing pipeline diagnostic sensor."""
        super().__init__(coordinator, entry, "pricing_pipeline")

    @property
    def native_value(self) -> int:
        """Return the number of pricing slots available.

        Returns:
            Slot count, or 0 when no pricing data is present.
        """
        pricing: PriceSeries | None = (self.coordinator.data or {}).get("pricing")
        return len(pricing.slots) if pricing else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full pricing slot data and summary statistics.

        Returns:
            Dict with resolution, computed_at, buy/sell min/max, and per-slot detail.
        """
        pricing: PriceSeries | None = (self.coordinator.data or {}).get("pricing")
        if not pricing:
            return {}
        buy_prices = [s.buy_eur_kwh for s in pricing.slots]
        sell_prices = [s.sell_eur_kwh for s in pricing.slots]
        return {
            "resolution_s": int(pricing.resolution.total_seconds()),
            "computed_at": pricing.computed_at.isoformat(),
            "negative_sell_count": sum(1 for s in pricing.slots if s.sell_eur_kwh <= 0),
            "min_buy": round(min(buy_prices), 4) if buy_prices else None,
            "max_buy": round(max(buy_prices), 4) if buy_prices else None,
            "min_sell": round(min(sell_prices), 4) if sell_prices else None,
            "max_sell": round(max(sell_prices), 4) if sell_prices else None,
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "buy_eur_kwh": round(s.buy_eur_kwh, 4),
                    "sell_eur_kwh": round(s.sell_eur_kwh, 4),
                    "spot_eur_kwh": round(s.spot_eur_kwh, 4),
                }
                for s in pricing.slots
            ],
        }


class ForecastPipelineSensor(_PanelPayloadSensor):
    """Diagnostic sensor — exposes full GenerationSeries for chart rendering."""

    _attr_name = "sunSale Forecast"
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise forecast pipeline diagnostic sensor."""
        super().__init__(coordinator, entry, "forecast_pipeline")

    @property
    def native_value(self) -> float:
        """Return total expected solar generation for today in kWh.

        Returns:
            Sum of expected_kwh across today's forecast slots, or 0.0 when unavailable.
        """
        gen: GenerationSeries | None = (self.coordinator.data or {}).get("forecast")
        if not gen:
            return 0.0
        today = datetime.now(UTC).date()
        return round(
            sum(s.expected_kwh for s in gen.slots if s.start.date() == today),
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full forecast slot data and per-day totals.

        Returns:
            Dict with daily kWh totals and per-slot detail.
        """
        gen: GenerationSeries | None = (self.coordinator.data or {}).get("forecast")
        if not gen:
            return {}
        price: PriceForecast | None = (self.coordinator.data or {}).get("price_forecast")
        return {
            **gen.daily_totals_kwh(4),
            # Week-ahead price sits on the same day axis as the week-ahead
            # generation above, so a consumer can read "how much will I make"
            # against "how much will I generate" without a second lookup.
            **(price.daily_price_stats(4) if price is not None else {}),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "expected_kwh": round(s.expected_kwh, 4),
                }
                for s in gen.slots
            ],
        }


class PriceForecastSensor(_PanelPayloadSensor):
    """Week-ahead price forecast — peak/trough bands and negative-price exposure.

    The state is tomorrow's expected 4 h peak, because that is the nearest
    figure a hold-or-sell decision turns on; everything else (today through
    d6, the trough bands, negative hours and the PV exposed to them) is in the
    attributes. ``shape_days`` is published alongside so the forecast's own
    maturity is visible: it counts the settled days the shape engine has
    learned from, and a day the engine cannot yet speak for is absent from the
    horizon rather than guessed.
    """

    _attr_name = "sunSale Price Forecast"
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Placeholder rewritten to the configured display currency by _BaseSensor.
    _attr_native_unit_of_measurement = "EUR/kWh"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the week-ahead price forecast sensor."""
        super().__init__(coordinator, entry, "price_forecast")

    @property
    def native_value(self) -> float | None:
        """Return tomorrow's expected 4 h peak price, or None when unavailable."""
        forecast: PriceForecast | None = (self.coordinator.data or {}).get("price_forecast")
        if forecast is None or not forecast.days:
            return None
        tomorrow = next((d for d in forecast.days if d.horizon_days == 1), None)
        if tomorrow is None or tomorrow.peak_4h_eur_kwh is None:
            return None
        return round(tomorrow.peak_4h_eur_kwh, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return per-day statistics plus the model's own skill diagnostics."""
        forecast: PriceForecast | None = (self.coordinator.data or {}).get("price_forecast")
        if forecast is None:
            return {}
        return {
            **forecast.daily_price_stats(4),
            "history_days": forecast.history_days,
            "shape_days": forecast.shape_days,
            "computed_at": (
                forecast.computed_at.isoformat()
                if forecast.computed_at is not None else None
            ),
        }


class CalculationPipelineSensor(_PanelPayloadSensor):
    """Diagnostic sensor — exposes CalculationResult (lockout windows, etc.)."""

    _attr_name = "sunSale Calculation"
    _attr_icon = "mdi:calculator"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise calculation pipeline diagnostic sensor."""
        super().__init__(coordinator, entry, "calculation_pipeline")

    @property
    def native_value(self) -> int:
        """Return the number of active feed-in lockout windows.

        Returns:
            Count of lockout windows, or 0 when no calculation data is present.
        """
        calc: CalculationResult | None = (self.coordinator.data or {}).get("calculation")
        return len(calc.feed_in_lockout_windows) if calc else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full calculation result including lockout windows and slot detail.

        Returns:
            Dict with computed_at, negative sale total, lockout windows, and per-slot data.
        """
        calc: CalculationResult | None = (self.coordinator.data or {}).get("calculation")
        if not calc:
            return {}
        return {
            "computed_at": calc.computed_at.isoformat(),
            "total_negative_sale_kwh": round(calc.total_negative_sale_kwh, 4),
            "feed_in_lockout_windows": [
                {"start": w[0].isoformat(), "end": w[1].isoformat()}
                for w in calc.feed_in_lockout_windows
            ],
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "expected_solar_kwh": round(s.expected_solar_kwh, 4),
                    "expected_solar_negative_sale_kwh": round(s.expected_solar_negative_sale_kwh, 4),
                    "notes": list(s.notes),
                }
                for s in calc.slots
            ],
        }


class _BaseloadSensor(_BaseSensor):
    """Mixin: pull BaseLoadProfile / BatteryRuntimeEstimate from coordinator data."""

    @property
    def _profile(self) -> BaseLoadProfile | None:
        """Return the current BaseLoadProfile from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("base_load_profile")

    @property
    def _runtime(self) -> BatteryRuntimeEstimate | None:
        """Return the current BatteryRuntimeEstimate from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("battery_runtime")


class CurrentBaseloadSensor(_BaseloadSensor):
    """Sensor reporting the estimated household base load at the current time slot."""

    _attr_name = "sunSale Current Baseload"
    _attr_native_unit_of_measurement = "kW"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-lightning-bolt"

    def __init__(self, coordinator, entry):
        """Initialise current baseload sensor."""
        super().__init__(coordinator, entry, "current_baseload")

    @property
    def native_value(self) -> float | None:
        """Return estimated household base load for the current hour in kW.

        Returns:
            Base load kW rounded to 3 dp, or None when the profile is unavailable.
        """
        profile = self._profile
        if profile is None:
            return None
        local_tz = self.coordinator._sun_sale_config.local_tz
        return round(profile.at(datetime.now(UTC), local_tz), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return full baseload profile statistics and hourly slot data.

        Returns:
            Dict with fallback_kw, percentiles, confidence, sample counts, and hourly slots.
        """
        profile = self._profile
        if profile is None:
            return {}
        return {
            "fallback_kw": round(profile.fallback_kw, 3),
            "overall_p10_kw": round(profile.overall_p10_kw, 3),
            "overall_median_kw": round(profile.overall_median_kw, 3),
            "confidence": profile.confidence,
            "sample_count": profile.sample_count,
            "distinct_days": profile.distinct_days,
            "slots": [
                {
                    "hour": s.hour,
                    "baseload_kw": round(s.baseload_kw, 3),
                    "sample_count": s.sample_count,
                    "is_fallback": s.is_fallback,
                }
                for s in profile.slots
            ],
        }


class BatteryRuntimeMinutesSensor(_BaseloadSensor):
    """Sensor reporting estimated minutes until the battery is depleted at current load."""

    _attr_name = "sunSale Battery Runtime"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator, entry):
        """Initialise battery runtime minutes sensor."""
        super().__init__(coordinator, entry, "battery_runtime_minutes")

    @property
    def native_value(self) -> float | None:
        """Return estimated battery runtime in minutes.

        Returns:
            Runtime minutes rounded to 1 dp, or None when unavailable.
        """
        runtime = self._runtime
        if runtime is None or runtime.runtime_minutes is None:
            return None
        return round(runtime.runtime_minutes, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return battery runtime estimate detail.

        Returns:
            Dict with usable remaining kWh, average drain rate, horizon, and computed_at.
        """
        runtime = self._runtime
        if runtime is None:
            return {}
        return {
            "remaining_kwh_usable": round(runtime.remaining_kwh_usable, 3),
            "avg_drain_kw_next_hour": round(runtime.avg_drain_kw_next_hour, 3),
            "horizon_hours": runtime.horizon_hours,
            "computed_at": runtime.computed_at.isoformat(),
        }


class BatteryDrainUntilSensor(_BaseloadSensor):
    """Sensor reporting the projected timestamp at which the battery will be depleted."""

    _attr_name = "sunSale Battery Drain Until"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        """Initialise battery drain-until timestamp sensor."""
        super().__init__(coordinator, entry, "battery_drain_until")

    @property
    def native_value(self) -> datetime | None:
        """Return the projected battery depletion timestamp.

        Returns:
            Timezone-aware datetime when the battery is expected to reach min SoC,
            or None when the estimate is unavailable.
        """
        runtime = self._runtime
        return runtime.until if runtime else None


class BaseloadConfidenceSensor(_BaseloadSensor):
    """Sensor reporting the confidence score of the current base load profile."""

    _attr_name = "sunSale Baseload Confidence"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator, entry):
        """Initialise baseload confidence sensor."""
        super().__init__(coordinator, entry, "baseload_confidence")

    @property
    def native_value(self) -> float | None:
        """Return the baseload profile confidence score.

        Returns:
            Confidence value in [0, 1] rounded to 3 dp, or None when unavailable.
        """
        profile = self._profile
        if profile is None or profile.confidence is None:
            return None
        return round(profile.confidence, 3)


class MonthlyBillSensor(_PanelPayloadSensor):
    """Sensor reporting the net electricity bill for the current calendar month.

    The value is carry (month start → yesterday midnight) plus the live
    yday-to-now portion derived from grid power history and current prices.
    Resets to zero at the start of each new calendar month.
    """

    _attr_name = "sunSale Monthly Bill"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise monthly bill sensor."""
        super().__init__(coordinator, entry, "monthly_bill")

    @property
    def native_value(self) -> float | None:
        """Return the net electricity bill for the current month in EUR.

        Returns:
            Total month bill rounded to 4 dp, or None when no data is available.
        """
        result: MonthlyBillResult | None = (self.coordinator.data or {}).get("monthly_bill")
        if result is None:
            return None
        return round(result.total_month_eur, 4)

    @property
    def last_reset(self) -> datetime:
        """Return the start of the current calendar month in UTC.

        Returns:
            Timezone-aware datetime for midnight on the 1st of the current month.
        """
        now = datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bill breakdown: carry, yday-to-now, month, and per-slot detail.

        Returns:
            Dict with month_str, carry_eur, yday_to_now_eur, total_month_eur,
            slot_count, computed_at, and per-slot import/export/cost data.
        """
        result: MonthlyBillResult | None = (self.coordinator.data or {}).get("monthly_bill")
        if result is None:
            return {}
        return {
            "month_str": result.month_str,
            "carry_eur": round(result.carry_eur, 4),
            "yday_to_now_eur": round(result.yday_to_now_eur, 4),
            "total_month_eur": round(result.total_month_eur, 4),
            "previous_month_str": result.previous_month_str,
            "previous_month_eur": round(result.previous_month_eur, 4),
            "slot_count": len(result.slots),
            "computed_at": result.computed_at.isoformat(),
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
                for s in result.slots
            ],
        }


class _DailyCostSensorBase(_BaseSensor):
    """Shared base for the per-day electricity-cost sensors.

    Both read the daily breakdowns already computed by the monthly-bill
    pipeline (``MonthlyBillResult.today_cost_eur`` /
    ``yesterday_cost_eur``). MONETARY device class with a ``TOTAL`` state
    class and a daily ``last_reset`` lets HA store a clean per-day cost total
    in long-term statistics (the value resets each local midnight).
    """

    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:currency-eur"

    def _result(self) -> MonthlyBillResult | None:
        """Return the current MonthlyBillResult, or None when unavailable."""
        return (self.coordinator.data or {}).get("monthly_bill")

    def _local_tz(self):
        """Return the configured local timezone, falling back to UTC.

        Falls back when the coordinator config is not yet initialised (HA can
        read ``last_reset`` before the first refresh completes).
        """
        cfg = getattr(self.coordinator, "_sun_sale_config", None)
        return getattr(cfg, "local_tz", UTC) or UTC

    def _local_midnight_utc(self, days_ago: int) -> datetime:
        """Return the UTC instant of local midnight ``days_ago`` days back.

        Args:
            days_ago: 0 for today's local midnight, 1 for yesterday's, etc.

        Returns:
            Timezone-aware UTC datetime of that local midnight.
        """
        local_tz = self._local_tz()
        d = datetime.now(UTC).astimezone(local_tz).date() - timedelta(days=days_ago)
        return datetime(d.year, d.month, d.day, tzinfo=local_tz).astimezone(UTC)

    @staticmethod
    def _sum_flows(slots, start: datetime, end: datetime | None) -> dict[str, float]:
        """Sum imported/exported kWh over bill slots whose start is in [start, end).

        Args:
            slots: MonthlyBillResult.slots (live-window BillSlots).
            start: Inclusive lower bound on slot start.
            end: Exclusive upper bound, or None for no upper bound.

        Returns:
            Dict with ``imported_kwh`` and ``exported_kwh`` totals (4 dp).
        """
        def _in(s) -> bool:
            return s.start >= start and (end is None or s.start < end)
        return {
            "imported_kwh": round(sum(s.imported_kwh for s in slots if _in(s)), 4),
            "exported_kwh": round(sum(s.exported_kwh for s in slots if _in(s)), 4),
        }


class DailyCostSensor(_DailyCostSensorBase):
    """Net electricity cost for today so far (today local midnight → now).

    Resets to zero each local midnight (``last_reset``), so HA records one
    finalised cost figure per day in its long-term statistics.
    """

    _attr_name = "sunSale Daily Cost"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today daily-cost sensor."""
        super().__init__(coordinator, entry, "daily_cost")

    @property
    def native_value(self) -> float | None:
        """Return today's net electricity cost in EUR, or None when unavailable."""
        result = self._result()
        if result is None:
            return None
        return round(result.today_cost_eur, 4)

    @property
    def last_reset(self) -> datetime:
        """Return today's local midnight as a UTC datetime (daily reset anchor)."""
        return self._local_midnight_utc(0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose today's import/export kWh and context for the daily cost."""
        result = self._result()
        if result is None:
            return {}
        attrs: dict[str, Any] = {
            "month_str": result.month_str,
            "computed_at": result.computed_at.isoformat(),
        }
        attrs.update(self._sum_flows(result.slots, self._local_midnight_utc(0), None))
        return attrs


class YesterdayCostSensor(_DailyCostSensorBase):
    """Net electricity cost for the full previous local day.

    Finalises once per day: the value covers [yesterday 00:00, today 00:00)
    and only refines as yesterday's observed grid is baked in. ``last_reset``
    is yesterday's local midnight so HA attributes the figure to that day.
    """

    _attr_name = "sunSale Yesterday Cost"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the yesterday daily-cost sensor."""
        super().__init__(coordinator, entry, "yesterday_cost")

    @property
    def native_value(self) -> float | None:
        """Return yesterday's net electricity cost in EUR, or None when unavailable."""
        result = self._result()
        if result is None:
            return None
        return round(result.yesterday_cost_eur, 4)

    @property
    def last_reset(self) -> datetime:
        """Return yesterday's local midnight as a UTC datetime (reset anchor)."""
        return self._local_midnight_utc(1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose yesterday's import/export kWh and the local date it covers.

        The kWh breakdown is derived from the live-window slots, which span
        yesterday + today on a normal day; it is omitted on the first of a
        month (when yesterday falls in the previous month and is therefore
        outside the live window) so a misleading zero is never shown.
        """
        result = self._result()
        if result is None:
            return {}
        yday_start = self._local_midnight_utc(1)
        today_start = self._local_midnight_utc(0)
        attrs: dict[str, Any] = {
            "date": yday_start.astimezone(self._local_tz()).date().isoformat(),
            "computed_at": result.computed_at.isoformat(),
        }
        if result.slots and result.slots[0].start <= yday_start:
            attrs.update(self._sum_flows(result.slots, yday_start, today_start))
        return attrs


# ---------------------------------------------------------------------------
# Observed-series sensors (live counter / slot-sum / baked yesterday)
# ---------------------------------------------------------------------------

class _ObservedKwhSensorBase(_BaseSensor):
    """Shared kWh sensor base — energy class + native unit + non-negative round."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_suggested_display_precision = 3


class _TodayLiveKwhSensor(_ObservedKwhSensorBase):
    """Sensor reading a live today-total kWh counter from coordinator.data.

    The value is the inverter's daily-resetting counter as last sampled,
    matched directly to what the inverter app shows. State class
    ``TOTAL_INCREASING`` because the counter monotonically rises within a
    day and resets to 0 at local midnight (HA recognises the reset).
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    # Subclasses set _data_key (the coordinator.data dict key to read).
    _data_key: str = ""

    @property
    def native_value(self) -> float | None:
        """Return the live counter value, or ``None`` when unavailable."""
        data = self.coordinator.data or {}
        value = data.get(self._data_key)
        if value is None:
            return None
        return round(float(value), 3)


class TodayGenerationLiveSensor(_TodayLiveKwhSensor):
    """Today's solar generation total reported live by the inverter counter."""

    _attr_name = "sunSale Today Generation"
    _attr_icon = "mdi:solar-power"
    _data_key = "today_generation_live_kwh"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today-generation live counter sensor."""
        super().__init__(coordinator, entry, "today_generation_live")


class TodayImportedLiveSensor(_TodayLiveKwhSensor):
    """Today's grid-imported total reported live by the inverter counter."""

    _attr_name = "sunSale Today Imported"
    _attr_icon = "mdi:transmission-tower-import"
    _data_key = "today_imported_live_kwh"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today-imported live counter sensor."""
        super().__init__(coordinator, entry, "today_imported_live")


class TodayExportedLiveSensor(_TodayLiveKwhSensor):
    """Today's grid-exported total reported live by the inverter counter."""

    _attr_name = "sunSale Today Exported"
    _attr_icon = "mdi:transmission-tower-export"
    _data_key = "today_exported_live_kwh"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today-exported live counter sensor."""
        super().__init__(coordinator, entry, "today_exported_live")


class _TodaySlotSumKwhSensor(_ObservedKwhSensorBase):
    """Diagnostic sensor exposing the sum of today's raw averaged slots.

    Mid-day this can drift from the live inverter counter because today's
    slots are raw power averages with no per-cycle correction; both are
    expected to align at end-of-day when the bake-in finalises yesterday.
    State class ``MEASUREMENT`` (not for energy-dashboard integration).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False  # diagnostic; opt-in


class TodayGenerationSlotSumSensor(_TodaySlotSumKwhSensor):
    """Slot-sum of today's observed-generation series (diagnostic)."""

    _attr_name = "sunSale Today Generation (slot sum)"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today-generation slot-sum diagnostic sensor."""
        super().__init__(coordinator, entry, "today_generation_slot_sum")

    @property
    def native_value(self) -> float | None:
        """Return ``ObservedGenerationSeries.total_today_so_far_kwh`` or ``None``."""
        observed: ObservedGenerationSeries | None = (
            self.coordinator.data or {}
        ).get("observed_generation")
        if observed is None:
            return None
        return round(observed.total_today_so_far_kwh, 3)


class TodayImportedSlotSumSensor(_TodaySlotSumKwhSensor):
    """Slot-sum of today's observed grid-import series (diagnostic)."""

    _attr_name = "sunSale Today Imported (slot sum)"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today-imported slot-sum diagnostic sensor."""
        super().__init__(coordinator, entry, "today_imported_slot_sum")

    @property
    def native_value(self) -> float | None:
        """Return ``ObservedGridSeries.total_today_imported_kwh`` or ``None``."""
        observed: ObservedGridSeries | None = (
            self.coordinator.data or {}
        ).get("observed_grid")
        if observed is None:
            return None
        return round(observed.total_today_imported_kwh, 3)


class TodayExportedSlotSumSensor(_TodaySlotSumKwhSensor):
    """Slot-sum of today's observed grid-export series (diagnostic)."""

    _attr_name = "sunSale Today Exported (slot sum)"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the today-exported slot-sum diagnostic sensor."""
        super().__init__(coordinator, entry, "today_exported_slot_sum")

    @property
    def native_value(self) -> float | None:
        """Return ``ObservedGridSeries.total_today_exported_kwh`` or ``None``."""
        observed: ObservedGridSeries | None = (
            self.coordinator.data or {}
        ).get("observed_grid")
        if observed is None:
            return None
        return round(observed.total_today_exported_kwh, 3)


class _YesterdayBakedKwhSensor(_ObservedKwhSensorBase):
    """Sensor reading the baked yesterday total for one side.

    Sources from ``coordinator.data["baked_observed_history"]``. State class
    ``TOTAL`` because the value finalises once per day and never updates
    again for that date. Exposes ``source_kind``, ``counter_total_used``,
    and ``date_str`` as attributes so dashboards can branch on provenance.
    """

    _attr_state_class = SensorStateClass.TOTAL

    _side_id: str = ""

    def _yesterday_record(self):
        """Return the BakedDayRecord for yesterday + ``_side_id``, or ``None``."""
        history: BakedObservedHistory | None = (
            self.coordinator.data or {}
        ).get("baked_observed_history")
        if history is None:
            return None
        local_tz = self.coordinator._sun_sale_config.local_tz  # noqa: SLF001
        local_today = datetime.now(UTC).astimezone(local_tz).date()
        yesterday_str = (local_today - timedelta(days=1)).isoformat()
        for r in history.records:
            if r.date_str == yesterday_str and r.side_id == self._side_id:
                return r
        return None

    @property
    def native_value(self) -> float | None:
        """Return ``baked_sum`` for yesterday on this side, or ``None``."""
        record = self._yesterday_record()
        if record is None:
            return None
        return round(record.baked_sum, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the bake provenance fields as attributes."""
        record = self._yesterday_record()
        if record is None:
            return {}
        return {
            "date":               record.date_str,
            "source_kind":        record.source_kind,
            "counter_total_used": round(record.counter_total_used, 3),
            "baked_at":           record.baked_at.isoformat(),
        }


class YesterdayGenerationBakedSensor(_YesterdayBakedKwhSensor):
    """Yesterday's solar generation total, finalised by the once-per-day bake-in."""

    _attr_name = "sunSale Yesterday Generation"
    _attr_icon = "mdi:solar-power-variant"
    _side_id = "generation"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the yesterday-generation baked sensor."""
        super().__init__(coordinator, entry, "yesterday_generation_baked")


class YesterdayImportedBakedSensor(_YesterdayBakedKwhSensor):
    """Yesterday's grid-imported total, finalised by the once-per-day bake-in."""

    _attr_name = "sunSale Yesterday Imported"
    _attr_icon = "mdi:transmission-tower-import"
    _side_id = "grid_import"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the yesterday-imported baked sensor."""
        super().__init__(coordinator, entry, "yesterday_imported_baked")


class YesterdayExportedBakedSensor(_YesterdayBakedKwhSensor):
    """Yesterday's grid-exported total, finalised by the once-per-day bake-in."""

    _attr_name = "sunSale Yesterday Exported"
    _attr_icon = "mdi:transmission-tower-export"
    _side_id = "grid_export"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the yesterday-exported baked sensor."""
        super().__init__(coordinator, entry, "yesterday_exported_baked")
