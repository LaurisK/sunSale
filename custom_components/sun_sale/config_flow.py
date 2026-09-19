"""Config flow and options flow for sunSale.

Both flows run one shared implementation — :class:`.setup_flow.SetupHub`, the
setup menu tree (root → section menus → one-module pages; every page a form
whose one *Confirm* button saves it, and whose last field is a *Confirm
dropdown* offering ✓ save-and-go-back vs ✕ discard) that lives in the
``setup_flow`` package. This module only binds it to Home Assistant: the config
flow opens it on an empty installation, the options flow on an existing entry.
"""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .contract.const import DOMAIN
from .setup_flow import IMPLEMENTED_INVERTER_PLATFORMS, INVERTER_PLATFORMS, SetupHub

__all__ = [
    "IMPLEMENTED_INVERTER_PLATFORMS",
    "INVERTER_PLATFORMS",
    "SunSaleConfigFlow",
    "SunSaleOptionsFlow",
]


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class SunSaleConfigFlow(SetupHub, config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """First-time setup: the root menu, opened on an empty installation."""

    VERSION = 1
    _HUB_STEP_ID = "hub"

    def __init__(self) -> None:
        """Initialise an empty accumulator with no page confirmed yet."""
        self._data: dict[str, Any] = {}
        self._confirmed: set[str] = set()

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Open the flow on the root menu."""
        return await self._async_show_hub()

    async def async_step_hub(self, user_input: dict | None = None) -> FlowResult:
        """Show the root menu (also the target of every ``← Back to setup`` row).

        HA refuses to show a menu or form whose ``step_id`` has no
        ``async_step_<id>`` handler (``UnknownStep``), and the frontend then
        reports every later click as "Invalid flow specified" — so the root's
        step id must resolve even though its rows route to other steps.
        """
        return await self._async_show_hub()

    async def _async_finish(self) -> FlowResult:
        """Create the config entry from the accumulated data."""
        return self.async_create_entry(title="sunSale", data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SunSaleOptionsFlow:
        """Return the options flow handler for this config entry.

        Args:
            config_entry: The existing config entry to reconfigure.

        Returns:
            SunSaleOptionsFlow instance pre-loaded with the current entry.
        """
        return SunSaleOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow — the same menu tree, opened on an already-complete entry
# ---------------------------------------------------------------------------

class SunSaleOptionsFlow(SetupHub, config_entries.OptionsFlow):
    """Reconfigure an entry: the setup menus with every existing page confirmed."""

    _HUB_STEP_ID = "init"

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Seed the accumulator from the entry and confirm the pages it holds.

        Args:
            config_entry: The config entry being reconfigured.
        """
        self._entry = config_entry
        self._data: dict[str, Any] = {**config_entry.data, **config_entry.options}
        self._confirmed: set[str] = set()
        self._seed_confirmed()

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Open the options flow on the root menu (also every ``← Back`` target)."""
        return await self._async_show_hub()

    async def _async_finish(self) -> FlowResult:
        """Write the full accumulated config as the entry's options."""
        return self.async_create_entry(title="", data=self._data)
