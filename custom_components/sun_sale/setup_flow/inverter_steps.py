"""Inverter-and-battery section steps: platform, battery, entity mapping, energy counters."""
from __future__ import annotations

from homeassistant.data_entry_flow import FlowResult

from ..contract.const import CONF_BMS_BATTERY_SOC, CONF_INVERTER_PLATFORM, CONF_SOLIS_CONFIG_ENTRY_ID
from ..inbound.inverter_sources import (
    SourceOptions,
    detect_bms_sources,
    detect_inverter_sources,
    device_of,
)
from ..inbound.platform_profiles import config_entry_key, manual_role_key, profile_for
from ..outbound.inverter import InverterPlatform
from .core import HubCore
from .fields import NONE, OTHER, missing_required
from .inverter import (
    BMS_DEVICE,
    IMPLEMENTED_INVERTER_PLATFORMS,
    INVERTER_ENTITY_KEYS,
    PROFILE_PLATFORMS,
    SOURCE_BMS_KEYS,
    SOURCE_ENERGY_KEYS,
    SOURCE_POWER_KEYS,
    battery_schema,
    bms_device_schema,
    inverter_entities_schema,
    inverter_platform_schema,
    inverter_solis_pick_schema,
    inverter_solis_schema,
    platform_label,
    profile_manual_schema,
    profile_pick_schema,
    sources_manual_schema,
    sources_schema,
    sources_summary,
    validate_battery,
    validate_inverter_power,
)
from .sections import (
    PAGE_BATTERY,
    PAGE_BMS,
    PAGE_MAPPING,
    PAGE_PLATFORM,
    PAGE_SOURCES,
    PAGE_SOURCES_ENERGY,
    SECTION_INVERTER,
    STEP_BMS_DETAILS,
    STEP_SOURCES_MANUAL,
)


class InverterSteps(HubCore):
    """The Inverter and battery section menu and its pages."""

    async def async_step_inverter(self, user_input: dict | None = None) -> FlowResult:
        """Open the Inverter and battery menu."""
        return await self._async_show_section(SECTION_INVERTER)

    async def async_step_inverter_remove(self, user_input: dict | None = None) -> FlowResult:
        """Take the inverter and battery out of the installation."""
        return await self._async_remove_section(SECTION_INVERTER)

    async def async_step_inverter_platform(self, user_input: dict | None = None) -> FlowResult:
        """Collect the inverter platform + power ratings, auto-detecting the inverter."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        defaults = self._data
        if user_input is not None:
            platform = user_input[CONF_INVERTER_PLATFORM]
            errors = validate_inverter_power(user_input)
            if platform not in IMPLEMENTED_INVERTER_PLATFORMS:
                errors[CONF_INVERTER_PLATFORM] = "platform_not_implemented"
            if errors:
                return self._show_form(
                    step_id=PAGE_PLATFORM,
                    back_to=SECTION_INVERTER,
                    data_schema=inverter_platform_schema({**defaults, **user_input}),
                    errors=errors,
                    description_placeholders={"platform": platform_label(platform)},
                )
            if platform != self._platform():
                self._confirmed.discard(PAGE_MAPPING)
            self._data.update(user_input)
            if (auto := self._auto_mapping()) is not None:
                key, entry_id = auto
                self._data[key] = entry_id
                self._confirmed.add(PAGE_MAPPING)
            return await self._async_page_done(PAGE_PLATFORM, SECTION_INVERTER)

        return self._show_form(
            step_id=PAGE_PLATFORM,
            back_to=SECTION_INVERTER,
            data_schema=inverter_platform_schema(defaults),
            description_placeholders={"platform": platform_label(defaults.get(CONF_INVERTER_PLATFORM, ""))},
        )

    async def async_step_battery(self, user_input: dict | None = None) -> FlowResult:
        """Collect the battery parameters."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        errors: dict[str, str] = {}
        defaults = self._data
        if user_input is not None:
            errors = validate_battery(user_input)
            defaults = {**self._data, **user_input}
            if not errors:
                self._data.update(user_input)
                return await self._async_page_done(PAGE_BATTERY, SECTION_INVERTER)

        return self._show_form(
            step_id=PAGE_BATTERY,
            back_to=SECTION_INVERTER,
            errors=errors,
            data_schema=battery_schema(defaults),
        )

    async def async_step_inverter_mapping(self, user_input: dict | None = None) -> FlowResult:
        """Open the entity-mapping page that fits the chosen platform."""
        platform = self._platform()
        if platform == InverterPlatform.SOLIS.value:
            return await self.async_step_inverter_solis()
        if platform in PROFILE_PLATFORMS:
            return await self.async_step_inverter_generic()
        return await self.async_step_inverter_entities()

    async def async_step_inverter_generic(self, user_input: dict | None = None) -> FlowResult:
        """Auto-detect or manually map a Huawei/SolaX/Sungrow/GoodWe/Deye inverter.

        Mirrors the Solis step but is profile-driven: auto-detect the vendor
        config entry (single → confirmed, multiple → picker, none → manual),
        or go straight to manual mapping for profiles with no config-entry
        domain (Sungrow). The submitted values (a picked config-entry id, or the
        per-role entity ids) are stored verbatim; resolution happens at
        coordinator setup.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        profile = profile_for(InverterPlatform(self._platform()))
        if profile is None:  # defensive — routing only sends profile platforms here
            return await self.async_step_inverter_entities()
        errors: dict[str, str] = {}
        defaults = self._data
        if user_input is not None:
            # The picker always carries its entry id; the manual form must fill its required roles.
            if config_entry_key(profile.platform) not in user_input:
                required = [manual_role_key(profile.platform, spec.role) for spec in profile.roles if spec.required]
                errors = missing_required(user_input, required)
            if not errors:
                self._data.update(user_input)
                return await self._async_page_done(PAGE_MAPPING, SECTION_INVERTER)
            defaults = {**self._data, **user_input}
        if profile.config_entry_domain and not errors:
            entries = self.hass.config_entries.async_entries(profile.config_entry_domain)  # type: ignore[attr-defined]
            if len(entries) == 1:
                self._data[config_entry_key(profile.platform)] = entries[0].entry_id
                return await self._async_page_done(PAGE_MAPPING, SECTION_INVERTER)
            if len(entries) > 1:
                options = [{"value": e.entry_id, "label": e.title} for e in entries]
                return self._show_form(
                    step_id="inverter_generic",
                    back_to=SECTION_INVERTER,
                    data_schema=profile_pick_schema(profile, options, defaults),
                )
        return self._show_form(
            step_id="inverter_generic",
            back_to=SECTION_INVERTER,
            errors=errors,
            data_schema=profile_manual_schema(profile, defaults),
        )

    async def async_step_inverter_entities(self, user_input: dict | None = None) -> FlowResult:
        """Map the four standard inverter entities (non-Solis, non-profile platforms)."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = missing_required(user_input, INVERTER_ENTITY_KEYS)
            if not errors:
                self._data.update(user_input)
                return await self._async_page_done(PAGE_MAPPING, SECTION_INVERTER)

        return self._show_form(
            step_id="inverter_entities",
            back_to=SECTION_INVERTER,
            errors=errors,
            data_schema=inverter_entities_schema({**self._data, **(user_input or {})}),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Auto-detect solis_modbus, or show the picker / manual entity mapping.

        One solis_modbus config entry is taken silently, several get a compact
        picker, none falls back to the full manual entity mapping form.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        if user_input is not None:
            self._data.update(user_input)
            return await self._async_page_done(PAGE_MAPPING, SECTION_INVERTER)

        solis_entries = self.hass.config_entries.async_entries("solis_modbus")  # type: ignore[attr-defined]

        if len(solis_entries) == 1:
            self._data[CONF_SOLIS_CONFIG_ENTRY_ID] = solis_entries[0].entry_id
            return await self._async_page_done(PAGE_MAPPING, SECTION_INVERTER)

        if len(solis_entries) > 1:
            options = [{"value": e.entry_id, "label": e.title} for e in solis_entries]
            return self._show_form(
                step_id="inverter_solis",
                back_to=SECTION_INVERTER,
                data_schema=inverter_solis_pick_schema(options, self._data),
            )

        return self._show_form(
            step_id="inverter_solis",
            back_to=SECTION_INVERTER,
            data_schema=inverter_solis_schema(self._data),
        )

    # The sources page waiting on its by-hand form: which page, the keys it
    # holds, the values submitted so far and the ones still to be picked.
    _sources_pending: dict | None = None

    def _inverter_sources(self) -> dict[str, SourceOptions]:
        """Return the source candidates found on the inverter (overridable in tests)."""
        return detect_inverter_sources(self.hass, self._data)  # type: ignore[attr-defined]

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Collect the optional power sensors + BMS."""
        return await self._async_sources_page(PAGE_SOURCES, SOURCE_POWER_KEYS, user_input)

    async def async_step_sources_energy(self, user_input: dict | None = None) -> FlowResult:
        """Collect the optional energy counters, today's and yesterday's."""
        return await self._async_sources_page(PAGE_SOURCES_ENERGY, SOURCE_ENERGY_KEYS, user_input)

    async def _async_sources_page(
        self, page: str, keys: tuple[str, ...], user_input: dict | None,
    ) -> FlowResult:
        """Show or save one optional-sources page.

        Every row offers what was found on the inverter itself; a row set to
        *Other* is picked by hand on :meth:`async_step_sources_manual` instead,
        so one page can send several sources there at once.

        Args:
            page: The page's step id.
            keys: Its source keys, in display order.
            user_input: Submitted values, or None to show the form.

        Returns:
            The inverter menu once saved, the by-hand form when a row chose
            Other, or the page's form.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            self._sources_pending = None
            return moved
        if user_input is not None:
            if by_hand := [key for key in keys if user_input.get(key) == OTHER]:
                self._sources_pending = {"page": page, "keys": keys, "values": user_input, "by_hand": by_hand}
                return await self.async_step_sources_manual()
            return await self._async_save_sources(page, keys, user_input)

        detected = self._inverter_sources()
        return self._show_form(
            step_id=page,
            back_to=SECTION_INVERTER,
            data_schema=sources_schema(keys, self._data, detected),
            description_placeholders={"detected": sources_summary(keys, detected)},
        )

    async def async_step_sources_manual(self, user_input: dict | None = None) -> FlowResult:
        """Pick the sources whose row chose Other from every sensor on the system."""
        if (moved := await self._async_follow(user_input)) is not None:
            self._sources_pending = None
            return moved
        pending = self._sources_pending
        if pending is None:
            # Reached without a page's values (e.g. a stale browser tab).
            return await self.async_step_sources()
        if user_input is not None:
            # Absent means cleared here, whatever the sources page held.
            values = {**pending["values"], **{key: user_input.get(key) for key in pending["by_hand"]}}
            self._sources_pending = None
            return await self._async_save_sources(pending["page"], pending["keys"], values)

        return self._show_form(
            step_id=STEP_SOURCES_MANUAL,
            back_to=SECTION_INVERTER,
            data_schema=sources_manual_schema(pending["by_hand"], self._data),
        )

    # The picked battery device while its sensors are confirmed on bms_details.
    _bms_pending: dict | None = None

    def _bms_sources(self, device_id: str) -> dict[str, SourceOptions]:
        """Return a battery device's BMS source candidates (overridable in tests)."""
        return detect_bms_sources(self.hass, device_id, self._data)  # type: ignore[attr-defined]

    def _bms_device(self) -> str:
        """Return the device the stored BMS SoC sensor belongs to (overridable in tests)."""
        return device_of(self.hass, str(self._data.get(CONF_BMS_BATTERY_SOC) or ""))  # type: ignore[attr-defined]

    async def async_step_bms(self, user_input: dict | None = None) -> FlowResult:
        """Pick the battery device a dedicated BMS reports through.

        A BMS is separate hardware from the inverter, so it is chosen as a
        device and its SoC / pack-power sensors are resolved from it — the same
        shape as picking a metered device. Confirming with no device is how a
        BMS is taken away: the inverter becomes the only battery source again.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            self._bms_pending = None
            return moved
        errors: dict[str, str] = {}
        device_id = self._bms_device()
        if user_input is not None:
            device_id = str(user_input.get(BMS_DEVICE) or "")
            if not device_id:
                return await self._async_save_sources(PAGE_BMS, SOURCE_BMS_KEYS, {})
            detected = self._bms_sources(device_id)
            if CONF_BMS_BATTERY_SOC in detected:
                self._bms_pending = {"device_id": device_id, "detected": detected}
                return await self.async_step_bms_details()
            # Without a state of charge the chain in battery_source.py never
            # prefers the BMS, so accepting the device would change nothing.
            errors[BMS_DEVICE] = "bms_no_soc"

        return self._show_form(
            step_id=PAGE_BMS,
            back_to=SECTION_INVERTER,
            errors=errors,
            data_schema=bms_device_schema(device_id),
            # Confirm does not save this page — it resolves the device's sensors
            # and opens them, so the dropdown must not promise otherwise.
            save_label="✓ continue to the battery's sensors",
        )

    async def async_step_bms_details(self, user_input: dict | None = None) -> FlowResult:
        """Confirm which of the picked battery device's sensors the BMS is read from."""
        if (moved := await self._async_follow(user_input)) is not None:
            self._bms_pending = None
            return moved
        pending = self._bms_pending
        if pending is None:
            # Reached without a picked device (e.g. a stale browser tab).
            return await self.async_step_bms()
        if user_input is not None:
            if by_hand := [key for key in SOURCE_BMS_KEYS if user_input.get(key) == OTHER]:
                self._sources_pending = {
                    "page": PAGE_BMS, "keys": SOURCE_BMS_KEYS, "values": user_input, "by_hand": by_hand,
                }
                self._bms_pending = None
                return await self.async_step_sources_manual()
            self._bms_pending = None
            return await self._async_save_sources(PAGE_BMS, SOURCE_BMS_KEYS, user_input)

        return self._show_form(
            step_id=STEP_BMS_DETAILS,
            back_to=SECTION_INVERTER,
            data_schema=sources_schema(SOURCE_BMS_KEYS, self._data, pending["detected"]),
            description_placeholders={"device": self._ha_device_name(pending["device_id"]) or "the picked device"},
        )

    async def _async_save_sources(self, page: str, keys: tuple[str, ...], values: dict) -> FlowResult:
        """Store a sources page's values, confirm it and return to the inverter menu.

        Only the page's own keys are written, and each one absent or set to the
        None row is stored as "" — a source the user takes away has to actually
        go away, which a defaulted optional field could never express. A left-over
        ``OTHER`` clears too: the by-hand form always replaces the rows that chose
        it, so seeing one here means no entity was picked, and storing the
        sentinel as an entity id would be far worse than storing nothing.

        Args:
            page: The page being saved.
            keys: Its source keys.
            values: The submitted values, with any by-hand picks merged in.

        Returns:
            The inverter menu.
        """
        for key in keys:
            picked = values.get(key)
            self._data[key] = "" if picked in (None, "", NONE, OTHER) else picked
        return await self._async_page_done(page, SECTION_INVERTER)
