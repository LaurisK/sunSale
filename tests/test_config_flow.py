"""Tests for config_flow.py — SunSaleConfigFlow validation and step routing."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sun_sale.config_flow import SunSaleConfigFlow
from custom_components.sun_sale.contract.const import (
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_INVERTER_EXPORT_LIMIT_KW,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_INVERTER_PLATFORM,
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
)
from custom_components.sun_sale.outbound.inverter import InverterPlatform

VALID_TARIFF_INPUT = {
    CONF_TARIFF_DISTRIBUTION_FEE: 0.03,
    CONF_TARIFF_TAX_RATE: 21.0,
    CONF_TARIFF_MARKUP: 0.005,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE: 0.01,
    CONF_TARIFF_SELL_TAX_RATE: 0.0,
    CONF_TARIFF_SELL_MARKUP: 0.0,
}

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


# ---------------------------------------------------------------------------
# async_step_user — form and validation
# ---------------------------------------------------------------------------

async def test_step_user_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_user(None)
    assert result["step_id"] == "user"
    assert result.get("errors", {}) == {}


async def test_step_user_valid_input_proceeds_to_weekday_bands():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_user(VALID_TARIFF_INPUT)
    assert result["step_id"] == "tariff_weekday"


async def test_tariff_band_steps_chain_to_battery():
    flow = SunSaleConfigFlow()
    await flow.async_step_user(VALID_TARIFF_INPUT)
    # Empty band forms (all rows blank) → no bands, proceed weekday → weekend → battery.
    result = await flow.async_step_tariff_weekday({})
    assert result["step_id"] == "tariff_weekend"
    assert flow._data["tariff_weekday_bands"] == []
    result = await flow.async_step_tariff_weekend({})
    assert result["step_id"] == "battery"
    assert flow._data["tariff_weekend_bands"] == []


async def test_tariff_band_step_parses_and_validates():
    flow = SunSaleConfigFlow()
    await flow.async_step_user(VALID_TARIFF_INPUT)
    # One valid band + one malformed start → error, stays on the step.
    bad = {"band0_start": "08:00", "band0_buy_fee": 0.06, "band0_sell_fee": 0.04,
           "band1_start": "nope", "band1_buy_fee": 0.02, "band1_sell_fee": 0.01}
    result = await flow.async_step_tariff_weekday(bad)
    assert result["step_id"] == "tariff_weekday"
    assert "band1_start" in result["errors"]
    # Fix it → bands stored, normalised and sorted.
    good = {"band0_start": "8:00", "band0_buy_fee": 0.06, "band0_sell_fee": 0.04,
            "band1_start": "23:30", "band1_buy_fee": 0.02, "band1_sell_fee": 0.01}
    result = await flow.async_step_tariff_weekday(good)
    assert result["step_id"] == "tariff_weekend"
    assert flow._data["tariff_weekday_bands"] == [
        {"start": "08:00", "buy_fee": 0.06, "sell_fee": 0.04},
        {"start": "23:30", "buy_fee": 0.02, "sell_fee": 0.01},
    ]


async def test_step_user_negative_fee_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_TARIFF_INPUT, CONF_TARIFF_DISTRIBUTION_FEE: -0.01}
    result = await flow.async_step_user(bad)
    assert result["step_id"] == "user"
    assert CONF_TARIFF_DISTRIBUTION_FEE in result["errors"]


async def test_step_user_invalid_tax_rate_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_TARIFF_INPUT, CONF_TARIFF_TAX_RATE: 150.0}
    result = await flow.async_step_user(bad)
    assert result["step_id"] == "user"
    assert CONF_TARIFF_TAX_RATE in result["errors"]


async def test_step_user_negative_tax_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_TARIFF_INPUT, CONF_TARIFF_TAX_RATE: -1.0}
    result = await flow.async_step_user(bad)
    assert result["step_id"] == "user"
    assert CONF_TARIFF_TAX_RATE in result["errors"]


async def test_step_user_stores_data():
    flow = SunSaleConfigFlow()
    await flow.async_step_user(VALID_TARIFF_INPUT)
    assert flow._data[CONF_TARIFF_DISTRIBUTION_FEE] == 0.03
    assert flow._data[CONF_TARIFF_TAX_RATE] == 21.0


# ---------------------------------------------------------------------------
# async_step_battery — form and validation
# ---------------------------------------------------------------------------

async def test_step_battery_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_battery(None)
    assert result["step_id"] == "battery"


async def test_step_battery_valid_input_proceeds_to_inverter():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_battery(VALID_BATTERY_INPUT)
    assert result["step_id"] == "inverter"


async def test_step_battery_zero_capacity_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_NOMINAL_CAPACITY: 0.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_NOMINAL_CAPACITY in result["errors"]


async def test_step_battery_zero_price_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_PURCHASE_PRICE: 0.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_PURCHASE_PRICE in result["errors"]


async def test_step_battery_invalid_efficiency_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 110.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_ROUND_TRIP_EFFICIENCY in result["errors"]


async def test_step_battery_zero_efficiency_returns_error():
    flow = SunSaleConfigFlow()
    bad = {**VALID_BATTERY_INPUT, CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 0.0}
    result = await flow.async_step_battery(bad)
    assert result["step_id"] == "battery"
    assert CONF_BATTERY_ROUND_TRIP_EFFICIENCY in result["errors"]


# ---------------------------------------------------------------------------
# async_step_inverter — platform routing
# ---------------------------------------------------------------------------

async def test_step_inverter_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_inverter(None)
    assert result["step_id"] == "inverter"


def _flow_with_hass(solis_entries: list, forecast_entries: list | None = None) -> SunSaleConfigFlow:
    """Return a SunSaleConfigFlow with a mock hass pre-configured with entries.

    ``async_entries`` is domain-aware: solis_modbus returns ``solis_entries``;
    recognised forecast domains return ``forecast_entries`` (default empty).
    """
    flow = SunSaleConfigFlow()
    mock_hass = MagicMock()
    forecast = forecast_entries or []

    def _async_entries(domain):
        if domain == "solis_modbus":
            return solis_entries
        return [e for e in forecast if getattr(e, "domain", None) == domain]

    mock_hass.config_entries.async_entries.side_effect = _async_entries
    flow.hass = mock_hass
    return flow


async def test_step_inverter_solis_routes_to_solis_step_no_entries():
    flow = _flow_with_hass([])
    result = await flow.async_step_inverter({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    assert result["step_id"] == "inverter_solis"


async def test_step_inverter_solis_autodetects_single_entry():
    entry = MagicMock()
    entry.entry_id = "solis_entry_abc"
    flow = _flow_with_hass([entry])
    result = await flow.async_step_inverter_solis(None)
    # Auto-detect advances to the forecast step; with no forecast devices it
    # shows the manual fallback form.
    assert result["step_id"] == "forecast_manual"
    assert flow._data[CONF_SOLIS_CONFIG_ENTRY_ID] == "solis_entry_abc"


async def test_step_inverter_solis_shows_picker_for_multiple_entries():
    entries = [
        MagicMock(entry_id="e1", title="Solis 1"),
        MagicMock(entry_id="e2", title="Solis 2"),
    ]
    flow = _flow_with_hass(entries)
    result = await flow.async_step_inverter_solis(None)
    assert result["step_id"] == "inverter_solis"


async def test_step_inverter_solis_picker_submission_proceeds():
    entries = [
        MagicMock(entry_id="e1", title="Solis 1"),
        MagicMock(entry_id="e2", title="Solis 2"),
    ]
    flow = _flow_with_hass(entries)
    result = await flow.async_step_inverter_solis({CONF_SOLIS_CONFIG_ENTRY_ID: "e2"})
    assert result["step_id"] == "forecast_manual"
    assert flow._data[CONF_SOLIS_CONFIG_ENTRY_ID] == "e2"


async def test_step_forecast_shows_device_picker_when_discovered():
    fc = MagicMock(entry_id="f1", title="Garazas")
    fc.domain = "open_meteo_solar_forecast"
    flow = _flow_with_hass([], forecast_entries=[fc])
    result = await flow.async_step_forecast(None)
    assert result["step_id"] == "forecast"


async def test_step_forecast_manual_fallback_when_none_discovered():
    flow = _flow_with_hass([], forecast_entries=[])
    result = await flow.async_step_forecast(None)
    assert result["step_id"] == "forecast_manual"


async def test_step_forecast_submission_stores_devices_and_proceeds():
    fc = MagicMock(entry_id="f1", title="Garazas")
    fc.domain = "solcast_solar"
    flow = _flow_with_hass([], forecast_entries=[fc])
    result = await flow.async_step_forecast({CONF_SOLAR_FORECAST_DEVICE_IDS: ["f1"]})
    assert result["step_id"] == "sources"
    assert flow._data[CONF_SOLAR_FORECAST_DEVICE_IDS] == ["f1"]


async def test_step_forecast_manual_submission_proceeds():
    flow = _flow_with_hass([], forecast_entries=[])
    result = await flow.async_step_forecast_manual({CONF_SOLAR_FORECAST_ENTITY: "sensor.pv_today"})
    assert result["step_id"] == "sources"
    assert flow._data[CONF_SOLAR_FORECAST_ENTITY] == "sensor.pv_today"


async def test_step_inverter_unimplemented_platform_is_rejected():
    # Non-Solis platforms are listed for reference but not selectable: choosing
    # one re-shows the inverter step with an error and never advances.
    flow = SunSaleConfigFlow()
    result = await flow.async_step_inverter({CONF_INVERTER_PLATFORM: InverterPlatform.GENERIC.value})
    assert result["step_id"] == "inverter"
    assert result["errors"] == {"base": "platform_not_implemented"}
    assert CONF_INVERTER_PLATFORM not in flow._data


async def test_step_inverter_stores_platform():
    flow = _flow_with_hass([])
    await flow.async_step_inverter({CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})
    assert flow._data[CONF_INVERTER_PLATFORM] == InverterPlatform.SOLIS.value


async def test_step_inverter_stores_power_ratings():
    flow = _flow_with_hass([])
    await flow.async_step_inverter({
        CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
        CONF_INVERTER_MAX_POWER_KW: 5.0,
        CONF_INVERTER_EXPORT_LIMIT_KW: 4.0,
    })
    assert flow._data[CONF_INVERTER_MAX_POWER_KW] == 5.0
    assert flow._data[CONF_INVERTER_EXPORT_LIMIT_KW] == 4.0


async def test_step_inverter_rejects_non_positive_max_power():
    flow = _flow_with_hass([])
    result = await flow.async_step_inverter({
        CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
        CONF_INVERTER_MAX_POWER_KW: 0.0,
        CONF_INVERTER_EXPORT_LIMIT_KW: 4.0,
    })
    assert result["step_id"] == "inverter"
    assert result["errors"] == {CONF_INVERTER_MAX_POWER_KW: "invalid_inverter_power"}
    assert CONF_INVERTER_PLATFORM not in flow._data


async def test_step_inverter_rejects_negative_export_limit():
    flow = _flow_with_hass([])
    result = await flow.async_step_inverter({
        CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
        CONF_INVERTER_MAX_POWER_KW: 5.0,
        CONF_INVERTER_EXPORT_LIMIT_KW: -1.0,
    })
    assert result["step_id"] == "inverter"
    assert result["errors"] == {CONF_INVERTER_EXPORT_LIMIT_KW: "invalid_export_limit"}


# ---------------------------------------------------------------------------
# async_step_sources — entry creation
# ---------------------------------------------------------------------------

async def test_step_sources_no_input_shows_form():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_sources(None)
    assert result["step_id"] == "sources"


async def test_step_sources_creates_entry():
    flow = SunSaleConfigFlow()
    result = await flow.async_step_sources({
        CONF_NORDPOOL_ENTITY: "sensor.nordpool",
        CONF_SOLAR_FORECAST_ENTITY: "",
    })
    assert result["title"] == "sunSale"
    assert result["data"][CONF_NORDPOOL_ENTITY] == "sensor.nordpool"
