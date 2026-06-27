"""Select entities for sunSale: manual StorageMode override.

Exposes ``select.sunsale_mode_override`` with a list of all dispatchable
``StorageMode`` values plus a sentinel ``sunsale`` option (the default,
rendered as "sunSale" in the panel UI). When ``sunsale`` is selected the
inverter control module follows the scheduler's current-slot choice; any
other value is dispatched verbatim — and bypasses the ``automation_enabled``
gate (operator intent always reaches the inverter). State persists across
HA restarts via RestoreEntity.

Useful for experimentation: pair with the diagnostic
``sensor.sunsale_observed_inverter_mode`` to see whether the inverter
actually obeys a commanded mode.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .contract.const import DOMAIN
from .contract.models import DISPATCHABLE_MODES, StorageMode
from .orchestration.coordinator import SunSaleCoordinator


# Sentinel value indicating "no override — follow the scheduler's choice".
# Rendered as "sunSale" in the panel UI via the panel's option-label map.
MODE_OVERRIDE_SUNSALE = "sunsale"

# Override options: the canonical dispatchable modes (see contract.models.
# DISPATCHABLE_MODES — shared with the DP planner so the two surfaces can't
# diverge) plus the leading "sunSale" sentinel that releases control to the
# scheduler.
_OVERRIDE_OPTIONS: list[str] = [MODE_OVERRIDE_SUNSALE] + [m.value for m in DISPATCHABLE_MODES]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sunSale select entities for a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being set up.
        async_add_entities: Callback to register entities with HA.
    """
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ModeOverrideSelect(coordinator, entry)])


class ModeOverrideSelect(CoordinatorEntity, RestoreEntity, SelectEntity):
    """Manual StorageMode override exposed as a HA select entity.

    ``sunsale`` (rendered as "sunSale" in the panel) releases the override so
    the scheduler drives mode selection; any other option is dispatched
    verbatim each cycle, regardless of the ``automation_enabled`` switch.
    Persists via RestoreEntity.
    """

    _attr_name = "sunSale Mode Override"
    _attr_icon = "mdi:tune-variant"
    _attr_options = _OVERRIDE_OPTIONS

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        """Initialise the override select and pin its unique_id to the entry.

        Args:
            coordinator: sunSale data coordinator.
            entry: Config entry this select belongs to.
        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mode_override"
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        """Restore the last selected option and reflect it on the coordinator.

        HA may restore as ``unknown``/``unavailable`` during boot — those
        carry no decision, so we leave the coordinator at its init default
        (no override).
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        state = last.state
        if state not in _OVERRIDE_OPTIONS:
            return
        self.coordinator.mode_override = _option_to_mode(state)

    @property
    def device_info(self) -> dict:
        """Return the shared sunSale device record for the registry."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def current_option(self) -> str:
        """Return the option label currently active on the coordinator."""
        override = self.coordinator.mode_override
        return override.value if override is not None else MODE_OVERRIDE_SUNSALE

    async def async_select_option(self, option: str) -> None:
        """Update the coordinator's override and request an immediate dispatch.

        Args:
            option: Selected option label; must be one of ``_attr_options``.
        """
        if option not in _OVERRIDE_OPTIONS:
            return
        self.coordinator.mode_override = _option_to_mode(option)
        self.async_write_ha_state()
        # Push the new override to the inverter without waiting for the next
        # 5-minute tick — manual overrides are usually for experimentation
        # where the operator expects an immediate effect. This dispatches
        # straight through the control module rather than triggering a full
        # coordinator refresh (the whole DAG) just to reach the dispatcher.
        await self.coordinator.dispatch_mode_override()


def _option_to_mode(option: str) -> StorageMode | None:
    """Resolve a select-option label to its StorageMode (or None for sunsale).

    Args:
        option: Option label such as ``"sunsale"`` or ``"discharge"``.

    Returns:
        The matching StorageMode, or ``None`` when ``option`` is the
        ``sunsale`` sentinel.
    """
    if option == MODE_OVERRIDE_SUNSALE:
        return None
    return StorageMode(option)
