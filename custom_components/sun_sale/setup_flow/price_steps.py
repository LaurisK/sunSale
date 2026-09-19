"""Electricity-prices section steps: price feed, buy / sell price formulas and price level."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.data_entry_flow import FlowResult

from ..contract.const import (
    CONF_CURRENCY,
    CONF_NORDPOOL_ENTITY,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_LEVEL_CHEAP_BELOW,
    CONF_PRICE_LEVEL_CHEAP_SHARE,
    CONF_PRICE_LEVEL_EXPENSIVE_ABOVE,
    CONF_PRICE_LEVEL_EXPENSIVE_SHARE,
    CONF_PRICE_SOURCE,
    CONF_WEATHER_ENTITY,
    DEFAULT_CURRENCY,
    DEFAULT_PRICE_LEVEL_SHARE_PCT,
    DEFAULT_PRICE_SOURCE,
    FORMULA_SCHEDULES,
)
from ..inbound.pricing import detect_price_sensors
from ..inbound.weather import DetectedWeather, detect_weather_entities
from ..pipeline.tariff import ALL_SCHEDULE_KEYS, schedule_keys, tariff_count
from .core import HubCore
from .fields import NONE
from .prices import (
    PRICE_EXPORT_CHOICE,
    PRICE_EXPORT_NONE,
    PRICE_SENSOR_CHOICE,
    PRICE_SENSOR_OTHER,
    PRICE_SOURCE_SHORT_LABELS,
    detected_summary,
    price_forecast_schema,
    price_levels_schema,
    price_manual_schema,
    price_picker_schema,
    prices_schema,
    validate_export_choice,
    validate_price_feed,
    validate_price_levels,
    weather_summary,
)
from .sections import (
    PAGE_PRICE_FEED,
    PAGE_PRICE_FORECAST,
    PAGE_PRICE_LEVELS,
    SECTION_PRICES,
    STEP_PRICE_FEED_MANUAL,
)
from .tariffs import (
    SIDE_BUY,
    SIDE_CONF,
    SIDE_PAGES,
    SIDE_SELL,
    SIDES,
    apply_fees,
    apply_formula,
    fees_page,
    fees_schema,
    fees_summary,
    formula_page,
    formula_schema,
    schedule_form,
    schedule_form_values,
    schedule_label,
    schedule_page,
    schedule_schema,
    side_menu,
    validate_formula,
    validate_schedule,
)


class PriceSteps(HubCore):
    """The Electricity prices section menu, its pages and the buy / sell price menus."""

    # The schedule a direction's schedule form edits; set by the menu row that opened it.
    _schedule_key: str = ""

    async def async_step_prices(self, user_input: dict | None = None) -> FlowResult:
        """Open the Electricity prices menu."""
        return await self._async_show_section(SECTION_PRICES)

    async def async_step_prices_remove(self, user_input: dict | None = None) -> FlowResult:
        """Take electricity prices out of the installation."""
        return await self._async_remove_section(SECTION_PRICES)

    # The price-feed form's values while its Other row's form picks the source and sensor.
    _feed_pending: dict | None = None

    async def async_step_price_feed(self, user_input: dict | None = None) -> FlowResult:
        """Collect the price sensor, the export sensor, resolution and display currency.

        The sensors on this system a price translator can read are offered as
        a list whose Other row opens :meth:`async_step_price_feed_manual`; with
        none detected, the source and sensor are picked on this form.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        detected = detect_price_sensors(getattr(self, "hass", None))
        errors: dict[str, str] = {}
        defaults = self._data
        source = self._data.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE)
        choice = export_choice = None
        if user_input is not None:
            choice = user_input.pop(PRICE_SENSOR_CHOICE, None)
            export_choice = user_input.pop(PRICE_EXPORT_CHOICE, None)
            if export_choice is not None:
                user_input[CONF_PRICE_EXPORT_ENTITY] = "" if export_choice == PRICE_EXPORT_NONE else export_choice
            if choice == PRICE_SENSOR_OTHER:
                errors = validate_price_feed(user_input, needs_sensor=False)
                if not errors:
                    self._feed_pending = user_input
                    return await self.async_step_price_feed_manual()
            else:
                if choice is not None:
                    user_input[CONF_NORDPOOL_ENTITY] = choice
                    user_input[CONF_PRICE_SOURCE] = next(
                        (sensor.source for sensor in detected if sensor.entity_id == choice), source,
                    )
                source = user_input.get(CONF_PRICE_SOURCE, source)
                errors = {
                    **validate_price_feed(user_input, self._needs_feed()),
                    **validate_export_choice(detected, choice, export_choice),
                }
                if not errors:
                    return await self._async_save_feed(user_input, source)
            defaults = {**self._data, **user_input}

        picker = any(not sensor.export for sensor in detected)
        return self._show_form(
            step_id=PAGE_PRICE_FEED,
            back_to=SECTION_PRICES,
            errors=errors,
            data_schema=(
                price_picker_schema(defaults, detected, choice, export_choice) if picker else prices_schema(defaults)
            ),
            description_placeholders={
                "source": PRICE_SOURCE_SHORT_LABELS.get(source, source),
                "detected": detected_summary(detected),
            },
        )

    async def async_step_price_feed_manual(self, user_input: dict | None = None) -> FlowResult:
        """Collect the price source and sensor by hand (the detected-sensor list's Other row)."""
        if (moved := await self._async_follow(user_input)) is not None:
            self._feed_pending = None
            return moved
        if self._feed_pending is None:
            # Reached without the price-feed form's values (e.g. a stale browser tab).
            return await self.async_step_price_feed()
        errors: dict[str, str] = {}
        # The export sensor picked on the price-feed form pre-fills this one.
        defaults = {**self._data, **self._feed_pending}
        source = self._data.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE)
        if user_input is not None:
            # Absent means cleared here, whatever the price-feed form held.
            values = {
                **self._feed_pending,
                **user_input,
                CONF_PRICE_EXPORT_ENTITY: user_input.get(CONF_PRICE_EXPORT_ENTITY, ""),
            }
            source = values.get(CONF_PRICE_SOURCE, source)
            errors = validate_price_feed(values, self._needs_feed())
            if not errors:
                return await self._async_save_feed(values, source)
            defaults = {**self._data, **values}

        return self._show_form(
            step_id=STEP_PRICE_FEED_MANUAL,
            back_to=SECTION_PRICES,
            errors=errors,
            data_schema=price_manual_schema(defaults),
            description_placeholders={"source": PRICE_SOURCE_SHORT_LABELS.get(source, source)},
        )

    async def _async_save_feed(self, values: dict, source: str) -> FlowResult:
        """Store the price-feed values, confirm the page and return to the prices menu.

        Args:
            values: Validated price-feed values, including the sensor.
            source: The price source the sensor belongs to.

        Returns:
            The prices menu.
        """
        self._data.update(values)
        self._data[CONF_PRICE_SOURCE] = source
        # Every price-feed form shows an export field, so absent means none.
        self._data[CONF_PRICE_EXPORT_ENTITY] = values.get(CONF_PRICE_EXPORT_ENTITY) or ""
        self._data[CONF_CURRENCY] = str(values.get(CONF_CURRENCY, DEFAULT_CURRENCY)).strip().upper()
        self._feed_pending = None
        return await self._async_page_done(PAGE_PRICE_FEED, SECTION_PRICES)

    def _weather_entities(self) -> list[DetectedWeather]:
        """Return the weather entities on this system (overridable in tests)."""
        return detect_weather_entities(getattr(self, "hass", None))

    async def async_step_price_forecast(self, user_input: dict | None = None) -> FlowResult:
        """Choose which weather entity the week-ahead price forecast reads.

        The translator auto-detects one, which is why this page is optional —
        but it was previously the only input to a dispatch-affecting forecast
        that the user could neither see nor change.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        detected = self._weather_entities()
        if user_input is not None:
            picked = user_input.get(CONF_WEATHER_ENTITY)
            # Absent (a cleared plain picker) and the None row both mean "no
            # weather"; stored empty is what keeps detection from re-filling it.
            self._data[CONF_WEATHER_ENTITY] = "" if picked in (None, "", NONE) else picked
            return await self._async_page_done(PAGE_PRICE_FORECAST, SECTION_PRICES)

        return self._show_form(
            step_id=PAGE_PRICE_FORECAST,
            back_to=SECTION_PRICES,
            data_schema=price_forecast_schema(self._data, detected),
            description_placeholders={"detected": weather_summary(detected)},
        )

    async def async_step_price_levels(self, user_input: dict | None = None) -> FlowResult:
        """Collect what counts as a cheap / expensive price."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        errors: dict[str, str] = {}
        defaults = self._data
        if user_input is not None:
            errors = validate_price_levels(user_input)
            if not errors:
                for key in (CONF_PRICE_LEVEL_CHEAP_SHARE, CONF_PRICE_LEVEL_EXPENSIVE_SHARE):
                    self._data[key] = user_input.get(key, DEFAULT_PRICE_LEVEL_SHARE_PCT)
                # Absent means cleared: store None so an old limit can't linger.
                for key in (CONF_PRICE_LEVEL_CHEAP_BELOW, CONF_PRICE_LEVEL_EXPENSIVE_ABOVE):
                    self._data[key] = user_input.get(key)
                return await self._async_page_done(PAGE_PRICE_LEVELS, SECTION_PRICES)
            defaults = {**self._data, **user_input}

        return self._show_form(
            step_id=PAGE_PRICE_LEVELS,
            back_to=SECTION_PRICES,
            errors=errors,
            data_schema=price_levels_schema(defaults),
        )

    # --- Buy / sell price menus ----------------------------------------------

    async def _async_show_price_side(self, side: str) -> FlowResult:
        """Show a direction's menu: formula, grid fees, the schedules it needs, ← Back."""
        pages = self._side_pages(side)
        lines = [
            f"- ✓ **{SIDE_PAGES[page][1]}** — {self._page_summary(page)}"
            if self._page_done(page) else f"- · **{SIDE_PAGES[page][1]}** — still to confirm"
            for page in pages
        ]
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id=side_menu(side),
            menu_options=[*pages, SECTION_PRICES],
            description_placeholders={"status": "\n".join(lines)},
        )

    async def _async_side_page_done(self, side: str, page: str, formula: dict) -> FlowResult:
        """Store a direction's updated formula, mark the page confirmed and return to its menu."""
        self._data[SIDE_CONF[side]] = formula
        self._confirmed.add(page)
        return await self._async_show_price_side(side)

    async def _async_formula_step(self, side: str, user_input: dict | None) -> FlowResult:
        """Collect a direction's formula: energy, markup, VAT and grid-fee structure."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        formula = self._formula(side)
        errors: dict[str, str] = {}
        defaults = formula
        if user_input is not None:
            errors = validate_formula(user_input)
            if not errors:
                return await self._async_side_page_done(side, formula_page(side), apply_formula(formula, user_input))
            defaults = {**formula, **user_input}

        return self._show_form(
            step_id=formula_page(side),
            back_to=side_menu(side),
            errors=errors,
            data_schema=formula_schema(defaults),
        )

    async def _async_fees_step(self, side: str, user_input: dict | None) -> FlowResult:
        """Collect one grid fee per tariff of a direction."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        formula = self._formula(side)
        if user_input is not None:
            return await self._async_side_page_done(side, fees_page(side), apply_fees(formula, user_input))

        return self._show_form(
            step_id=fees_page(side),
            back_to=side_menu(side),
            data_schema=fees_schema(formula),
            description_placeholders={"count": str(tariff_count(formula))},
        )

    async def _async_open_schedule(self, side: str, key: str) -> FlowResult:
        """Open a direction's schedule form on one season / day type (a menu row)."""
        self._schedule_key = key
        return await self._async_schedule_step(side, None)

    async def _async_schedule_step(self, side: str, user_input: dict | None) -> FlowResult:
        """Collect the "from HH:MM → tariff" rows of the schedule opened last."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        formula = self._formula(side)
        key = self._schedule_key
        if key not in schedule_keys(formula):
            return await self._async_show_price_side(side)
        schedules = formula.get(FORMULA_SCHEDULES) or {}
        errors: dict[str, str] = {}
        values = schedule_form_values(schedules.get(key) or [])
        if user_input is not None:
            rows, errors = validate_schedule(user_input, tariff_count(formula))
            if not errors:
                updated = {**formula, FORMULA_SCHEDULES: {**schedules, key: rows}}
                return await self._async_side_page_done(side, schedule_page(side, key), updated)
            values = user_input

        return self._show_form(
            step_id=schedule_form(side),
            back_to=side_menu(side),
            errors=errors,
            data_schema=schedule_schema(formula, values),
            description_placeholders={"schedule": schedule_label(key), "fees": fees_summary(formula)},
        )

    async def async_step_price_buy(self, user_input: dict | None = None) -> FlowResult:
        """Open the Buy price menu."""
        return await self._async_show_price_side(SIDE_BUY)

    async def async_step_price_sell(self, user_input: dict | None = None) -> FlowResult:
        """Open the Sell price menu."""
        return await self._async_show_price_side(SIDE_SELL)

    async def async_step_price_buy_formula(self, user_input: dict | None = None) -> FlowResult:
        """Collect the buy price formula."""
        return await self._async_formula_step(SIDE_BUY, user_input)

    async def async_step_price_sell_formula(self, user_input: dict | None = None) -> FlowResult:
        """Collect the sell price formula."""
        return await self._async_formula_step(SIDE_SELL, user_input)

    async def async_step_price_buy_fees(self, user_input: dict | None = None) -> FlowResult:
        """Collect the buy-side grid fees."""
        return await self._async_fees_step(SIDE_BUY, user_input)

    async def async_step_price_sell_fees(self, user_input: dict | None = None) -> FlowResult:
        """Collect the sell-side grid fees."""
        return await self._async_fees_step(SIDE_SELL, user_input)

    async def async_step_price_buy_schedule(self, user_input: dict | None = None) -> FlowResult:
        """Collect a buy-side tariff schedule (the one its menu row opened)."""
        return await self._async_schedule_step(SIDE_BUY, user_input)

    async def async_step_price_sell_schedule(self, user_input: dict | None = None) -> FlowResult:
        """Collect a sell-side tariff schedule (the one its menu row opened)."""
        return await self._async_schedule_step(SIDE_SELL, user_input)


def _schedule_opener(side: str, key: str) -> Callable[..., Awaitable[FlowResult]]:
    """Return the menu-row step handler that opens one schedule of a direction.

    HA dispatches a menu click to ``async_step_<row>``, so each of the nine
    season / day-type schedules per direction needs its own handler; they all
    show the direction's one schedule form.

    Args:
        side: ``buy`` or ``sell``.
        key: The ``"<season>_<day type>"`` schedule key.

    Returns:
        An ``async_step_*`` method.
    """
    async def opener(self: PriceSteps, user_input: dict | None = None) -> FlowResult:
        """Open this schedule's form."""
        return await self._async_open_schedule(side, key)

    opener.__doc__ = f"Open the {side} price's {schedule_label(key).lower()}."
    return opener


for _side in SIDES:
    for _key in ALL_SCHEDULE_KEYS:
        setattr(PriceSteps, f"async_step_{schedule_page(_side, _key)}", _schedule_opener(_side, _key))
