"""Binary sensors for sunSale: is the current electricity price cheap / expensive.

Published for automations and other integrations to act on (sunSale controls
nothing with them). They mirror the *Price level* sensor, so a trigger can be a
plain on/off instead of a string comparison.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .contract.const import DOMAIN
from .contract.models import PriceLevel, PriceLevelSeries
from .orchestration.coordinator import SunSaleCoordinator
from .pipeline.price_level import next_start_of, price_level_status
from .sensor import SLOT_BOUNDARY_MINUTES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cheap / expensive price binary sensors.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being set up.
        async_add_entities: Callback to register entities with HA.
    """
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CheapPriceBinarySensor(coordinator, entry),
        ExpensivePriceBinarySensor(coordinator, entry),
    ])


class _PriceLevelBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """On while the current slot has ``_level``; subclasses set name, key and level."""

    _level: PriceLevel
    _unique_suffix: str = ""

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise and pin the unique_id to the config entry.

        Args:
            coordinator: sunSale data coordinator.
            entry: Config entry this sensor belongs to.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_suffix}"
        self._entry = entry

    @property
    def device_info(self) -> dict:
        """Return device registry info so the sensor groups under the sunSale device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

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

    def _series(self) -> PriceLevelSeries | None:
        """Return the classified price series, or None when no prices are known."""
        return (self.coordinator.data or {}).get("price_level")

    @property
    def is_on(self) -> bool | None:
        """Return True while the current slot has this sensor's level (None if unknown)."""
        series = self._series()
        status = price_level_status(series, datetime.now(UTC)) if series else None
        return None if status is None else status["level"] == self._level.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the state next flips: ``until`` while on, ``next_start`` while off."""
        series = self._series()
        now = datetime.now(UTC)
        status = price_level_status(series, now) if series else None
        if series is None or status is None:
            return {}
        if status["level"] == self._level.value:
            return {"until": status["next_change"], "buy_price": status["buy_price"]}
        start = next_start_of(series, now, self._level)
        return {"next_start": start.isoformat() if start else None, "buy_price": status["buy_price"]}


class CheapPriceBinarySensor(_PriceLevelBinarySensor):
    """On while the current price is cheap (see the Price level sensor)."""

    _attr_name = "sunSale Cheap Price"
    _attr_icon = "mdi:cash-minus"
    _unique_suffix = "cheap_price"
    _level = PriceLevel.CHEAP


class ExpensivePriceBinarySensor(_PriceLevelBinarySensor):
    """On while the current price is expensive (see the Price level sensor)."""

    _attr_name = "sunSale Expensive Price"
    _attr_icon = "mdi:cash-plus"
    _unique_suffix = "expensive_price"
    _level = PriceLevel.EXPENSIVE
