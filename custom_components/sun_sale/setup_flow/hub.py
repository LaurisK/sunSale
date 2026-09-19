"""The setup menu tree: the section mixins composed, plus the root rows and Finish."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.data_entry_flow import FlowResult

from ..contract.const import (
    CONF_INSTALL_CAPABILITIES,
    CONF_PANEL_TITLE,
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
)
from ..contract.install_capabilities import CAP_INVERTER, CAP_PRICES, CAP_SOLAR_FORECAST
from ..inbound.forecast_resolver import (
    DetectedForecast,
    detect_forecast_sensors,
    discover_forecast_entries,
    resolve_forecast_entities,
)
from ..pipeline.tariff import LEGACY_PRICE_KEYS, price_config
from .fields import suggest
from .forecast import FORECAST_DETECTED, forecast_schema, forecast_summary
from .inverter_steps import InverterSteps
from .price_steps import PriceSteps
from .sections import (
    HUB_NAME,
    SECTION_FORECAST,
    SECTION_INVERTER,
    SECTION_LABELS,
    SECTION_PRICES,
)


def installation_name_schema(d: dict) -> vol.Schema:
    """Build the installation-name form (the sidebar panel title).

    Args:
        d: Existing config/options dict used for the pre-filled value.

    Returns:
        Schema with the optional panel title. ``suggested_value`` rather than a
        default, so clearing it really returns to the automatic "sunSale".
    """
    return vol.Schema({
        vol.Optional(CONF_PANEL_TITLE, description=suggest(d.get(CONF_PANEL_TITLE))): str,
    })


class SetupHub(PriceSteps, InverterSteps):
    """Menu tree shared by both flows: root → section menus → one-module pages.

    HA's flow dialog has exactly one button per form and none on a menu, so all
    navigation lives in menu rows: ``Name →`` opens a deeper menu or page,
    ``← Back`` goes one level up, ``+ Add …`` extends a list. Every form's
    button is *Confirm*, and the form's last field is a *Confirm dropdown*:
    **✓ save and go back to <menu>** (the default) or **✕ discard and go back
    to <menu>** — how a one-button form can leave without saving.

    The root replaces the old capability checklist + hub pair — opening a
    section and confirming a page is how the section becomes part of the
    installation, and ``✕ Remove`` inside it takes it out again. ``✓ Finish
    configuration`` is always offered; when something is missing it re-shows
    the root with the reason instead of saving a half-built entry.

    Each section's steps come from its mixin (:class:`PriceSteps`,
    :class:`InverterSteps`). This class adds the root rows that are not
    sections, the single-page Solar forecast, and Finish.
    """

    async def async_step_installation_name(self, user_input: dict | None = None) -> FlowResult:
        """Name the installation (the sidebar panel title), then return to the root."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        if user_input is not None:
            self._data[CONF_PANEL_TITLE] = str(user_input.get(CONF_PANEL_TITLE) or "").strip()
            return await self._async_show_hub()
        return self._show_form(
            step_id=HUB_NAME,
            back_to=self._HUB_STEP_ID,
            data_schema=installation_name_schema(self._data),
        )

    def _forecast_devices(self) -> list[dict]:
        """Return the discovered forecast-integration entries (overridable in tests)."""
        return discover_forecast_entries(self.hass)  # type: ignore[attr-defined]

    def _forecast_resolved(self, device_ids: list[str]) -> dict[str, str]:
        """Return each chosen device's resolved base sensor, "" when none (overridable in tests).

        Args:
            device_ids: The forecast config-entry ids currently chosen.

        Returns:
            ``{entry_id: entity_id}`` in the given order.
        """
        return {
            entry_id: (resolve_forecast_entities(self.hass, [entry_id]) or [""])[0]  # type: ignore[attr-defined]
            for entry_id in device_ids
        }

    def _forecast_sensors(self, covered: list[str]) -> list[DetectedForecast]:
        """Return forecast sensors no discovered device covers (overridable in tests)."""
        return detect_forecast_sensors(self.hass, covered)  # type: ignore[attr-defined]

    async def async_step_forecast(self, user_input: dict | None = None) -> FlowResult:
        """Select any number of forecast devices and/or forecast sensors.

        One form covers three disjoint ways to name an array — the device
        picker, the list of forecast sensors no device covers, and the by-hand
        picker (see :func:`.forecast.forecast_schema`). The last two are merged
        into one stored sensor list, which supersedes the two legacy keys.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        options = self._forecast_devices()
        device_ids = list(self._data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or [])
        if user_input is not None:
            device_ids = list(user_input.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or [])
            self._data[CONF_SOLAR_FORECAST_DEVICE_IDS] = device_ids
            # The detected list and the by-hand picker both name base sensors;
            # they are one setting, de-duplicated in display order.
            self._data[CONF_SOLAR_FORECAST_ENTITIES] = list(dict.fromkeys([
                *(user_input.get(FORECAST_DETECTED) or []),
                *(user_input.get(CONF_SOLAR_FORECAST_ENTITIES) or []),
            ]))
            self._data.pop(CONF_SOLAR_FORECAST_ENTITY, None)
            self._data.pop(CONF_SOLAR_FORECAST_ENTITY_2, None)
            self._confirmed.add(SECTION_FORECAST)
            return await self._async_show_hub()

        resolved = self._forecast_resolved(device_ids)
        detected = self._forecast_sensors([e for e in resolved.values() if e])
        return self._show_form(
            step_id=SECTION_FORECAST,
            back_to=self._HUB_STEP_ID,
            data_schema=forecast_schema(options, detected, self._data),
            description_placeholders={"detected": forecast_summary(options, detected, resolved)},
        )

    def _finish_error(self) -> str:
        """Return why the configuration cannot be saved yet (blank when it can)."""
        unfinished = [
            SECTION_LABELS[section]
            for section in (SECTION_PRICES, SECTION_INVERTER)
            if self._included(section) and not self._complete(section)
        ]
        if unfinished:
            return (
                f"Not finished: {', '.join(unfinished)}. Complete the pages marked · "
                "or take the section out with ✕ inside it."
            )
        # The inverter is what sunSale dispatches; the battery block it
        # configures is read unconditionally at startup, so an entry without a
        # complete inverter section has nothing to run.
        if not self._has_real_inverter():
            return "Nothing to save yet. Set up Inverter and battery first."
        return ""

    async def async_step_finish(self, user_input: dict | None = None) -> FlowResult:
        """Save the configuration, or show on the root what is still missing."""
        if error := self._finish_error():
            return await self._async_show_hub(error)
        prices = self._complete(SECTION_PRICES)
        # The entry keeps only the buy / sell formulas (a legacy tariff converted).
        self._data[CONF_PRICE_BUY], self._data[CONF_PRICE_SELL] = price_config(self._data)
        for key in LEGACY_PRICE_KEYS:
            self._data.pop(key, None)
        self._data[CONF_INSTALL_CAPABILITIES] = {
            CAP_PRICES: prices,
            CAP_INVERTER: True,
            CAP_SOLAR_FORECAST: True,
        }
        return await self._async_finish()
