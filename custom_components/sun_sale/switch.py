"""Switch entities for sunSale: master automation + schedule policy toggles."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .contract.const import (
    DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID,
    DEFAULT_SCHEDULE_ALLOW_FEED_IN,
    DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING,
    DEFAULT_SCHEDULE_USE_STANDBY,
    DOMAIN,
)
from .orchestration.coordinator import SunSaleCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sunSale switches (automation master + schedule policy toggles).

    Args:
        hass: Home Assistant instance.
        entry: Config entry being set up.
        async_add_entities: Callback to register entities with HA.
    """
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AutomationSwitch(coordinator, entry),
        UseStandbySwitch(coordinator, entry),
        AllowGridChargingSwitch(coordinator, entry),
        AllowFeedInSwitch(coordinator, entry),
        AllowDischargeToGridSwitch(coordinator, entry),
    ])


class _SunSaleSwitchBase(CoordinatorEntity, RestoreEntity, SwitchEntity):
    """Common scaffolding for sunSale switches that mirror a coordinator flag.

    Subclasses set ``_attr_name``, ``_attr_icon``, ``_unique_suffix`` (used to
    build the entity unique_id), ``_coord_attr`` (name of the bool attribute on
    the coordinator), and ``_default_on`` (state assumed on first install when
    nothing is restored).
    """

    _unique_suffix: str = ""
    _coord_attr: str = ""
    _default_on: bool = False

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the switch and pin its unique_id to the config entry.

        Args:
            coordinator: sunSale data coordinator.
            entry: Config entry this switch belongs to.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_suffix}"
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        """Restore the persisted on/off state when available.

        HA may restore the entity with ``unknown``/``unavailable`` during boot
        — those states carry no policy decision, so we keep the coordinator's
        init default (which already mirrors the class ``_default_on``) instead
        of forcing the flag to False.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (STATE_ON, STATE_OFF):
            setattr(self.coordinator, self._coord_attr, last.state == STATE_ON)

    @property
    def device_info(self) -> dict:
        """Return device registry info so all switches group under the sunSale device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def is_on(self) -> bool:
        """Return the current value of the backing coordinator flag."""
        return bool(getattr(self.coordinator, self._coord_attr))

    async def async_turn_on(self, **kwargs) -> None:
        """Flip the backing flag on and push the new state to HA."""
        setattr(self.coordinator, self._coord_attr, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Flip the backing flag off and push the new state to HA."""
        setattr(self.coordinator, self._coord_attr, False)
        self.async_write_ha_state()


class AutomationSwitch(_SunSaleSwitchBase):
    """When off the coordinator still computes schedules but does not send commands.

    State is persisted via RestoreEntity so it survives HA restarts.
    Defaults to OFF on first install.
    """

    _attr_name = "sunSale Automation"
    _attr_icon = "mdi:auto-fix"
    _unique_suffix = "enabled"
    _coord_attr = "automation_enabled"
    _default_on = False


class UseStandbySwitch(_SunSaleSwitchBase):
    """When on the scheduler may pick StandBy (battery idle) during no-generation windows.

    When off StandBy is removed from the DP action set, so the planner falls back
    to SelfUse during nighttime — the battery stays available to cover load
    instead of sitting idle. Defaults to ON so existing installs see no change.
    """

    _attr_name = "sunSale Use Standby"
    _attr_icon = "mdi:battery-clock"
    _unique_suffix = "use_standby"
    _coord_attr = "use_standby"
    _default_on = DEFAULT_SCHEDULE_USE_STANDBY


class AllowGridChargingSwitch(_SunSaleSwitchBase):
    """When on the scheduler may pick GridCharge (force grid-charge) at low prices.

    When off GridCharge is removed from the DP action set, prohibiting battery
    charging from the grid no matter how cheap import becomes. Defaults to ON
    so existing installs see no change.
    """

    _attr_name = "sunSale Allow Grid Charging"
    _attr_icon = "mdi:transmission-tower-import"
    _unique_suffix = "allow_grid_charging"
    _coord_attr = "allow_grid_charging"
    _default_on = DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING


class AllowFeedInSwitch(_SunSaleSwitchBase):
    """When on the scheduler may pick FeedIn-priority during surplus solar slots.

    When off, FeedIn is removed from the action set. Export to the grid can
    still occur via SelfUse when there is over-cap solar, but the explicit
    feed-in-priority mode is suppressed. Defaults to ON.
    """

    _attr_name = "sunSale Allow Feed-In"
    _attr_icon = "mdi:transmission-tower-export"
    _unique_suffix = "allow_feed_in"
    _coord_attr = "allow_feed_in"
    _default_on = DEFAULT_SCHEDULE_ALLOW_FEED_IN


class AllowDischargeToGridSwitch(_SunSaleSwitchBase):
    """When on the scheduler may pick the explicit Discharge-to-grid mode at high sell prices.

    When off, Discharge is removed from the action set. The battery may still
    discharge to cover local load via SelfUse/NoExport, but the planner will
    not actively sell stored energy. Defaults to ON.
    """

    _attr_name = "sunSale Allow Discharge to Grid"
    _attr_icon = "mdi:battery-arrow-up"
    _unique_suffix = "allow_discharge_to_grid"
    _coord_attr = "allow_discharge_to_grid"
    _default_on = DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID
