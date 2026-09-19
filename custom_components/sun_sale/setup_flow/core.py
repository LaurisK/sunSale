"""Flow state, status text and menu navigation shared by every setup page."""
from __future__ import annotations

from typing import Any

from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr

from ..contract.const import (
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BMS_BATTERY_SOC,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_INVERTER_PLATFORM,
    CONF_NORDPOOL_ENTITY,
    CONF_PANEL_TITLE,
    CONF_PRICE_BUY,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_LEVEL_CHEAP_SHARE,
    CONF_PRICE_LEVEL_EXPENSIVE_SHARE,
    CONF_PRICE_SELL,
    CONF_PRICE_SOURCE,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_INVERTER_MAX_POWER_KW,
    DEFAULT_PRICE_LEVEL_SHARE_PCT,
    DEFAULT_PRICE_SOURCE,
    ENERGY_DYNAMIC,
    FORMULA_ENERGY,
    FORMULA_SCHEDULES,
)
from ..contract.install_capabilities import (
    CAP_INVERTER,
    CAP_PRICES,
    CAP_SOLAR_FORECAST,
    resolve_install_capabilities,
)
from ..inbound.forecast_resolver import manual_forecast_entities
from ..inbound.platform_profiles import config_entry_key, profile_for
from ..outbound.inverter import InverterPlatform
from ..pipeline.tariff import default_formula_config, price_config, schedule_keys
from .fields import GO_TO, SAVE, next_options, with_next
from .inverter import PROFILE_PLATFORMS, SOURCE_ENERGY_KEYS, SOURCE_POWER_KEYS, platform_label
from .prices import PRICE_SOURCE_SHORT_LABELS
from .sections import (
    BACK_NAMES,
    HUB_FINISH,
    HUB_NAME,
    OPTIONAL_PAGE_HINTS,
    PAGE_BATTERY,
    PAGE_BMS,
    PAGE_LABELS,
    PAGE_MAPPING,
    PAGE_PLATFORM,
    PAGE_PRICE_FEED,
    PAGE_PRICE_FORECAST,
    PAGE_PRICE_LEVELS,
    PAGE_SOURCES,
    PAGE_SOURCES_ENERGY,
    PRICE_SIDE_MENUS,
    SECTION_FORECAST,
    SECTION_INVERTER,
    SECTION_LABELS,
    SECTION_PAGES,
    SECTION_PRICES,
)
from .tariffs import (
    SCHEDULE_PAGES,
    SIDE_CONF,
    SIDE_PAGES,
    SIDES,
    fees_page,
    fees_summary,
    formula_page,
    formula_summary,
    schedule_page,
    schedule_summary,
    side_menu,
)


class HubCore:
    """State and navigation every setup page builds on.

    Both flows accumulate into ``self._data`` (the options flow seeds it with
    the entry's merged data + options, so it doubles as the form defaults and
    editing one page never drops another's values) and track confirmed pages
    in ``self._confirmed``. The per-section step mixins subclass this; the
    concrete flows set ``_HUB_STEP_ID`` and implement ``_async_finish``.

    Attributes:
        _HUB_STEP_ID: The root-menu step id — ``hub`` in the config flow,
            ``init`` in the options flow.
        _data: The accumulated config / options values.
        _confirmed: Page ids whose Confirm button was pressed.
    """

    _HUB_STEP_ID: str
    _BACK_NAMES = BACK_NAMES
    _data: dict[str, Any]
    _confirmed: set[str]

    async def _async_finish(self) -> FlowResult:
        """Persist ``self._data`` — a new entry, or the entry's options.

        Raises:
            NotImplementedError: The concrete flow supplies this.
        """
        raise NotImplementedError

    # --- State ---------------------------------------------------------------

    def _platform(self) -> str:
        """Return the chosen inverter platform value (``generic`` until chosen)."""
        return self._data.get(CONF_INVERTER_PLATFORM, InverterPlatform.GENERIC.value)

    def _formula(self, side: str) -> dict[str, Any]:
        """Return a price direction's formula dict with defaults filled in.

        An entry that predates the formulas reads its converted legacy tariff.
        """
        stored = self._data.get(SIDE_CONF[side])
        if not isinstance(stored, dict):
            stored = price_config(self._data)[SIDES.index(side)]
        return {**default_formula_config(), **stored}

    def _side_pages(self, side: str) -> list[str]:
        """Return a price direction's pages: formula, grid fees and the schedules it needs."""
        keys = schedule_keys(self._formula(side))
        return [formula_page(side), fees_page(side), *(schedule_page(side, key) for key in keys)]

    def _needs_feed(self) -> bool:
        """Return True when the buy or sell price follows the market, so a price sensor is needed."""
        return any(self._formula(side)[FORMULA_ENERGY] == ENERGY_DYNAMIC for side in SIDES)

    def _ha_device_name(self, ha_device_id: str) -> str:
        """Return a Home Assistant device's display name, or "" (overridable in tests).

        The user's rename wins over the integration's name, which is what the
        BMS page shows back so the picked device is recognisable.

        Args:
            ha_device_id: The HA device-registry id.

        Returns:
            The display name, or "" when the device is unknown.
        """
        device = dr.async_get(self.hass).async_get(ha_device_id)  # type: ignore[attr-defined]
        if device is None:
            return ""
        return device.name_by_user or device.name or ""

    def _auto_mapping(self) -> tuple[str, str] | None:
        """Return ``(config key, entry id)`` when the platform's inverter is auto-detected.

        Solis and the profile platforms with a vendor config-entry domain are
        auto-detected when exactly one such entry exists; there is then nothing
        to map by hand.
        """
        platform = self._platform()
        if platform == InverterPlatform.SOLIS.value:
            domain, key = "solis_modbus", CONF_SOLIS_CONFIG_ENTRY_ID
        elif platform in PROFILE_PLATFORMS:
            profile = profile_for(InverterPlatform(platform))
            if profile is None or not profile.config_entry_domain:
                return None
            domain, key = profile.config_entry_domain, config_entry_key(profile.platform)
        else:
            return None
        entries = self.hass.config_entries.async_entries(domain)  # type: ignore[attr-defined]
        return (key, entries[0].entry_id) if len(entries) == 1 else None

    def _required_pages(self, section: str) -> list[str]:
        """Return the pages a section needs before it is complete.

        Optional pages (price level, energy counters) have working defaults
        and never block Finish; the solar forecast is optional too. The price
        source is only required while a price follows the market.
        """
        if section == SECTION_PRICES:
            pages = [PAGE_PRICE_FEED] if self._needs_feed() else []
            for side in SIDES:
                pages += self._side_pages(side)
            return pages
        if section == SECTION_INVERTER:
            return [PAGE_PLATFORM, PAGE_BATTERY, PAGE_MAPPING]
        return []

    def _page_done(self, page: str) -> bool:
        """Return True when a page is set: its form was confirmed and what it holds is still usable.

        The price source also needs its sensor while a price follows the
        market, and a schedule its rows (changing the tariff count can drop them).
        """
        if page not in self._confirmed:
            return False
        if page == PAGE_PRICE_FEED:
            return not self._needs_feed() or bool(self._data.get(CONF_NORDPOOL_ENTITY))
        if page in SCHEDULE_PAGES:
            side, key = SCHEDULE_PAGES[page]
            return bool((self._formula(side).get(FORMULA_SCHEDULES) or {}).get(key))
        return True

    def _missing_pages(self, section: str) -> list[str]:
        """Return the section's required pages that are not set yet."""
        return [page for page in self._required_pages(section) if not self._page_done(page)]

    def _included(self, section: str) -> bool:
        """Return True once the section is part of the installation.

        A section joins the installation by having one of its pages confirmed.
        """
        return any(page in self._confirmed for page in SECTION_PAGES[section])

    def _complete(self, section: str) -> bool:
        """Return True when the section is part of the installation and fully set."""
        return self._included(section) and not self._missing_pages(section)

    def _has_real_inverter(self) -> bool:
        """Return True when the inverter section is complete."""
        return self._complete(SECTION_INVERTER)

    def _seed_confirmed(self) -> None:
        """Mark every page an existing entry already holds, so options can save at once.

        A legacy tariff is converted into the buy / sell formulas first, so the
        price pages open on the prices the entry already produces.
        """
        self._data[CONF_PRICE_BUY], self._data[CONF_PRICE_SELL] = price_config(self._data)
        caps = resolve_install_capabilities(self._data)
        if caps[CAP_PRICES]:
            self._confirmed |= {PAGE_PRICE_FEED, *SIDE_PAGES, PAGE_PRICE_LEVELS, PAGE_PRICE_FORECAST}
        if caps[CAP_INVERTER]:
            self._confirmed |= {PAGE_PLATFORM, PAGE_SOURCES, PAGE_SOURCES_ENERGY}
            if self._data.get(CONF_BMS_BATTERY_SOC):
                self._confirmed.add(PAGE_BMS)
            self._confirmed |= {PAGE_BATTERY, PAGE_MAPPING}
        if caps[CAP_SOLAR_FORECAST]:
            self._confirmed.add(SECTION_FORECAST)

    # --- Status text ---------------------------------------------------------

    def _section_summary(self, section: str) -> str:
        """Return a short description of a section's current setting."""
        if section == SECTION_PRICES:
            return self._page_summary(PAGE_PRICE_FEED) if self._needs_feed() else "fixed prices"
        if section == SECTION_INVERTER:
            return platform_label(self._platform())
        parts = []
        if devices := self._data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []:
            parts.append(f"{len(devices)} forecast device(s)")
        if sensors := manual_forecast_entities(self._data):
            parts.append(f"{len(sensors)} forecast sensor(s)")
        return " + ".join(parts) or "no forecast selected"

    def _page_summary(self, page: str) -> str:
        """Return a short description of one set page's current values."""
        d = self._data
        if page == PAGE_PRICE_FEED:
            source = d.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE)
            label = PRICE_SOURCE_SHORT_LABELS.get(source, source)
            export = d.get(CONF_PRICE_EXPORT_ENTITY)
            return f"{label}, {d.get(CONF_NORDPOOL_ENTITY) or 'no sensor'}" + (f", export {export}" if export else "")
        if page in SIDE_PAGES:
            side = SIDE_PAGES[page][0]
            formula = self._formula(side)
            if page == formula_page(side):
                return formula_summary(side, formula)
            if page == fees_page(side):
                return fees_summary(formula)
            return schedule_summary(formula, SCHEDULE_PAGES[page][1])
        if page == PAGE_PRICE_FORECAST:
            return f"weather {d[CONF_WEATHER_ENTITY]}" if d.get(CONF_WEATHER_ENTITY) else "no weather"
        if page == PAGE_PRICE_LEVELS:
            cheap = d.get(CONF_PRICE_LEVEL_CHEAP_SHARE, DEFAULT_PRICE_LEVEL_SHARE_PCT)
            expensive = d.get(CONF_PRICE_LEVEL_EXPENSIVE_SHARE, DEFAULT_PRICE_LEVEL_SHARE_PCT)
            return f"cheapest {cheap:g} % cheap, dearest {expensive:g} % expensive"
        if page == PAGE_PLATFORM:
            max_kw = d.get(CONF_INVERTER_MAX_POWER_KW, DEFAULT_INVERTER_MAX_POWER_KW)
            return f"{platform_label(self._platform())}, {max_kw:g} kW"
        if page == PAGE_BATTERY:
            return (
                f"{d.get(CONF_BATTERY_NOMINAL_CAPACITY, 0.0):g} kWh, SoC "
                f"{d.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC):g}–"
                f"{d.get(CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC):g} %"
            )
        if page == PAGE_MAPPING:
            return "auto-detected" if self._auto_mapping() else "entities mapped"
        if page == PAGE_BMS:
            return d[CONF_BMS_BATTERY_SOC] if d.get(CONF_BMS_BATTERY_SOC) else "battery read from the inverter"
        if page in (PAGE_SOURCES, PAGE_SOURCES_ENERGY):
            keys = SOURCE_POWER_KEYS if page == PAGE_SOURCES else SOURCE_ENERGY_KEYS
            count = sum(1 for key in keys if d.get(key))
            return f"{count} of {len(keys)} set" if count else "none set"
        return ""

    def _page_line(self, section: str, page: str) -> str:
        """Render one section-menu status line: ✓ set, · still needed, ○ optional."""
        label = PAGE_LABELS[page]
        if page in PRICE_SIDE_MENUS:
            side = PRICE_SIDE_MENUS[page]
            missing = [p for p in self._side_pages(side) if not self._page_done(p)]
            if missing:
                return f"- · **{label}** — still to do: {', '.join(SIDE_PAGES[p][1] for p in missing)}"
            return f"- ✓ **{label}** — {formula_summary(side, self._formula(side))}"
        if self._page_done(page):
            return f"- ✓ **{label}** — {self._page_summary(page)}"
        if page in self._required_pages(section):
            return f"- · **{label}** — still to confirm"
        return f"- ○ **{label}** — optional: {OPTIONAL_PAGE_HINTS.get(page, 'not set')}"

    def _hub_line(self, section: str) -> str:
        """Render one root status line: ✓ complete, · unfinished, ○ not used."""
        label = SECTION_LABELS[section]
        if section == SECTION_FORECAST:
            if self._data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or manual_forecast_entities(self._data):
                return f"- ✓ **{label}** — {self._section_summary(section)}"
            return f"- ○ **{label}** — optional: none selected"
        if self._complete(section):
            return f"- ✓ **{label}** — {self._section_summary(section)}"
        if self._included(section):
            missing = ", ".join(PAGE_LABELS[page] for page in self._missing_pages(section))
            return f"- · **{label}** — still to do: {missing}"
        return f"- ○ **{label}** — not part of this installation"

    def _hub_sections(self) -> list[str]:
        """Return the root's section rows; Solar forecast needs a real inverter behind it."""
        sections = [SECTION_PRICES, SECTION_INVERTER]
        if self._has_real_inverter():
            sections.append(SECTION_FORECAST)
        return sections

    # --- Navigation ----------------------------------------------------------

    async def _async_follow(self, user_input: dict | None) -> FlowResult | None:
        """Return where a form's Confirm dropdown pointed, or None when it saves.

        Every form has one button, so "what Confirm does" rides in the GO_TO
        selector: the reserved SAVE value means save this page, any other value
        is the parent window's step id (see :func:`fields.next_options`) and
        navigates there without saving. Popping the key keeps it out of
        ``self._data`` on a normal Confirm.

        Args:
            user_input: Submitted form values.

        Returns:
            The parent window's flow result, or None to save the page.
        """
        if user_input is None:
            return None
        target = user_input.pop(GO_TO, SAVE)
        if target == SAVE or target not in self._BACK_NAMES:
            return None
        return await getattr(self, f"async_step_{target}")(None)

    def _show_form(self, *, back_to: str, save_label: str | None = None, **kwargs: Any) -> FlowResult:
        """Show a page form with the Confirm dropdown as its last field.

        The dropdown offers "✓ save and go back to <parent>" (default) and
        "✕ discard and go back to <parent>"; ``_async_follow`` routes the
        discard option back to ``back_to``. Every other kwarg is passed to
        :meth:`async_show_form`.

        Args:
            back_to: Step id of the parent window the discard option returns to.
            save_label: Replaces the default option's text when Confirm does
                not save and go back.

        Returns:
            The HA form step.
        """
        kwargs["data_schema"] = with_next(
            kwargs["data_schema"], next_options(back_to, self._BACK_NAMES[back_to], save_label)
        )
        return self.async_show_form(**kwargs)  # type: ignore[attr-defined]

    # --- Menus ---------------------------------------------------------------

    async def _async_show_hub(self, error: str = "") -> FlowResult:
        """Show the root menu, rebuilt from the current state on every visit.

        Args:
            error: Why Finish was refused, shown above the status (blank: none).
        """
        sections = self._hub_sections()
        lines = [self._hub_line(section) for section in sections]
        name = str(self._data.get(CONF_PANEL_TITLE) or "").strip()
        lines.append(f"- **Installation name** — {name or 'sunSale (default)'}")
        status = "\n".join(lines)
        if error:
            status = f"⚠ **{error}**\n\n{status}"
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id=self._HUB_STEP_ID,
            menu_options=[*sections, HUB_NAME, HUB_FINISH],
            description_placeholders={"status": status},
        )

    def _section_page_rows(self, section: str) -> list[str]:
        """Return the page rows a section menu offers, in walk-through order."""
        if section == SECTION_PRICES:
            return [
                PAGE_PRICE_FEED, *(side_menu(side) for side in SIDES), PAGE_PRICE_LEVELS, PAGE_PRICE_FORECAST,
            ]
        pages = [PAGE_PLATFORM, PAGE_BATTERY, PAGE_BMS]
        # An auto-detected inverter has nothing to map by hand.
        auto_mapped = PAGE_MAPPING in self._confirmed and self._auto_mapping() is not None
        if PAGE_PLATFORM in self._confirmed and not auto_mapped:
            pages.append(PAGE_MAPPING)
        return [*pages, PAGE_SOURCES, PAGE_SOURCES_ENERGY]

    async def _async_show_section(self, section: str) -> FlowResult:
        """Show a section menu: its pages →, ✕ Remove once included, ← Back."""
        pages = self._section_page_rows(section)
        options = list(pages)
        if self._included(section):
            options.append(f"{section}_remove")
        options.append(self._HUB_STEP_ID)
        # An auto-detected mapping has no row but still earns its ✓ line.
        status_pages = list(pages)
        if PAGE_BATTERY in pages and PAGE_MAPPING not in pages and PAGE_MAPPING in self._confirmed:
            status_pages.insert(pages.index(PAGE_BATTERY) + 1, PAGE_MAPPING)
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id=section,
            menu_options=options,
            description_placeholders={"status": "\n".join(self._page_line(section, page) for page in status_pages)},
        )

    async def _async_page_done(self, page: str, section: str) -> FlowResult:
        """Mark a page confirmed and return to its section menu."""
        self._confirmed.add(page)
        return await self._async_show_section(section)

    async def _async_remove_section(self, section: str) -> FlowResult:
        """Take a section out of the installation and return to the root.

        Stored values are kept, so adding the section back pre-fills every
        page; Finish writes the placeholders ``async_setup`` needs for a
        section that is not part of the installation.
        """
        self._confirmed -= set(SECTION_PAGES[section])
        return await self._async_show_hub()
