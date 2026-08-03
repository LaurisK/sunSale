"""Number entities for sunSale schedule-policy tuning knobs.

Each entity is a thin proxy over a single ``float`` attribute on
``SunSaleCoordinator``. The coordinator clamps the value into the bounds
declared in ``contract.const`` before packing it into ``SchedulePolicy``
each cycle, so an out-of-range write here never reaches the DP unclamped.

State is persisted via ``RestoreEntity`` so users do not need to re-tune
their setpoints after a HA restart.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .contract.const import (
    DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH,
    DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA,
    DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT,
    DOMAIN,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
    SCHEDULE_MODE_CHANGE_PENALTY_MAX,
    SCHEDULE_MODE_CHANGE_PENALTY_MIN,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
)
from .currency import per_kwh_unit, resolve_currency
from .orchestration.coordinator import SunSaleCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register sunSale schedule-policy Number entities.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being set up.
        async_add_entities: Callback to register entities with HA.
    """
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ModeChangePenaltyNumber(coordinator, entry),
        ProfitabilityTiltAlphaNumber(coordinator, entry),
        TerminalValueDiscountNumber(coordinator, entry),
        MaxDischargeToGridKwNumber(coordinator, entry),
    ])


class _SunSalePolicyNumber(CoordinatorEntity, RestoreEntity, NumberEntity):
    """Common scaffolding for a Number entity mirroring a float coordinator attr.

    Subclasses set ``_attr_name``, ``_attr_icon``, ``_unique_suffix``
    (entity unique_id suffix), ``_coord_attr`` (float attribute on the
    coordinator), ``_attr_native_min_value`` / ``_attr_native_max_value`` /
    ``_attr_native_step``, and ``_default_value`` (first-install fallback).
    """

    _attr_mode = NumberMode.BOX
    _attr_entity_category = None    # surface as a primary control, not diagnostic.
    _unique_suffix: str = ""
    _coord_attr: str = ""
    _default_value: float = 0.0

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Pin the unique_id to the config entry and stash the entry for device_info.

        Args:
            coordinator: sunSale data coordinator.
            entry: Config entry this entity belongs to.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_suffix}"
        self._entry = entry
        if getattr(self, "_attr_native_unit_of_measurement", None) == "EUR/kWh":
            self._attr_native_unit_of_measurement = per_kwh_unit(resolve_currency(entry))

    async def async_added_to_hass(self) -> None:
        """Restore the persisted setpoint; otherwise keep the coordinator's init default.

        ``unknown`` / ``unavailable`` restore states do not encode a setpoint,
        so we leave the coordinator's init default in place rather than
        overwriting it with NaN.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
            return
        try:
            value = float(last.state)
        except (TypeError, ValueError):
            return
        setattr(self.coordinator, self._coord_attr, value)

    @property
    def device_info(self) -> dict:
        """Group all sunSale entities under the same device row."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def native_value(self) -> float:
        """Return the current value of the backing coordinator attribute."""
        return float(getattr(self.coordinator, self._coord_attr))

    async def async_set_native_value(self, value: float) -> None:
        """Persist the new value and push state to HA.

        Args:
            value: The new setpoint requested by the user.
        """
        setattr(self.coordinator, self._coord_attr, float(value))
        self.async_write_ha_state()


class ModeChangePenaltyNumber(_SunSalePolicyNumber):
    """EUR/kWh penalty deducted whenever the planner changes mode between slots."""

    _attr_name = "sunSale Mode-Change Penalty"
    _attr_icon = "mdi:swap-horizontal"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_native_min_value = SCHEDULE_MODE_CHANGE_PENALTY_MIN
    _attr_native_max_value = SCHEDULE_MODE_CHANGE_PENALTY_MAX
    _attr_native_step = 0.001
    _unique_suffix = "mode_change_penalty"
    _coord_attr = "mode_change_penalty_eur_per_kwh"
    _default_value = DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH


class ProfitabilityTiltAlphaNumber(_SunSalePolicyNumber):
    """Strength of the profitability-score bias on end-of-horizon SoC value."""

    _attr_name = "sunSale Profitability Tilt α"
    _attr_icon = "mdi:tune-vertical"
    _attr_native_min_value = SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN
    _attr_native_max_value = SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX
    _attr_native_step = 0.05
    _unique_suffix = "profitability_tilt_alpha"
    _coord_attr = "profitability_tilt_alpha"
    _default_value = DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA


class TerminalValueDiscountNumber(_SunSalePolicyNumber):
    """Multiplier applied to the in-horizon median sell when valuing end-SoC."""

    _attr_name = "sunSale Terminal-Value Discount"
    _attr_icon = "mdi:percent-outline"
    _attr_native_min_value = SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN
    _attr_native_max_value = SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX
    _attr_native_step = 0.05
    _unique_suffix = "terminal_value_discount"
    _coord_attr = "terminal_value_discount"
    _default_value = DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT


class MaxDischargeToGridKwNumber(CoordinatorEntity, RestoreEntity, NumberEntity):
    """AC power cap (kW) for Discharge-to-grid mode.

    Setting this below the hardware max limits how aggressively the DP
    schedules grid export. ``max_discharge_to_grid_kw`` on the coordinator
    stores ``None`` for "use hardware max", represented in the UI as the
    entity's maximum value (``SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX``).

    This knob can only ever *reduce* the planned export rate. The coordinator
    additionally reduces it by the driver's live ``capability()`` before the DP
    sees it (``_read_schedule_knobs``), so a value above what the inverter's
    control registers will accept has no effect — the binding cap wins. Raising
    this to its maximum therefore means "no planner-side cap beyond the
    hardware's own", not "export faster than the hardware allows".
    """

    _attr_name = "sunSale Max Discharge to Grid"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "kW"
    _attr_native_min_value = SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN
    _attr_native_max_value = SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Pin entity to config entry.

        Args:
            coordinator: sunSale data coordinator.
            entry: Config entry this entity belongs to.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_max_discharge_to_grid_kw"
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        """Restore persisted setpoint on startup.

        Args: none (inherited HA lifecycle hook).
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
            return
        try:
            value = float(last.state)
        except (TypeError, ValueError):
            return
        # Treat max value as "unlimited" sentinel → None
        if value >= SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX:
            self.coordinator.max_discharge_to_grid_kw = None
        else:
            self.coordinator.max_discharge_to_grid_kw = value

    @property
    def device_info(self) -> dict:
        """Group under the sunSale device row."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def native_value(self) -> float:
        """Return kW cap; maps None (unlimited) to the entity maximum."""
        v = self.coordinator.max_discharge_to_grid_kw
        return v if v is not None else SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX

    async def async_set_native_value(self, value: float) -> None:
        """Update the discharge cap; treat max value as unlimited.

        Args:
            value: New kW setpoint from HA UI.
        """
        if value >= SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX:
            self.coordinator.max_discharge_to_grid_kw = None
        else:
            self.coordinator.max_discharge_to_grid_kw = value
        self.async_write_ha_state()
