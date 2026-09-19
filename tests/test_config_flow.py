"""Tests for config_flow.py — the shared setup menu tree and both flows."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.sun_sale.config_flow import SunSaleConfigFlow, SunSaleOptionsFlow
from custom_components.sun_sale.contract.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_CURRENCY,
    CONF_INSTALL_CAPABILITIES,
    CONF_INVERTER_EXPORT_LIMIT_KW,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_INVERTER_PLATFORM,
    CONF_NORDPOOL_ENTITY,
    CONF_PANEL_TITLE,
    CONF_PRICE_BUY,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_LEVEL_CHEAP_BELOW,
    CONF_PRICE_LEVEL_CHEAP_SHARE,
    CONF_PRICE_LEVEL_EXPENSIVE_ABOVE,
    CONF_PRICE_LEVEL_EXPENSIVE_SHARE,
    CONF_PRICE_SELL,
    CONF_PRICE_SOURCE,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CONF_TARIFF_WEEKDAY_BANDS,
    CONF_WEATHER_ENTITY,
)
from custom_components.sun_sale.contract.install_capabilities import (
    CAP_INVERTER,
    CAP_PRICES,
    CAP_SOLAR_FORECAST,
    resolve_install_capabilities,
)
from custom_components.sun_sale.inbound.forecast_resolver import (
    DetectedForecast,
    combine_forecast_entities,
    manual_forecast_entities,
)
from custom_components.sun_sale.inbound.inverter_sources import DetectedSource, SourceOptions
from custom_components.sun_sale.inbound.pricing import (
    DetectedPriceSensor,
    detect_price_sensors,
    is_export_feed,
    price_source_of,
)
from custom_components.sun_sale.inbound.weather import DetectedWeather
from custom_components.sun_sale.outbound.inverter import InverterPlatform
from custom_components.sun_sale.pipeline.tariff import ALL_SCHEDULE_KEYS
from custom_components.sun_sale.setup_flow.fields import GO_TO, NONE, OTHER, SAVE, detected_options
from custom_components.sun_sale.setup_flow.forecast import FORECAST_DETECTED
from custom_components.sun_sale.setup_flow.inverter import source_rows
from custom_components.sun_sale.setup_flow.prices import (
    PRICE_EXPORT_CHOICE,
    PRICE_EXPORT_NONE,
    PRICE_SENSOR_CHOICE,
    PRICE_SENSOR_OTHER,
    detected_label,
    export_choice_default,
    price_choice_default,
)
from custom_components.sun_sale.setup_flow.tariffs import schedule_form_values

_STRINGS = json.loads(
    (Path(__file__).resolve().parents[1] / "custom_components/sun_sale/strings.json").read_text()
)

# The tariff keys of an entry from before the buy / sell formulas (converted on read).
LEGACY_TARIFF = {
    CONF_TARIFF_DISTRIBUTION_FEE: 0.03,
    CONF_TARIFF_TAX_RATE: 21.0,
    CONF_TARIFF_MARKUP: 0.005,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE: 0.01,
    CONF_TARIFF_SELL_TAX_RATE: 0.0,
    CONF_TARIFF_SELL_MARKUP: 0.0,
}

FORMULA_INPUT = {
    "energy": "dynamic", "fixed_price": 0.0, "markup": 0.005, "vat": 21.0, "tariffs": "1",
    "weekends": False, "holidays": False, "seasons": False, "summer_start": "04-01", "winter_start": "11-01",
}
FIXED_INPUT = {**FORMULA_INPUT, "energy": "fixed", "fixed_price": 0.2}

VALID_BATTERY_INPUT = {
    CONF_BATTERY_NOMINAL_CAPACITY: 10.0,
    CONF_BATTERY_PURCHASE_PRICE: 5000.0,
    CONF_BATTERY_RATED_CYCLE_LIFE: 6000,
    CONF_BATTERY_MAX_CHARGE_POWER: 5.0,
    CONF_BATTERY_MAX_DISCHARGE_POWER: 5.0,
    CONF_BATTERY_MIN_SOC: 10,
    CONF_BATTERY_MAX_SOC: 95,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 90,
    CONF_BATTERY_NOMINAL_VOLTAGE: 48.0,
}

_ROOT_EMPTY = ["prices", "inverter", "installation_name", "finish"]


def _flow_with_hass(
    solis_entries: list | None = None, forecast_entries: list | None = None,
) -> SunSaleConfigFlow:
    """Return a SunSaleConfigFlow with a mock hass pre-configured with entries.

    ``async_entries`` is domain-aware: solis_modbus returns ``solis_entries``;
    recognised forecast domains return ``forecast_entries`` (default empty).
    """
    flow = SunSaleConfigFlow()
    flow.hass = _mock_hass(solis_entries or [], forecast_entries or [])
    return flow


def _mock_hass(solis_entries: list, forecast_entries: list) -> MagicMock:
    """Return a hass mock whose ``async_entries`` answers per domain."""
    mock_hass = MagicMock()

    def _async_entries(domain):
        if domain == "solis_modbus":
            return solis_entries
        return [e for e in forecast_entries if getattr(e, "domain", None) == domain]

    mock_hass.config_entries.async_entries.side_effect = _async_entries
    return mock_hass


def _single_solis_entry() -> list:
    """Return one discovered solis_modbus config entry."""
    entry = MagicMock()
    entry.entry_id = "solis_entry_abc"
    return [entry]


async def _set_up_prices(flow) -> dict:
    """Confirm the required prices pages (Nord Pool, buy and sell formula + fee); return the prices menu."""
    await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.nordpool"})
    for side in ("buy", "sell"):
        await getattr(flow, f"async_step_price_{side}_formula")(FORMULA_INPUT)
        await getattr(flow, f"async_step_price_{side}_fees")({"fee_1": 0.03})
    return await flow.async_step_prices()


async def _set_up_solis(flow) -> dict:
    """Confirm platform (auto-detected Solis) + battery; return the inverter menu."""
    await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    return await flow.async_step_battery(VALID_BATTERY_INPUT)


def _status(result: dict) -> str:
    """Return a menu's status markdown."""
    return result["description_placeholders"]["status"]


def _detected(result: dict) -> str:
    """Return a form's "what was found" description text."""
    return result["description_placeholders"]["detected"]


# ---------------------------------------------------------------------------
# Root menu — the old checklist and hub in one screen
# ---------------------------------------------------------------------------

async def test_user_opens_on_the_root_menu():
    result = await SunSaleConfigFlow().async_step_user(None)
    assert result["type"] == "menu"
    assert result["step_id"] == "hub"
    assert result["menu_options"] == _ROOT_EMPTY
    assert _status(result).count("not part of this installation") == 2


async def test_finish_on_an_empty_installation_explains_instead_of_saving():
    result = await SunSaleConfigFlow().async_step_finish()
    assert result["type"] == "menu"
    assert result["step_id"] == "hub"
    assert _status(result).startswith("⚠ **Nothing to save yet.")


async def test_finish_refuses_an_unfinished_section():
    flow = _flow_with_hass()
    await _set_up_prices(flow)
    # No solis_modbus entry → the mapping page is still needed.
    await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    result = await flow.async_step_finish()
    assert result["type"] == "menu"
    assert "⚠ **Not finished: Inverter and battery." in _status(result)
    assert "· **Inverter and battery** — still to do: Battery, Entity mapping" in _status(result)


async def test_installation_name_is_stored_and_clearable():
    flow = SunSaleConfigFlow()
    form = await flow.async_step_installation_name(None)
    assert form["step_id"] == "installation_name"
    result = await flow.async_step_installation_name({CONF_PANEL_TITLE: "  Flat 3 "})
    assert result["step_id"] == "hub"
    assert flow._data[CONF_PANEL_TITLE] == "Flat 3"
    assert "**Installation name** — Flat 3" in _status(result)
    # A cleared optional field is submitted as absent.
    result = await flow.async_step_installation_name({})
    assert flow._data[CONF_PANEL_TITLE] == ""
    assert "sunSale (default)" in _status(result)


# ---------------------------------------------------------------------------
# Prices section
# ---------------------------------------------------------------------------

async def test_prices_menu_lists_its_pages_and_a_back_row():
    result = await SunSaleConfigFlow().async_step_prices()
    assert result["type"] == "menu"
    assert result["step_id"] == "prices"
    assert result["menu_options"] == [
        "price_feed", "price_buy", "price_sell", "price_levels", "price_forecast", "hub",
    ]
    assert "· **Price source and sensor** — still to confirm" in _status(result)
    assert "· **Buy price** — still to do: Price formula, Grid fees" in _status(result)
    assert "○ **Price level** — optional" in _status(result)


async def test_confirming_a_page_returns_to_its_menu_and_adds_the_section():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.nordpool"})
    assert result["step_id"] == "prices"
    assert "prices_remove" in result["menu_options"]
    assert "✓ **Price source and sensor** — Nord Pool, sensor.nordpool" in _status(result)
    root = await flow.async_step_hub()
    assert "· **Electricity prices** — still to do: Buy price: price formula, Buy price: grid fees" in _status(root)


def _with_weather(flow, detected: list[tuple[str, bool]]):
    """Give the flow detected weather entities: ``[(entity_id, in_use), …]``."""
    flow._weather_entities = lambda: [DetectedWeather(entity_id, "", auto) for entity_id, auto in detected]
    return flow


async def test_the_weather_the_price_forecast_reads_is_shown_and_can_be_changed():
    """It auto-detects, so the page is optional — but it was previously invisible."""
    flow = _with_weather(_flow_with_hass(), [("weather.forecast_home", True), ("weather.other", False)])
    form = await flow.async_step_price_forecast(None)
    assert form["step_id"] == "price_forecast"
    assert "Found 2 weather entities on this system" in form["description_placeholders"]["detected"]

    result = await flow.async_step_price_forecast({CONF_WEATHER_ENTITY: "weather.other"})
    assert result["step_id"] == "prices"
    assert flow._data[CONF_WEATHER_ENTITY] == "weather.other"
    assert "✓ **Price forecast weather** — weather weather.other" in _status(result)


async def test_the_weather_correction_can_be_turned_off():
    """Stored empty, so detection cannot quietly re-enable it on the next visit."""
    flow = _with_weather(_flow_with_hass(), [("weather.forecast_home", True)])
    result = await flow.async_step_price_forecast({CONF_WEATHER_ENTITY: NONE})
    assert flow._data[CONF_WEATHER_ENTITY] == ""
    assert "✓ **Price forecast weather** — no weather" in _status(result)


async def test_with_no_weather_integration_the_page_says_the_forecast_uses_history_alone():
    form = await _with_weather(_flow_with_hass(), []).async_step_price_forecast(None)
    assert "No weather entity was found on this system" in form["description_placeholders"]["detected"]


async def test_the_price_forecast_page_is_reachable_without_an_inverter():
    """Weather feeds the *price* forecast, so a prices-only install must still see it."""
    flow = _flow_with_hass()
    assert "price_forecast" in (await flow.async_step_prices())["menu_options"]
    assert "forecast" not in (await flow.async_step_hub())["menu_options"]


async def test_removing_prices_takes_the_section_out_but_keeps_its_values():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.nordpool"})
    result = await flow.async_step_prices_remove()
    assert result["step_id"] == "hub"
    assert "○ **Electricity prices** — not part of this installation" in _status(result)
    assert flow._data[CONF_NORDPOOL_ENTITY] == "sensor.nordpool"
    assert "prices_remove" not in (await flow.async_step_prices())["menu_options"]


async def test_price_feed_requires_a_sensor_for_a_live_source():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_feed({CONF_PRICE_SOURCE: "entsoe"})
    assert result["step_id"] == "price_feed"
    assert result["errors"] == {CONF_NORDPOOL_ENTITY: "price_entity_required"}
    # The error names the source the user just picked, not the stored one.
    assert result["description_placeholders"]["source"] == "ENTSO-e"


async def test_price_feed_stores_the_source():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_feed({CONF_PRICE_SOURCE: "entsoe", CONF_NORDPOOL_ENTITY: "sensor.entsoe"})
    assert flow._data[CONF_PRICE_SOURCE] == "entsoe"


async def test_price_feed_checks_and_normalises_the_currency():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.np", CONF_CURRENCY: "euro"})
    assert result["errors"] == {CONF_CURRENCY: "invalid_currency"}
    await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.np", CONF_CURRENCY: " gbp "})
    assert flow._data[CONF_CURRENCY] == "GBP"


async def test_fixed_prices_need_no_price_sensor():
    flow = SunSaleConfigFlow()
    for side in ("buy", "sell"):
        await getattr(flow, f"async_step_price_{side}_formula")(FIXED_INPUT)
        await getattr(flow, f"async_step_price_{side}_fees")({"fee_1": 0.05})
    menu = await flow.async_step_prices()
    assert "○ **Price source and sensor** — optional: not needed while both prices are fixed" in _status(menu)
    assert "✓ **Electricity prices** — fixed prices" in _status(await flow.async_step_hub())
    # The page still saves without a sensor (it also holds the currency).
    assert (await flow.async_step_price_feed({CONF_CURRENCY: "SEK"}))["step_id"] == "prices"


async def test_a_confirmed_feed_needs_its_sensor_again_once_a_price_follows_the_market():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_buy_formula(FIXED_INPUT)
    await flow.async_step_price_sell_formula(FIXED_INPUT)
    await flow.async_step_price_feed({})
    assert "✓ **Price source and sensor**" in _status(await flow.async_step_prices())
    await flow.async_step_price_buy_formula(FORMULA_INPUT)
    assert "· **Price source and sensor** — still to confirm" in _status(await flow.async_step_prices())


# ---------------------------------------------------------------------------
# Price sensor detection — the price-feed form offers what is on the system
# ---------------------------------------------------------------------------

def _price_state(entity_id: str, **attributes) -> SimpleNamespace:
    """Return a sensor state with these attributes and a friendly name."""
    return SimpleNamespace(entity_id=entity_id, attributes={"friendly_name": f"Name of {entity_id}", **attributes})


_NORDPOOL_STATE = _price_state("sensor.nordpool_kwh_lt", raw_today=[{"start": "2026-09-15T00:00", "value": 0.1}])
_AMBER_STATE = _price_state("sensor.amber_general_forecast", forecasts=[{"start_time": "x", "per_kwh": 25.0}])
_AMBER_FEED_IN_STATE = _price_state(
    "sensor.amber_feed_in_forecast", channel_type="feedIn", forecasts=[{"start_time": "x", "per_kwh": -8.0}],
)
_OCTOPUS_RATES = [{"start": "x", "value_inc_vat": 0.2}]
_OCTOPUS_STATE = _price_state("sensor.octopus_electricity_1_current_rate", is_export=False, rates=_OCTOPUS_RATES)
_OCTOPUS_EXPORT_STATE = _price_state(
    "sensor.octopus_electricity_2_export_current_rate", is_export=True, rates=_OCTOPUS_RATES,
)


def _flow_detecting(*states) -> SunSaleConfigFlow:
    """Return a config flow whose hass holds these sensor states."""
    flow = _flow_with_hass()
    flow.hass.states.async_all.return_value = list(states)
    return flow


def test_a_detected_list_appends_its_reserved_rows_last():
    """Shared by the price feed and the inverter sources; entity ids hold a dot, so neither value collides."""
    rows = [{"value": "sensor.a", "label": "A"}]
    assert detected_options(rows) == rows
    assert [row["value"] for row in detected_options(rows, other="Other", none="None")] == [
        "sensor.a", OTHER, NONE,
    ]
    assert [row["value"] for row in detected_options(rows, none="None")] == ["sensor.a", NONE]
    assert detected_options(rows, other="Other")[-1]["label"] == "Other"


def test_price_sources_are_recognised_by_what_their_translator_reads():
    rows = [{"start": "x", "value_inc_vat": 0.2}]
    assert price_source_of(_NORDPOOL_STATE.attributes) == "nordpool"
    assert price_source_of({"today": [0.1] * 24, "tomorrow_valid": False}) == "nordpool"
    assert price_source_of({"prices_today": []}) == "entsoe"
    assert price_source_of({"prices": [{"time": "x", "price": 0.1}]}) == "entsoe"
    assert price_source_of({"rates": rows}) == "octopus"
    assert price_source_of({"all_rates": rows}) == "octopus"
    assert price_source_of(_AMBER_STATE.attributes) == "amber"
    # Look-alikes no translator could read.
    assert price_source_of({"today": [1, 2]}) is None
    assert price_source_of({"rates": []}) is None
    assert price_source_of({"forecasts": [{"pv_estimate": 1.0}]}) is None
    assert price_source_of({"friendly_name": "Grid power"}) is None


def test_detection_orders_by_source_and_skips_other_sensors():
    hass = MagicMock()
    hass.states.async_all.return_value = [
        _AMBER_FEED_IN_STATE, _AMBER_STATE, _price_state("sensor.power"), _NORDPOOL_STATE,
    ]
    assert detect_price_sensors(hass) == [
        DetectedPriceSensor("nordpool", "sensor.nordpool_kwh_lt", "Name of sensor.nordpool_kwh_lt", False),
        DetectedPriceSensor("amber", "sensor.amber_general_forecast", "Name of sensor.amber_general_forecast", False),
        DetectedPriceSensor("amber", "sensor.amber_feed_in_forecast", "Name of sensor.amber_feed_in_forecast", True),
    ]
    hass.states.async_all.assert_called_once_with(("sensor", "event"))
    assert detect_price_sensors(None) == []


def test_octopus_day_rate_events_are_offered_but_not_gas_or_other_days():
    base = "event.octopus_energy_electricity_1_2"
    hass = MagicMock()
    hass.states.async_all.return_value = [
        _price_state(f"{base}_current_day_rates", rates=_OCTOPUS_RATES),
        _price_state(f"{base}_next_day_rates", rates=_OCTOPUS_RATES),
        _price_state(f"{base}_previous_day_rates", rates=_OCTOPUS_RATES),
        _price_state(f"{base}_export_current_day_rates", rates=_OCTOPUS_RATES),
        _price_state("event.octopus_energy_gas_3_4_current_day_rates", rates=_OCTOPUS_RATES),
    ]
    assert [(sensor.entity_id, sensor.export) for sensor in detect_price_sensors(hass)] == [
        (f"{base}_current_day_rates", False),
        (f"{base}_export_current_day_rates", True),
    ]


def test_export_feeds_are_told_apart_from_price_sensors():
    assert is_export_feed("octopus", "sensor.octopus_x_current_rate", {"is_export": True})
    assert is_export_feed("amber", "sensor.site_forecast_2", {"channel_type": "feedIn"})
    # Versions without the attribute: the entity id says it.
    assert is_export_feed("octopus", "sensor.octopus_electricity_2_export_current_rate", {})
    assert is_export_feed("amber", "sensor.amber_feed_in_forecast", {})
    assert not is_export_feed("octopus", "sensor.octopus_x_current_rate", {"is_export": False})
    assert not is_export_feed("amber", "sensor.amber_general_forecast", {"channel_type": "general"})
    # Nord Pool and ENTSO-e have no separate export feed, whatever the name says.
    assert not is_export_feed("nordpool", "sensor.nordpool_export", {})


def test_the_picker_preselects_the_stored_sensor_else_the_first_found():
    detected = [
        DetectedPriceSensor("nordpool", "sensor.a", "A", False),
        DetectedPriceSensor("entsoe", "sensor.b", "B", False),
    ]
    assert price_choice_default({}, detected) == "sensor.a"
    assert price_choice_default({CONF_NORDPOOL_ENTITY: "sensor.b"}, detected) == "sensor.b"
    # A stored sensor that isn't detected was picked by hand — keep that path.
    assert price_choice_default({CONF_NORDPOOL_ENTITY: "sensor.gone"}, detected) == PRICE_SENSOR_OTHER
    assert price_choice_default({CONF_NORDPOOL_ENTITY: "sensor.b"}, detected, "sensor.a") == "sensor.a"
    # An export feed is never the price sensor.
    export_first = [DetectedPriceSensor("octopus", "sensor.oe", "OE", True), detected[1]]
    assert price_choice_default({}, export_first) == "sensor.b"
    assert detected_label(detected[0]) == "Nord Pool: A (sensor.a)"
    assert detected_label(detected[1]) == "ENTSO-e: B (sensor.b) — untested"


def test_the_export_feed_preselects_the_stored_one_else_the_price_sensors_own():
    detected = [
        DetectedPriceSensor("octopus", "sensor.oct", "O", False),
        DetectedPriceSensor("octopus", "sensor.oct_export", "OE", True),
        DetectedPriceSensor("amber", "sensor.amb", "A", False),
        DetectedPriceSensor("amber", "sensor.amb_feed_in", "AF", True),
    ]
    assert export_choice_default({}, detected, "sensor.amb") == "sensor.amb_feed_in"
    assert export_choice_default({}, detected, "sensor.oct") == "sensor.oct_export"
    assert export_choice_default({}, detected, PRICE_SENSOR_OTHER) == PRICE_EXPORT_NONE
    # Once stored — even as none — the stored value wins.
    assert export_choice_default({CONF_PRICE_EXPORT_ENTITY: ""}, detected, "sensor.amb") == PRICE_EXPORT_NONE
    assert export_choice_default({CONF_PRICE_EXPORT_ENTITY: "sensor.x"}, detected, "sensor.amb") == "sensor.x"
    assert export_choice_default({}, detected, "sensor.amb", "sensor.oct_export") == "sensor.oct_export"


async def test_the_price_feed_form_says_what_it_found():
    found = await _flow_detecting(_NORDPOOL_STATE, _AMBER_STATE).async_step_price_feed(None)
    assert found["description_placeholders"]["detected"].startswith("Found 2 price sensors on this system")
    none = await _flow_detecting().async_step_price_feed(None)
    assert none["description_placeholders"]["detected"].startswith("No price sensor sunSale can read")


async def test_picking_a_detected_sensor_stores_it_with_its_source():
    flow = _flow_detecting(_NORDPOOL_STATE, _AMBER_STATE)
    result = await flow.async_step_price_feed(
        {PRICE_SENSOR_CHOICE: "sensor.amber_general_forecast", CONF_CURRENCY: "aud"},
    )
    assert result["step_id"] == "prices"
    assert flow._data[CONF_PRICE_SOURCE] == "amber"
    assert flow._data[CONF_NORDPOOL_ENTITY] == "sensor.amber_general_forecast"
    assert flow._data[CONF_CURRENCY] == "AUD"
    assert PRICE_SENSOR_CHOICE not in flow._data
    assert "price_feed" in flow._confirmed


async def test_other_opens_the_by_hand_form_which_saves_both_forms_values():
    flow = _flow_detecting(_NORDPOOL_STATE)
    await flow.async_step_price_buy_formula(FORMULA_INPUT)
    result = await flow.async_step_price_feed({PRICE_SENSOR_CHOICE: PRICE_SENSOR_OTHER, CONF_CURRENCY: "sek"})
    assert result["step_id"] == "price_feed_manual"
    assert CONF_CURRENCY not in flow._data and "price_feed" not in flow._confirmed
    result = await flow.async_step_price_feed_manual({CONF_PRICE_SOURCE: "entsoe"})
    assert result["errors"] == {CONF_NORDPOOL_ENTITY: "price_entity_required"}
    assert result["description_placeholders"] == {"source": "ENTSO-e"}
    result = await flow.async_step_price_feed_manual({CONF_PRICE_SOURCE: "entsoe", CONF_NORDPOOL_ENTITY: "sensor.e"})
    assert result["step_id"] == "prices"
    assert (flow._data[CONF_PRICE_SOURCE], flow._data[CONF_NORDPOOL_ENTITY]) == ("entsoe", "sensor.e")
    assert flow._data[CONF_CURRENCY] == "SEK"
    assert "price_feed" in flow._confirmed


async def test_discarding_the_by_hand_form_drops_the_price_feed_forms_values():
    flow = _flow_detecting(_NORDPOOL_STATE)
    await flow.async_step_price_feed({PRICE_SENSOR_CHOICE: PRICE_SENSOR_OTHER, CONF_CURRENCY: "sek"})
    result = await flow.async_step_price_feed_manual({GO_TO: "prices"})
    assert result["step_id"] == "prices"
    assert CONF_CURRENCY not in flow._data and "price_feed" not in flow._confirmed
    # Without the price-feed form's values (a stale tab) it starts over there.
    assert (await flow.async_step_price_feed_manual({CONF_NORDPOOL_ENTITY: "sensor.e"}))["step_id"] == "price_feed"


async def test_other_with_a_bad_currency_stays_on_the_price_feed_form():
    flow = _flow_detecting(_NORDPOOL_STATE)
    result = await flow.async_step_price_feed({PRICE_SENSOR_CHOICE: PRICE_SENSOR_OTHER, CONF_CURRENCY: "euro"})
    assert (result["step_id"], result["errors"]) == ("price_feed", {CONF_CURRENCY: "invalid_currency"})


async def test_the_detected_sensor_path_is_wired():
    flow = _flow_detecting(_NORDPOOL_STATE)
    walk = [
        await flow.async_step_price_feed(None),
        await flow.async_step_price_feed({PRICE_SENSOR_CHOICE: PRICE_SENSOR_OTHER}),
        await flow.async_step_price_feed_manual({CONF_NORDPOOL_ENTITY: "sensor.x"}),
    ]
    assert [result["step_id"] for result in walk] == ["price_feed", "price_feed_manual", "prices"]
    for result in walk:
        _assert_step_is_wired(flow, result, "config")


async def test_picking_a_detected_export_feed_stores_it():
    flow = _flow_detecting(_AMBER_STATE, _AMBER_FEED_IN_STATE)
    form = await flow.async_step_price_feed(None)
    assert "1 price sensor and 1 export-price feed" in form["description_placeholders"]["detected"]
    await flow.async_step_price_feed(
        {PRICE_SENSOR_CHOICE: _AMBER_STATE.entity_id, PRICE_EXPORT_CHOICE: _AMBER_FEED_IN_STATE.entity_id},
    )
    assert flow._data[CONF_PRICE_EXPORT_ENTITY] == "sensor.amber_feed_in_forecast"
    assert PRICE_EXPORT_CHOICE not in flow._data
    assert "export sensor.amber_feed_in_forecast" in _status(await flow.async_step_prices())
    await flow.async_step_price_feed(
        {PRICE_SENSOR_CHOICE: _AMBER_STATE.entity_id, PRICE_EXPORT_CHOICE: PRICE_EXPORT_NONE},
    )
    assert flow._data[CONF_PRICE_EXPORT_ENTITY] == ""


async def test_an_export_feed_from_another_integration_is_refused():
    flow = _flow_detecting(_OCTOPUS_STATE, _OCTOPUS_EXPORT_STATE, _AMBER_STATE, _AMBER_FEED_IN_STATE)
    result = await flow.async_step_price_feed(
        {PRICE_SENSOR_CHOICE: _AMBER_STATE.entity_id, PRICE_EXPORT_CHOICE: _OCTOPUS_EXPORT_STATE.entity_id},
    )
    assert (result["step_id"], result["errors"]) == ("price_feed", {PRICE_EXPORT_CHOICE: "export_feed_mismatch"})
    assert "price_feed" not in flow._confirmed
    for flow_kind in ("config", "options"):
        assert "export_feed_mismatch" in _STRINGS[flow_kind]["error"]


async def test_only_export_feeds_found_keeps_the_by_hand_form():
    result = await _flow_detecting(_AMBER_FEED_IN_STATE).async_step_price_feed(None)
    assert result["description_placeholders"]["detected"].startswith("No price sensor sunSale can read")


async def test_the_by_hand_form_starts_from_the_picked_export_feed_and_can_clear_it():
    flow = _flow_detecting(_AMBER_STATE, _AMBER_FEED_IN_STATE)
    await flow.async_step_price_feed(
        {PRICE_SENSOR_CHOICE: PRICE_SENSOR_OTHER, PRICE_EXPORT_CHOICE: _AMBER_FEED_IN_STATE.entity_id},
    )
    assert flow._feed_pending[CONF_PRICE_EXPORT_ENTITY] == _AMBER_FEED_IN_STATE.entity_id
    await flow.async_step_price_feed_manual({CONF_PRICE_SOURCE: "amber", CONF_NORDPOOL_ENTITY: "sensor.by_hand"})
    assert flow._data[CONF_NORDPOOL_ENTITY] == "sensor.by_hand"
    assert flow._data[CONF_PRICE_EXPORT_ENTITY] == ""


async def test_clearing_the_export_sensor_stores_none():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.np", CONF_PRICE_EXPORT_ENTITY: "sensor.exp"})
    assert flow._data[CONF_PRICE_EXPORT_ENTITY] == "sensor.exp"
    await flow.async_step_price_feed({CONF_NORDPOOL_ENTITY: "sensor.np"})
    assert flow._data[CONF_PRICE_EXPORT_ENTITY] == ""


async def test_a_price_menu_offers_formula_and_fees_then_back():
    result = await SunSaleConfigFlow().async_step_price_buy()
    assert result["step_id"] == "price_buy"
    assert result["menu_options"] == ["price_buy_formula", "price_buy_fees", "prices"]
    assert "· **Price formula** — still to confirm" in _status(result)


async def test_the_formula_is_stored_per_direction():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_buy_formula(FORMULA_INPUT)
    assert result["step_id"] == "price_buy"
    assert (flow._data[CONF_PRICE_BUY]["markup"], flow._data[CONF_PRICE_BUY]["tariffs"]) == (0.005, 1)
    assert "✓ **Price formula** — market price, markup 0.005, VAT 21 %, 1 grid-fee tariff" in _status(result)
    await flow.async_step_price_sell_formula(FIXED_INPUT)
    assert flow._data[CONF_PRICE_SELL]["energy"] == "fixed"
    assert flow._data[CONF_PRICE_BUY]["energy"] == "dynamic"


async def test_the_formula_form_reports_bad_values_on_their_field():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_buy_formula({**FORMULA_INPUT, "vat": 150.0})
    assert result["errors"] == {"vat": "invalid_vat"}
    seasons = {**FORMULA_INPUT, "seasons": True}
    result = await flow.async_step_price_buy_formula({**seasons, "summer_start": "13-01"})
    assert result["errors"] == {"summer_start": "invalid_season_date"}
    result = await flow.async_step_price_buy_formula({**seasons, "winter_start": "04-01"})
    assert result["errors"] == {"winter_start": "seasons_same_start"}
    # Season dates are not checked while seasons are off; a bad one keeps the stored date.
    result = await flow.async_step_price_buy_formula({**FORMULA_INPUT, "summer_start": "junk"})
    assert result["step_id"] == "price_buy"
    assert flow._data[CONF_PRICE_BUY]["summer_start"] == "04-01"


async def test_the_tariff_structure_decides_which_schedules_are_offered():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2", "weekends": True, "holidays": True})
    assert (await flow.async_step_price_buy())["menu_options"] == [
        "price_buy_formula", "price_buy_fees", "price_buy_all_workday", "price_buy_all_weekend",
        "price_buy_all_holiday", "prices",
    ]
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2", "seasons": True})
    assert (await flow.async_step_price_buy())["menu_options"][2:-1] == [
        "price_buy_summer_workday", "price_buy_winter_workday",
    ]
    root = await flow.async_step_hub()
    assert "Buy price: summer workday schedule, Buy price: winter workday schedule" in _status(root)


async def test_the_fees_form_has_one_fee_per_tariff():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "4"})
    form = await flow.async_step_price_buy_fees(None)
    assert form["description_placeholders"] == {"count": "4"}
    result = await flow.async_step_price_buy_fees({"fee_1": 0.1, "fee_2": 0.08, "fee_3": 0.05, "fee_4": 0.02})
    assert flow._data[CONF_PRICE_BUY]["grid_fees"] == [0.1, 0.08, 0.05, 0.02]
    assert "✓ **Grid fees** — T1 0.1, T2 0.08, T3 0.05, T4 0.02" in _status(result)


async def test_a_schedule_form_stores_its_rows_sorted_and_reopens_on_them():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2"})
    await flow.async_step_price_buy_fees({"fee_1": 0.06, "fee_2": 0.02})
    form = await flow.async_step_price_buy_all_workday()
    assert form["step_id"] == "price_buy_schedule"
    assert form["description_placeholders"] == {"schedule": "Workday schedule", "fees": "T1 0.06, T2 0.02"}
    result = await flow.async_step_price_buy_schedule(
        {"from_1": "23:00", "tariff_1": "2", "from_2": "7:00", "tariff_2": "1"},
    )
    assert result["step_id"] == "price_buy"
    assert flow._data[CONF_PRICE_BUY]["schedules"] == {
        "all_workday": [{"start": "07:00", "tariff": 1}, {"start": "23:00", "tariff": 2}],
    }
    assert "✓ **Workday schedule** — 07:00 T1, 23:00 T2" in _status(result)
    # Reopening pre-fills the form from the stored rows.
    assert schedule_form_values(flow._data[CONF_PRICE_BUY]["schedules"]["all_workday"]) == {
        "from_1": "07:00", "tariff_1": "1", "from_2": "23:00", "tariff_2": "2",
    }


async def test_a_schedule_form_reports_problems_per_row():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2"})
    await flow.async_step_price_buy_all_workday()
    cases = [
        ({}, {"base": "schedule_empty"}),
        ({"from_1": "25:00", "tariff_1": "1"}, {"from_1": "invalid_band_time"}),
        ({"from_1": "07:00"}, {"tariff_1": "required"}),
        ({"tariff_2": "1"}, {"from_2": "required"}),
        ({"from_1": "07:00", "tariff_1": "1", "from_2": "7:00", "tariff_2": "2"}, {"from_2": "duplicate_band_start"}),
        ({"from_1": "07:00", "tariff_1": "3"}, {"tariff_1": "required"}),
    ]
    for payload, errors in cases:
        result = await flow.async_step_price_buy_schedule(payload)
        assert result["step_id"] == "price_buy_schedule", payload
        assert result["errors"] == errors, payload
    assert "price_buy_all_workday" not in flow._confirmed


async def test_fewer_tariffs_drop_the_fees_and_rows_that_no_longer_exist():
    flow = SunSaleConfigFlow()
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "4"})
    await flow.async_step_price_buy_fees({"fee_1": 0.1, "fee_2": 0.08, "fee_3": 0.05, "fee_4": 0.02})
    await flow.async_step_price_buy_all_workday()
    await flow.async_step_price_buy_schedule({"from_1": "07:00", "tariff_1": "1", "from_2": "22:00", "tariff_2": "3"})
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2"})
    buy = flow._data[CONF_PRICE_BUY]
    assert buy["grid_fees"] == [0.1, 0.08]
    assert buy["schedules"] == {"all_workday": [{"start": "07:00", "tariff": 1}]}
    await flow.async_step_price_buy_formula(FORMULA_INPUT)
    assert flow._data[CONF_PRICE_BUY]["schedules"] == {}
    assert (await flow.async_step_price_buy())["menu_options"] == ["price_buy_formula", "price_buy_fees", "prices"]


async def test_a_price_is_complete_only_with_every_schedule_it_needs():
    flow = SunSaleConfigFlow()
    await _set_up_prices(flow)
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2", "weekends": True})
    status = _status(await flow.async_step_prices())
    assert "· **Buy price** — still to do: Workday schedule, Weekend schedule" in status
    for key in ("all_workday", "all_weekend"):
        await getattr(flow, f"async_step_price_buy_{key}")()
        await flow.async_step_price_buy_schedule({"from_1": "00:00", "tariff_1": "2"})
    status = _status(await flow.async_step_prices())
    assert "✓ **Buy price** — market price, markup 0.005, VAT 21 %, 2 grid-fee tariffs (weekends)" in status


async def test_price_levels_store_shares_and_limits():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_levels({
        CONF_PRICE_LEVEL_CHEAP_SHARE: 30.0, CONF_PRICE_LEVEL_EXPENSIVE_SHARE: 20.0,
        CONF_PRICE_LEVEL_CHEAP_BELOW: 0.05,
    })
    assert result["step_id"] == "prices"
    assert flow._data[CONF_PRICE_LEVEL_CHEAP_SHARE] == 30.0
    assert flow._data[CONF_PRICE_LEVEL_CHEAP_BELOW] == 0.05
    assert flow._data[CONF_PRICE_LEVEL_EXPENSIVE_ABOVE] is None
    assert "✓ **Price level** — cheapest 30 % cheap, dearest 20 % expensive" in _status(result)


async def test_price_levels_clearing_a_limit_stores_none():
    """A cleared optional field is submitted as absent — it must not keep the old limit."""
    flow = SunSaleConfigFlow()
    flow._data[CONF_PRICE_LEVEL_CHEAP_BELOW] = 0.05
    await flow.async_step_price_levels({CONF_PRICE_LEVEL_CHEAP_SHARE: 25.0, CONF_PRICE_LEVEL_EXPENSIVE_SHARE: 25.0})
    assert flow._data[CONF_PRICE_LEVEL_CHEAP_BELOW] is None


async def test_price_levels_report_each_problem_on_its_field():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_price_levels({
        CONF_PRICE_LEVEL_CHEAP_SHARE: 120.0, CONF_PRICE_LEVEL_EXPENSIVE_SHARE: 25.0,
        CONF_PRICE_LEVEL_CHEAP_BELOW: 0.30, CONF_PRICE_LEVEL_EXPENSIVE_ABOVE: 0.20,
    })
    assert result["errors"] == {
        CONF_PRICE_LEVEL_CHEAP_SHARE: "invalid_level_share",
        CONF_PRICE_LEVEL_EXPENSIVE_ABOVE: "level_limits_inverted",
    }
    result = await flow.async_step_price_levels(
        {CONF_PRICE_LEVEL_CHEAP_SHARE: 60.0, CONF_PRICE_LEVEL_EXPENSIVE_SHARE: 50.0},
    )
    assert result["errors"] == {CONF_PRICE_LEVEL_EXPENSIVE_SHARE: "level_shares_overlap"}


# ---------------------------------------------------------------------------
# Inverter section — platform, battery, entity mapping, counters
# ---------------------------------------------------------------------------

async def test_inverter_menu_before_a_platform_is_chosen():
    result = await _flow_with_hass().async_step_inverter()
    assert result["step_id"] == "inverter"
    assert result["menu_options"] == [
        "inverter_platform", "battery", "bms", "sources", "sources_energy", "hub",
    ]


async def test_platform_autodetects_a_single_solis_entry_and_hides_the_mapping_row():
    flow = _flow_with_hass(_single_solis_entry())
    result = await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    assert result["step_id"] == "inverter"
    assert result["menu_options"] == [
        "inverter_platform", "battery", "bms", "sources", "sources_energy", "inverter_remove", "hub",
    ]
    assert flow._data[CONF_SOLIS_CONFIG_ENTRY_ID] == "solis_entry_abc"
    result = await flow.async_step_battery(VALID_BATTERY_INPUT)
    assert "✓ **Entity mapping** — auto-detected" in _status(result)
    assert "✓ **Inverter and battery** — Solis" in _status(await flow.async_step_hub())


async def test_platform_without_solis_entries_offers_manual_mapping():
    flow = _flow_with_hass()
    result = await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    assert "inverter_mapping" in result["menu_options"]
    form = await flow.async_step_inverter_mapping()
    assert form["step_id"] == "inverter_solis"
    result = await flow.async_step_inverter_solis({"inverter_entity_battery_soc": "sensor.soc"})
    assert result["step_id"] == "inverter"
    assert "inverter_mapping" in flow._confirmed


async def test_solis_picker_for_multiple_entries():
    entries = [MagicMock(entry_id="e1", title="Solis 1"), MagicMock(entry_id="e2", title="Solis 2")]
    flow = _flow_with_hass(entries)
    await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    form = await flow.async_step_inverter_mapping()
    assert form["step_id"] == "inverter_solis"
    result = await flow.async_step_inverter_solis({CONF_SOLIS_CONFIG_ENTRY_ID: "e2"})
    assert result["step_id"] == "inverter"
    assert flow._data[CONF_SOLIS_CONFIG_ENTRY_ID] == "e2"


async def test_changing_the_platform_asks_for_the_mapping_again():
    flow = _flow_with_hass()
    await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    await flow.async_step_inverter_solis({"inverter_entity_battery_soc": "sensor.soc"})
    await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SUNGROW.value})
    assert "inverter_mapping" not in flow._confirmed


async def test_step_inverter_unimplemented_platform_is_rejected():
    # Unwired platforms are listed for reference but not selectable: choosing
    # one re-shows the page with an error and never stores it.
    flow = _flow_with_hass()
    result = await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.GENERIC.value})
    assert result["step_id"] == "inverter_platform"
    assert result["errors"] == {CONF_INVERTER_PLATFORM: "platform_not_implemented"}
    assert result["description_placeholders"] == {"platform": "Generic"}
    assert CONF_INVERTER_PLATFORM not in flow._data


async def test_step_inverter_stores_power_ratings():
    flow = _flow_with_hass()
    await flow.async_step_inverter_platform({
        CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
        CONF_INVERTER_MAX_POWER_KW: 5.0,
        CONF_INVERTER_EXPORT_LIMIT_KW: 4.0,
    })
    assert flow._data[CONF_INVERTER_PLATFORM] == InverterPlatform.SOLIS.value
    assert flow._data[CONF_INVERTER_MAX_POWER_KW] == 5.0
    assert flow._data[CONF_INVERTER_EXPORT_LIMIT_KW] == 4.0


async def test_step_inverter_rejects_bad_power_ratings():
    flow = _flow_with_hass()
    base = {CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value, CONF_INVERTER_EXPORT_LIMIT_KW: 4.0}
    result = await flow.async_step_inverter_platform({**base, CONF_INVERTER_MAX_POWER_KW: 0.0})
    assert result["errors"] == {CONF_INVERTER_MAX_POWER_KW: "invalid_inverter_power"}
    result = await flow.async_step_inverter_platform(
        {**base, CONF_INVERTER_MAX_POWER_KW: 5.0, CONF_INVERTER_EXPORT_LIMIT_KW: -1.0},
    )
    assert result["errors"] == {CONF_INVERTER_EXPORT_LIMIT_KW: "invalid_export_limit"}
    assert CONF_INVERTER_PLATFORM not in flow._data


async def test_step_battery_returns_to_the_inverter_menu():
    flow = _flow_with_hass()
    result = await flow.async_step_battery(VALID_BATTERY_INPUT)
    assert result["step_id"] == "inverter"
    assert flow._data[CONF_BATTERY_NOMINAL_CAPACITY] == 10.0


async def test_step_battery_reports_each_bad_field_separately():
    bad = {
        **VALID_BATTERY_INPUT,
        CONF_BATTERY_PURCHASE_PRICE: 0.0,
        CONF_BATTERY_RATED_CYCLE_LIFE: 0,
        CONF_BATTERY_MAX_CHARGE_POWER: 0.0,
        CONF_BATTERY_MIN_SOC: 100,
        CONF_BATTERY_NOMINAL_VOLTAGE: -1.0,
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 110.0,
    }
    result = await SunSaleConfigFlow().async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert result["errors"] == {
        CONF_BATTERY_PURCHASE_PRICE: "invalid_price",
        CONF_BATTERY_RATED_CYCLE_LIFE: "invalid_cycle_life",
        CONF_BATTERY_MAX_CHARGE_POWER: "invalid_charge_power",
        CONF_BATTERY_MIN_SOC: "invalid_min_soc",
        CONF_BATTERY_NOMINAL_VOLTAGE: "invalid_voltage",
        CONF_BATTERY_ROUND_TRIP_EFFICIENCY: "invalid_efficiency",
    }


async def test_step_battery_rejects_an_inverted_soc_range_and_zero_capacity():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_battery({**VALID_BATTERY_INPUT, CONF_BATTERY_MIN_SOC: 90, CONF_BATTERY_MAX_SOC: 20})
    assert result["errors"] == {CONF_BATTERY_MAX_SOC: "soc_range_inverted"}
    result = await flow.async_step_battery({**VALID_BATTERY_INPUT, CONF_BATTERY_NOMINAL_CAPACITY: 0.0})
    assert result["errors"] == {CONF_BATTERY_NOMINAL_CAPACITY: "invalid_capacity"}


async def test_both_sources_pages_are_optional_and_return_to_the_inverter_menu():
    flow = _flow_with_hass()
    result = await flow.async_step_inverter()
    assert "○ **Inverter power sensors** — optional: none set" in _status(result)
    assert "○ **Energy counters** — optional: none set" in _status(result)
    result = await flow.async_step_sources({"inverter_entity_pv_power": "sensor.pv"})
    assert result["step_id"] == "inverter"
    assert "✓ **Inverter power sensors** — 1 of 5 set" in _status(result)
    result = await flow.async_step_sources_energy({"inverter_entity_generation_yesterday": "sensor.pv_yesterday"})
    assert "✓ **Energy counters** — 1 of 9 set" in _status(result)
    assert flow._data["inverter_entity_generation_yesterday"] == "sensor.pv_yesterday"


def _with_sources(flow, detected: dict[str, list[tuple[str, bool]]], defaults: dict[str, str] | None = None):
    """Give the flow detected inverter sources: ``{conf_key: [(entity_id, auto), …]}``."""
    options = {
        key: SourceOptions(
            tuple(DetectedSource(entity_id, "", auto) for entity_id, auto in rows),
            (defaults or {}).get(key, next((e for e, auto in rows if auto), "")),
        )
        for key, rows in detected.items()
    }
    flow._inverter_sources = lambda: options
    return flow


async def test_a_sources_page_says_what_was_found_on_the_inverter():
    """The candidate lists themselves are pinned in test_inverter_sources.py."""
    flow = _with_sources(_flow_with_hass(), {
        "inverter_entity_ac_port_power": [("sensor.ac_grid_port_power", True), ("sensor.other_power", False)],
    })
    form = await flow.async_step_sources(None)
    assert form["step_id"] == "sources"
    assert form["description_placeholders"]["detected"] == (
        "Found candidates for 1 of 5 sources on your inverter, 1 already filled in — "
        "check them and correct any that look wrong."
    )


def test_a_hand_picked_sensor_keeps_its_row_and_the_pre_selection():
    """It is not on the inverter's device — that is why it was picked by hand."""
    detected = DetectedSource("sensor.ac_grid_port_power", "AC port", True)
    options = SourceOptions((detected,), detected.entity_id)
    rows, default = source_rows(
        "inverter_entity_ac_port_power", {"inverter_entity_ac_port_power": "sensor.mine"}, options,
    )
    assert [row["value"] for row in rows] == ["sensor.ac_grid_port_power", "sensor.mine"]
    assert rows[0]["label"] == "AC port (sensor.ac_grid_port_power) — detected"
    assert rows[1]["label"] == "sensor.mine (picked by hand)"
    assert default == "sensor.mine"


def test_a_detected_source_is_pre_selected_when_nothing_is_stored():
    options = SourceOptions((DetectedSource("sensor.ac_grid_port_power", "", True),), "sensor.ac_grid_port_power")
    rows, default = source_rows("inverter_entity_ac_port_power", {}, options)
    assert [row["value"] for row in rows] == ["sensor.ac_grid_port_power"]
    assert default == "sensor.ac_grid_port_power"


async def test_a_page_with_nothing_detected_says_so_and_falls_back():
    form = await _flow_with_hass().async_step_sources(None)
    assert "No sensors could be read off the inverter" in form["description_placeholders"]["detected"]


async def test_choosing_other_opens_the_by_hand_form_and_keeps_the_rest_of_the_page():
    flow = _with_sources(_flow_with_hass(), {
        "inverter_entity_pv_power": [("sensor.total_pv_power", True)],
        "inverter_entity_ac_port_power": [("sensor.ac_grid_port_power", True)],
    })
    form = await flow.async_step_sources({
        "inverter_entity_pv_power": "sensor.total_pv_power",
        "inverter_entity_ac_port_power": OTHER,
    })
    assert form["step_id"] == "sources_manual"
    # Only the row that asked for it is picked by hand, and nothing is stored yet.
    assert flow._sources_pending["by_hand"] == ["inverter_entity_ac_port_power"]
    assert flow._data.get("inverter_entity_pv_power") is None, "the page saved before the by-hand picks"

    result = await flow.async_step_sources_manual({"inverter_entity_ac_port_power": "sensor.anything_else"})
    assert result["step_id"] == "inverter"
    assert flow._data["inverter_entity_pv_power"] == "sensor.total_pv_power"
    assert flow._data["inverter_entity_ac_port_power"] == "sensor.anything_else"


async def test_the_by_hand_form_without_a_page_behind_it_reopens_the_page():
    """A stale browser tab must not save a half-built page."""
    result = await _flow_with_hass().async_step_sources_manual({})
    assert result["step_id"] == "sources"


async def test_a_source_can_be_taken_away_again():
    """The None row clears a source — a defaulted optional field never could."""
    flow = _with_sources(_flow_with_hass(), {
        "inverter_entity_pv_power": [("sensor.total_pv_power", True)],
    })
    await flow.async_step_sources({"inverter_entity_pv_power": "sensor.total_pv_power"})
    assert flow._data["inverter_entity_pv_power"] == "sensor.total_pv_power"
    await flow.async_step_sources({"inverter_entity_pv_power": NONE})
    assert flow._data["inverter_entity_pv_power"] == ""
    # So does clearing a plain picker, whose cleared field arrives absent.
    await flow.async_step_sources_energy({"inverter_entity_solar_energy": "sensor.pv_today"})
    await flow.async_step_sources_energy({})
    assert flow._data["inverter_entity_solar_energy"] == ""


async def test_a_sources_page_never_writes_the_other_page_s_keys():
    flow = _flow_with_hass()
    await flow.async_step_sources_energy({"inverter_entity_solar_energy": "sensor.pv_today"})
    await flow.async_step_sources({"bms_battery_soc": "sensor.jk"})
    assert flow._data["inverter_entity_solar_energy"] == "sensor.pv_today"


def _with_bms(flow, detected: dict[str, list[str]], *, device: str = "", name: str = "JK-BMS"):
    """Give the flow a battery device whose sensors resolve to ``detected``."""
    options = {
        key: SourceOptions(tuple(DetectedSource(e, "", False) for e in ids), ids[0])
        for key, ids in detected.items()
    }
    flow._bms_sources = lambda device_id: options
    flow._bms_device = lambda: device
    flow._ha_device_name = lambda device_id: name
    return flow


async def test_a_bms_is_picked_as_a_device_and_its_sensors_are_resolved():
    flow = _with_bms(_flow_with_hass(), {
        "bms_battery_soc": ["sensor.jk_soc"],
        "bms_battery_power": ["sensor.jk_power"],
    })
    form = await flow.async_step_bms({"bms_device": "hadev_bms"})
    assert form["step_id"] == "bms_details"
    assert form["description_placeholders"]["device"] == "JK-BMS"
    assert flow._data.get("bms_battery_soc") is None, "the device page saved before its sensors were confirmed"

    result = await flow.async_step_bms_details({
        "bms_battery_soc": "sensor.jk_soc", "bms_battery_power": "sensor.jk_power",
    })
    assert result["step_id"] == "inverter"
    assert flow._data["bms_battery_soc"] == "sensor.jk_soc"
    assert flow._data["bms_battery_power"] == "sensor.jk_power"
    assert "✓ **Dedicated battery BMS** — sensor.jk_soc" in _status(result)


async def test_a_device_without_a_state_of_charge_is_refused():
    """Without SoC the chain never prefers the BMS, so accepting it would change nothing."""
    flow = _with_bms(_flow_with_hass(), {"bms_battery_power": ["sensor.jk_power"]})
    form = await flow.async_step_bms({"bms_device": "hadev_plug"})
    assert form["step_id"] == "bms"
    assert form["errors"] == {"bms_device": "bms_no_soc"}
    assert flow._data.get("bms_battery_soc") is None


async def test_confirming_the_bms_page_with_no_device_takes_the_bms_away():
    flow = _with_bms(_flow_with_hass(), {"bms_battery_soc": ["sensor.jk_soc"]})
    await flow.async_step_bms({"bms_device": "hadev_bms"})
    await flow.async_step_bms_details({"bms_battery_soc": "sensor.jk_soc"})
    result = await flow.async_step_bms({})
    assert result["step_id"] == "inverter"
    assert flow._data["bms_battery_soc"] == ""
    assert "✓ **Dedicated battery BMS** — battery read from the inverter" in _status(result)


async def test_the_bms_sensor_form_without_a_picked_device_reopens_the_picker():
    result = await _with_bms(_flow_with_hass(), {}).async_step_bms_details({})
    assert result["step_id"] == "bms"


async def test_a_bms_sensor_can_be_picked_by_hand():
    flow = _with_bms(_flow_with_hass(), {
        "bms_battery_soc": ["sensor.jk_soc"], "bms_battery_power": ["sensor.jk_power"],
    })
    await flow.async_step_bms({"bms_device": "hadev_bms"})
    form = await flow.async_step_bms_details({"bms_battery_soc": "sensor.jk_soc", "bms_battery_power": OTHER})
    assert form["step_id"] == "sources_manual"
    result = await flow.async_step_sources_manual({"bms_battery_power": "sensor.elsewhere"})
    # The by-hand form confirms the BMS page it came from, not a sources page.
    assert "✓ **Dedicated battery BMS** — sensor.jk_soc" in _status(result)
    assert flow._data["bms_battery_power"] == "sensor.elsewhere"


async def test_removing_the_inverter_hides_the_forecast_row():
    flow = _flow_with_hass(_single_solis_entry())
    await _set_up_solis(flow)
    assert "forecast" in (await flow.async_step_hub())["menu_options"]
    result = await flow.async_step_inverter_remove()
    assert result["menu_options"] == _ROOT_EMPTY


# ---------------------------------------------------------------------------
# Solar forecast — one page straight off the root, optional
# ---------------------------------------------------------------------------

async def test_full_solis_install_saves_without_a_forecast():
    flow = _flow_with_hass(_single_solis_entry())
    await _set_up_prices(flow)
    await _set_up_solis(flow)
    root = await flow.async_step_hub()
    assert root["menu_options"] == ["prices", "inverter", "forecast", "installation_name", "finish"]
    assert "○ **Solar forecast** — optional: none selected" in _status(root)
    result = await flow.async_step_finish()
    assert result["data"][CONF_SOLIS_CONFIG_ENTRY_ID] == "solis_entry_abc"
    assert result["data"][CONF_INSTALL_CAPABILITIES] == {
        CAP_PRICES: True, CAP_INVERTER: True, CAP_SOLAR_FORECAST: True,
    }


def _with_forecast(flow, detected: list[tuple[str, str]] = (), resolved: dict[str, str] | None = None):
    """Give the flow detected leftover forecast sensors and per-device resolutions."""
    flow._forecast_sensors = lambda covered: [
        DetectedForecast(eid, name) for eid, name in detected if eid not in covered
    ]
    flow._forecast_resolved = lambda device_ids: {d: (resolved or {}).get(d, "") for d in device_ids}
    return flow


async def test_step_forecast_shows_device_picker_when_discovered():
    fc = MagicMock(entry_id="f1", title="Garazas")
    fc.domain = "open_meteo_solar_forecast"
    flow = _with_forecast(_flow_with_hass(forecast_entries=[fc]))
    result = await flow.async_step_forecast(None)
    assert result["step_id"] == "forecast"
    assert "Found 1 forecast integration(s) on this system." in _detected(result)


async def test_step_forecast_without_devices_still_offers_the_sensor_list():
    result = await _with_forecast(_flow_with_hass()).async_step_forecast(None)
    assert result["step_id"] == "forecast"
    assert "No forecast integration or sensor was found" in _detected(result)


async def test_a_forecast_sensor_no_integration_covers_is_offered_as_a_list():
    """The device picker only knows three integrations; this is the long tail."""
    flow = _with_forecast(_flow_with_hass(), detected=[("sensor.shed_today", "Shed")])
    result = await flow.async_step_forecast(None)
    assert "1 forecast sensor(s) no integration above covers" in _detected(result)
    # Picked from the list, it is stored in the one sensor list the runtime reads.
    result = await flow.async_step_forecast({FORECAST_DETECTED: ["sensor.shed_today"]})
    assert flow._data[CONF_SOLAR_FORECAST_ENTITIES] == ["sensor.shed_today"]


async def test_a_sensor_a_device_already_resolves_is_not_offered_twice():
    """Otherwise the same array appears as a device and as a sensor."""
    fc = MagicMock(entry_id="f1", title="Garazas")
    fc.domain = "open_meteo_solar_forecast"
    flow = _with_forecast(
        _flow_with_hass(forecast_entries=[fc]),
        detected=[("sensor.energy_production_today", "Garazas today")],
        resolved={"f1": "sensor.energy_production_today"},
    )
    flow._data[CONF_SOLAR_FORECAST_DEVICE_IDS] = ["f1"]
    result = await flow.async_step_forecast(None)
    assert "forecast sensor(s) no integration above covers" not in _detected(result)
    assert "**Garazas** → `sensor.energy_production_today`" in _detected(result)


async def test_a_device_that_resolves_to_nothing_says_so_on_the_form():
    """It was only a log line before, so an array could contribute nothing in silence."""
    fc = MagicMock(entry_id="f1", title="Garazas")
    fc.domain = "open_meteo_solar_forecast"
    flow = _with_forecast(_flow_with_hass(forecast_entries=[fc]), resolved={"f1": ""})
    flow._data[CONF_SOLAR_FORECAST_DEVICE_IDS] = ["f1"]
    result = await flow.async_step_forecast(None)
    assert "⚠ **Garazas** exposes no forecast sensor sunSale recognises" in _detected(result)


async def test_the_detected_and_by_hand_sensor_fields_are_merged_and_deduped():
    flow = _with_forecast(_flow_with_hass(), detected=[("sensor.shed_today", "")])
    await flow.async_step_forecast({
        FORECAST_DETECTED: ["sensor.shed_today"],
        CONF_SOLAR_FORECAST_ENTITIES: ["sensor.shed_today", "sensor.odd_today"],
    })
    assert flow._data[CONF_SOLAR_FORECAST_ENTITIES] == ["sensor.shed_today", "sensor.odd_today"]


async def test_step_forecast_takes_more_than_two_sensors_and_drops_legacy_keys():
    flow = _flow_with_hass()
    flow._data[CONF_SOLAR_FORECAST_ENTITY] = "sensor.old_1"
    sensors = ["sensor.east_today", "sensor.west_today", "sensor.carport_today"]
    result = await flow.async_step_forecast({CONF_SOLAR_FORECAST_ENTITIES: sensors})
    assert result["step_id"] == "hub"
    assert flow._data[CONF_SOLAR_FORECAST_ENTITIES] == sensors
    assert CONF_SOLAR_FORECAST_ENTITY not in flow._data
    assert flow._section_summary("forecast") == "3 forecast sensor(s)"


async def test_step_forecast_submission_stores_devices_and_returns_to_the_root():
    fc = MagicMock(entry_id="f1", title="Garazas")
    fc.domain = "solcast_solar"
    flow = _flow_with_hass(_single_solis_entry(), forecast_entries=[fc])
    await _set_up_solis(flow)
    result = await flow.async_step_forecast({CONF_SOLAR_FORECAST_DEVICE_IDS: ["f1"]})
    assert result["step_id"] == "hub"
    assert flow._data[CONF_SOLAR_FORECAST_DEVICE_IDS] == ["f1"]
    assert "✓ **Solar forecast** — 1 forecast device(s)" in _status(result)


async def test_step_forecast_prefills_legacy_sensors_into_the_list():
    flow = _flow_with_hass()
    flow._data.update({CONF_SOLAR_FORECAST_ENTITY: "sensor.a", CONF_SOLAR_FORECAST_ENTITY_2: "sensor.b"})
    assert manual_forecast_entities(flow._data) == ["sensor.a", "sensor.b"]


def test_combine_forecast_keeps_legacy_behaviour_without_the_list():
    """Old entries: devices win outright, so a stale manual key can't double-count."""
    legacy = {CONF_SOLAR_FORECAST_ENTITY: "sensor.old"}
    assert combine_forecast_entities(["sensor.dev"], legacy) == ["sensor.dev"]
    assert combine_forecast_entities([], legacy) == ["sensor.old"]


def test_combine_forecast_adds_devices_and_sensors_once_the_list_is_saved():
    cfg = {CONF_SOLAR_FORECAST_ENTITY: "sensor.old", CONF_SOLAR_FORECAST_ENTITIES: ["sensor.b", "sensor.dev"]}
    # The list supersedes the legacy key; duplicates collapse, order is kept.
    assert combine_forecast_entities(["sensor.dev"], cfg) == ["sensor.dev", "sensor.b"]
    assert combine_forecast_entities([], {**cfg, CONF_SOLAR_FORECAST_ENTITIES: []}) == []


# ---------------------------------------------------------------------------
# Options flow — the same tree on an existing entry
# ---------------------------------------------------------------------------

_LEGACY_SOLIS_ENTRY = {
    **LEGACY_TARIFF,
    **VALID_BATTERY_INPUT,
    CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
    CONF_SOLIS_CONFIG_ENTRY_ID: "solis_entry_abc",
    CONF_NORDPOOL_ENTITY: "sensor.nordpool",
}


def _options_flow(data: dict, options: dict | None = None) -> SunSaleOptionsFlow:
    """Return an options flow over an entry with this data/options."""
    entry = MagicMock()
    entry.data = data
    entry.options = options or {}
    flow = SunSaleOptionsFlow(entry)
    flow.hass = _mock_hass(_single_solis_entry(), [])
    return flow


async def test_options_opens_on_a_complete_root_for_a_legacy_entry():
    """Pre-checklist entries infer prices + inverter + forecast and can save at once."""
    flow = _options_flow(_LEGACY_SOLIS_ENTRY)
    result = await flow.async_step_init()
    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert result["menu_options"] == [
        "prices", "inverter", "forecast", "installation_name", "finish",
    ]
    assert (await flow.async_step_finish())["title"] == ""


async def test_options_back_rows_return_to_init():
    flow = _options_flow(_LEGACY_SOLIS_ENTRY)
    assert (await flow.async_step_prices())["menu_options"][-2:] == ["prices_remove", "init"]
    assert (await flow.async_step_inverter())["menu_options"][-1] == "init"


async def test_options_editing_one_page_keeps_every_other_value():
    """Options replace wholesale on save — the seeded accumulator must carry the rest."""
    flow = _options_flow(_LEGACY_SOLIS_ENTRY, {CONF_TARIFF_MARKUP: 0.01})
    await flow.async_step_forecast({CONF_SOLAR_FORECAST_ENTITIES: ["sensor.pv_today"]})
    saved = (await flow.async_step_finish())["data"]
    assert saved[CONF_SOLAR_FORECAST_ENTITIES] == ["sensor.pv_today"]
    # The legacy tariff is saved as the buy / sell formulas.
    assert saved[CONF_PRICE_BUY]["markup"] == 0.01
    assert CONF_TARIFF_MARKUP not in saved
    assert saved[CONF_BATTERY_NOMINAL_CAPACITY] == 10.0
    assert saved[CONF_SOLIS_CONFIG_ENTRY_ID] == "solis_entry_abc"


async def test_options_forms_prefill_from_the_entry():
    flow = _options_flow(_LEGACY_SOLIS_ENTRY)
    result = await flow.async_step_price_buy_formula(None)
    assert result["step_id"] == "price_buy_formula"
    # The legacy tariff is converted into the seeded accumulator the schema reads.
    assert (flow._data[CONF_PRICE_BUY]["vat"], flow._data[CONF_PRICE_BUY]["grid_fees"]) == (21.0, [0.03])


async def test_options_convert_legacy_fee_bands_into_complete_schedules():
    bands = [{"start": "07:00", "buy_fee": 0.06, "sell_fee": 0.0}, {"start": "23:00", "buy_fee": 0.02, "sell_fee": 0.0}]
    flow = _options_flow({**_LEGACY_SOLIS_ENTRY, CONF_TARIFF_WEEKDAY_BANDS: bands})
    menu = await flow.async_step_price_buy()
    # Weekends kept the flat fee, so they get their own schedule.
    assert menu["menu_options"][2:-1] == ["price_buy_all_workday", "price_buy_all_weekend"]
    assert "✓ **Workday schedule** — 07:00 T1, 23:00 T2" in _status(menu)
    assert (await flow.async_step_finish())["title"] == ""


async def test_options_price_feed_offers_the_stored_source():
    flow = _options_flow({**_LEGACY_SOLIS_ENTRY, CONF_PRICE_SOURCE: "entsoe"})
    result = await flow.async_step_price_feed(None)
    assert result["description_placeholders"]["source"] == "ENTSO-e"


# ---------------------------------------------------------------------------
# "✕ discard and go back" — every form's way up one level
# ---------------------------------------------------------------------------

async def test_every_form_has_a_way_back_that_saves_nothing():
    """A form has one button, so the dropdown's discard option is the only way out short of cancelling setup."""
    flow = _flow_with_hass()
    flow._data[CONF_INVERTER_PLATFORM] = InverterPlatform.SUNGROW.value
    cases = [
        ("installation_name", {CONF_PANEL_TITLE: "Flat 3"}, "hub"),
        ("price_feed", {CONF_PRICE_SOURCE: "entsoe", CONF_NORDPOOL_ENTITY: "sensor.x"}, "prices"),
        ("price_feed_manual", {CONF_PRICE_SOURCE: "entsoe", CONF_NORDPOOL_ENTITY: "sensor.x"}, "prices"),
        ("price_buy_formula", FORMULA_INPUT, "price_buy"),
        ("price_sell_fees", {"fee_1": 0.1}, "price_sell"),
        ("price_buy_schedule", {"from_1": "07:00", "tariff_1": "1"}, "price_buy"),
        ("price_levels", {CONF_PRICE_LEVEL_CHEAP_SHARE: 40.0}, "prices"),
        ("price_forecast", {CONF_WEATHER_ENTITY: "weather.home"}, "prices"),
        ("inverter_platform", {CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value}, "inverter"),
        ("battery", VALID_BATTERY_INPUT, "inverter"),
        ("inverter_solis", {CONF_SOLIS_CONFIG_ENTRY_ID: "e1"}, "inverter"),
        ("inverter_generic", {}, "inverter"),
        ("inverter_entities", {}, "inverter"),
        ("sources", {"inverter_entity_pv_power": "sensor.pv"}, "inverter"),
        ("bms", {"bms_device": "hadev_bms"}, "inverter"),
        ("bms_details", {"bms_battery_soc": "sensor.jk_soc"}, "inverter"),
        ("sources_energy", {"inverter_entity_solar_energy": "sensor.pv"}, "inverter"),
        ("sources_manual", {"inverter_entity_ac_port_power": "sensor.ac"}, "inverter"),
        ("forecast", {CONF_SOLAR_FORECAST_ENTITIES: ["sensor.pv"]}, "hub"),
    ]
    for step, payload, parent in cases:
        data, confirmed = copy.deepcopy(flow._data), set(flow._confirmed)
        result = await getattr(flow, f"async_step_{step}")({**payload, GO_TO: parent})
        assert (result["type"], result["step_id"]) == ("menu", parent), step
        assert flow._data == data, f"{step} saved something on discard"
        assert flow._confirmed == confirmed, f"{step} confirmed a page on discard"


async def test_the_confirm_dropdown_is_never_stored():
    flow = _flow_with_hass()
    await flow.async_step_sources({GO_TO: SAVE, "inverter_entity_pv_power": "sensor.pv"})
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, GO_TO: SAVE})
    assert GO_TO not in flow._data
    assert GO_TO not in flow._data[CONF_PRICE_BUY]
    assert flow._data["inverter_entity_pv_power"] == "sensor.pv"


async def test_fields_without_a_prefill_are_checked_by_the_step():
    """They are optional in the schema so Back is never blocked — the step reports them instead."""
    flow = _flow_with_hass()
    assert (await flow.async_step_battery({}))["errors"] == {
        CONF_BATTERY_NOMINAL_CAPACITY: "required", CONF_BATTERY_PURCHASE_PRICE: "required",
    }
    errors = (await flow.async_step_inverter_entities({}))["errors"]
    assert len(errors) == 4 and set(errors.values()) == {"required"}
    await flow.async_step_price_buy_formula({**FORMULA_INPUT, "tariffs": "2"})
    await flow.async_step_price_buy_all_workday()
    assert (await flow.async_step_price_buy_schedule({"tariff_1": "1"}))["errors"] == {"from_1": "required"}
    flow._data[CONF_INVERTER_PLATFORM] = InverterPlatform.SUNGROW.value
    errors = (await flow.async_step_inverter_generic({}))["errors"]
    assert errors and set(errors.values()) == {"required"}
    assert "inverter_mapping" not in flow._confirmed


# ---------------------------------------------------------------------------
# Step ids, handlers and strings — the checks HA makes and the stubs skip
# ---------------------------------------------------------------------------

def _assert_step_is_wired(flow, result: dict, flow_kind: str) -> None:
    """Fail like HA's ``UnknownStep`` on a missing handler, and on missing strings."""
    steps = _STRINGS[flow_kind]["step"]
    step_id = result["step_id"]
    assert hasattr(flow, f"async_step_{step_id}"), f"{type(flow).__name__} has no async_step_{step_id}"
    assert step_id in steps, f"strings.json has no {flow_kind}.step.{step_id}"
    for option in result.get("menu_options", []):
        assert hasattr(flow, f"async_step_{option}"), f"{type(flow).__name__} has no async_step_{option}"
        assert option in steps[step_id]["menu_options"], f"{flow_kind}.step.{step_id} has no label for {option}"


async def test_every_step_shown_during_a_full_setup_is_wired():
    """Regression: the config-flow hub was shown as ``hub`` with no async_step_hub.

    HA raised UnknownStep on the first submit and the frontend then answered
    every click with "Invalid flow specified".
    """
    flow = _flow_with_hass()
    walk = [
        await flow.async_step_user(None),
        await flow.async_step_prices(),
        await flow.async_step_price_feed(None),
        await flow.async_step_price_feed({CONF_PRICE_SOURCE: "entsoe", CONF_NORDPOOL_ENTITY: "sensor.x"}),
        await flow.async_step_price_buy(),
        await flow.async_step_price_buy_formula(None),
        await flow.async_step_price_buy_formula(
            {**FORMULA_INPUT, "tariffs": "4", "weekends": True, "holidays": True, "seasons": True},
        ),
        await flow.async_step_price_buy_fees(None),
        await flow.async_step_price_buy_fees({"fee_1": 0.1, "fee_2": 0.2, "fee_3": 0.3, "fee_4": 0.4}),
    ]
    # Every schedule row is wired, whether this structure offers it or not.
    for key in ALL_SCHEDULE_KEYS:
        walk.append(await getattr(flow, f"async_step_price_buy_{key}")())
        walk.append(await flow.async_step_price_buy_schedule({"from_1": "00:00", "tariff_1": "1"}))
    walk += [
        await flow.async_step_price_sell(),
        await flow.async_step_price_sell_formula(FIXED_INPUT),
        await flow.async_step_price_sell_fees({"fee_1": 0.0}),
        await flow.async_step_price_levels({}),
        await flow.async_step_price_forecast(None),
        await flow.async_step_price_forecast({}),
        await flow.async_step_inverter(),
        await flow.async_step_inverter_platform(None),
        await flow.async_step_inverter_platform({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value}),
        await flow.async_step_battery(VALID_BATTERY_INPUT),
        await flow.async_step_inverter_mapping(),
        await flow.async_step_inverter_solis({"inverter_entity_battery_soc": "sensor.soc"}),
        await flow.async_step_sources(None),
        await flow.async_step_sources({}),
        await flow.async_step_sources_energy(None),
        await flow.async_step_sources_energy({}),
        await flow.async_step_bms(None),
        await flow.async_step_forecast(None),
        await flow.async_step_forecast({}),
        await flow.async_step_installation_name(None),
        await flow.async_step_hub(),
    ]
    for result in walk:
        _assert_step_is_wired(flow, result, "config")
    assert (await flow.async_step_finish())["title"] == "sunSale"


async def test_options_root_and_section_menus_are_wired():
    flow = _options_flow(_LEGACY_SOLIS_ENTRY)
    for result in (
        await flow.async_step_init(),
        await flow.async_step_prices(),
        await flow.async_step_inverter(),
        await flow.async_step_price_buy(),
        await flow.async_step_price_sell(),
    ):
        _assert_step_is_wired(flow, result, "options")


def test_every_form_button_says_confirm_and_menu_rows_show_direction():
    """One Confirm button per HA form, plus its save/discard dropdown; menu rows carry → / ← / + / ✕ / ✓."""
    for flow_kind in ("config", "options"):
        for step_id, step in _STRINGS[flow_kind]["step"].items():
            options = step.get("menu_options")
            if options is None:
                assert step.get("submit") == "Confirm", f"{flow_kind}.step.{step_id} button is not Confirm"
                assert step["data"].get("go_to"), f"{flow_kind}.step.{step_id} has no Confirm dropdown label"
                assert step["data_description"]["go_to"], f"{flow_kind}.step.{step_id} has no dropdown hint"
                continue
            for key, label in options.items():
                assert label[:1] in "←+✕✓" or label.endswith("→"), f"{flow_kind}.{step_id}.{key}: {label!r}"


def test_options_strings_mirror_the_config_strings():
    config = set(_STRINGS["config"]["step"]) - {"hub"}
    options = set(_STRINGS["options"]["step"]) - {"init"}
    assert config == options
    assert _STRINGS["config"]["error"] == _STRINGS["options"]["error"]


# ---------------------------------------------------------------------------
# Capability inference
# ---------------------------------------------------------------------------

def test_legacy_entry_infers_prices_inverter_and_forecast():
    caps = resolve_install_capabilities({CONF_INVERTER_PLATFORM: "solis"})
    assert caps == {CAP_PRICES: True, CAP_INVERTER: True, CAP_SOLAR_FORECAST: True}

