"""Tests for inbound.inverter_entity_resolver."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sun_sale.inbound import solis_entity_resolver as solis
from custom_components.sun_sale.inbound.inverter_entity_resolver import (
    resolve_inverter_entities,
)
from custom_components.sun_sale.outbound.inverter import InverterPlatform
from custom_components.sun_sale.contract.const import (
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_PLATFORM,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    DEFAULT_SOLIS_RC_GRID_ADJUSTMENT_SELECT,
    DEFAULT_SOLIS_RC_TIMEOUT,
    DEFAULT_SOLIS_SELF_USE_SWITCH,
)


def _hass_with_solis_entries(entries):
    """Return a MagicMock hass whose solis_modbus entry scan yields ``entries``."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = entries
    return hass


def _generic_data():
    """Minimal generic-platform config dict with all required role entities."""
    return {
        CONF_INVERTER_PLATFORM: InverterPlatform.GENERIC.value,
        CONF_INVERTER_ENTITY_BATTERY_SOC: "sensor.soc",
        CONF_INVERTER_ENTITY_BATTERY_POWER: "sensor.batt_power",
        CONF_INVERTER_ENTITY_GRID_POWER: "sensor.grid",
        CONF_INVERTER_ENTITY_CHARGE_CONTROL: "switch.charge",
    }


def _solis_data():
    """Minimal Solis-platform config dict (manual-mapping required keys)."""
    return {
        CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
        CONF_INVERTER_ENTITY_BATTERY_SOC: "sensor.soc",
        CONF_INVERTER_ENTITY_BATTERY_POWER: "sensor.batt_power",
        CONF_INVERTER_ENTITY_GRID_POWER: "sensor.grid_net",
    }


# --- Generic platform -----------------------------------------------------

def test_generic_platform_manual_mapping():
    """Generic platform reads the manual role mapping, including charge_control."""
    hass = MagicMock()
    resolved = resolve_inverter_entities(hass, _generic_data())

    assert resolved.platform == InverterPlatform.GENERIC
    ids = resolved.inverter_entity_ids
    assert ids["battery_soc"] == "sensor.soc"
    assert ids["charge_control"] == "switch.charge"
    assert resolved.signed_grid_power == "sensor.grid"
    # Generic never touches the solis_modbus registry.
    hass.config_entries.async_entries.assert_not_called()


# --- Solis manual fallback ------------------------------------------------

def test_solis_manual_fallback_when_no_config_entry():
    """No config-entry id and no sole solis entry → manual mapping with defaults."""
    hass = _hass_with_solis_entries([])
    resolved = resolve_inverter_entities(hass, _solis_data())

    assert resolved.platform == InverterPlatform.SOLIS
    ids = resolved.inverter_entity_ids
    # Manual fallback carries the solis switch/number defaults.
    assert ids["self_use_switch"] == DEFAULT_SOLIS_SELF_USE_SWITCH
    assert ids["rc_grid_adjustment_select"] == DEFAULT_SOLIS_RC_GRID_ADJUSTMENT_SELECT
    assert ids["rc_timeout"] == DEFAULT_SOLIS_RC_TIMEOUT
    assert "charge_control" not in ids


# --- Solis auto-detect ----------------------------------------------------

def test_solis_autodetect_runs_resolver_and_merges(monkeypatch):
    """A stored config-entry id triggers the resolver; its roles flow through."""
    auto = {
        "grid_power": "sensor.auto_grid_net",
        "grid_import_energy_today": "sensor.auto_imp",
        "grid_export_energy_today": "sensor.auto_exp",
        "pv_power": "sensor.auto_pv",
        "solar_energy_today": "sensor.auto_solar",
        "ac_port_power": "sensor.auto_ac",
        "backup_power": "sensor.auto_backup",
        "solar_energy_yesterday": "sensor.auto_gen_yday",
        "grid_import_energy_yesterday": "sensor.auto_imp_yday",
        "grid_export_energy_yesterday": "sensor.auto_exp_yday",
    }
    monkeypatch.setattr(solis, "resolve_solis_entities", lambda hass, eid: dict(auto))

    data = _solis_data()
    data[CONF_SOLIS_CONFIG_ENTRY_ID] = "abc123"
    resolved = resolve_inverter_entities(MagicMock(), data)

    # Telemetry roles missing from the resolver are backfilled from config.
    assert resolved.inverter_entity_ids["battery_soc"] == "sensor.soc"
    # Observer-pipeline entities pick up the auto-detected values.
    assert resolved.signed_grid_power == "sensor.auto_grid_net"
    assert resolved.pv_power == "sensor.auto_pv"
    assert resolved.solar_energy_today == "sensor.auto_solar"
    assert resolved.ac_port_power == "sensor.auto_ac"
    assert resolved.backup_power == "sensor.auto_backup"
    # Yesterday-total entities are mapped into the raw config dict in place.
    assert data["inverter_entity_generation_yesterday"] == "sensor.auto_gen_yday"
    assert data["inverter_entity_grid_import_yesterday"] == "sensor.auto_imp_yday"
    assert data["inverter_entity_grid_export_yesterday"] == "sensor.auto_exp_yday"


# --- Manual-first / auto-second merge -------------------------------------

def test_manual_config_overrides_autodetect_for_observer_ids(monkeypatch):
    """A manually configured observer entity wins over the auto-detected one."""
    monkeypatch.setattr(
        solis, "resolve_solis_entities",
        lambda hass, eid: {"pv_power": "sensor.auto_pv", "ac_port_power": "sensor.auto_ac"},
    )
    data = _solis_data()
    data[CONF_SOLIS_CONFIG_ENTRY_ID] = "abc123"
    data[CONF_INVERTER_ENTITY_PV_POWER] = "sensor.manual_pv"
    data[CONF_INVERTER_ENTITY_AC_PORT_POWER] = ""  # empty → falls back to auto

    resolved = resolve_inverter_entities(MagicMock(), data)

    assert resolved.pv_power == "sensor.manual_pv"
    assert resolved.ac_port_power == "sensor.auto_ac"


def test_per_direction_grid_power_from_config():
    """Per-direction grid-power entities come straight from config keys."""
    data = _solis_data()
    data[CONF_INVERTER_ENTITY_GRID_IMPORT_POWER] = "sensor.imp_power"
    resolved = resolve_inverter_entities(_hass_with_solis_entries([]), data)

    assert resolved.grid_import_power == "sensor.imp_power"
    assert resolved.grid_export_power == ""
