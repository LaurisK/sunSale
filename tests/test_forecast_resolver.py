"""Tests for inbound.forecast_resolver — device discovery + base-sensor resolution."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.inbound import forecast_resolver as r


class _RegEntry:
    """Minimal entity-registry entry stub (domain derived from entity_id)."""

    def __init__(self, unique_id: str, entity_id: str) -> None:
        self.unique_id = unique_id
        self.entity_id = entity_id
        self.domain = entity_id.split(".", 1)[0]


class _ConfigEntry:
    """Minimal config-entry stub."""

    def __init__(self, entry_id: str, domain: str, title: str) -> None:
        self.entry_id = entry_id
        self.domain = domain
        self.title = title


def _make_hass(config_entries: list[_ConfigEntry], reg_entries: dict[str, list[_RegEntry]]):
    """Build a hass mock backed by config-entry + per-entry registry stubs.

    Args:
        config_entries: All config entries known to hass.
        reg_entries: Map of entry_id → its entity-registry entries.

    Returns:
        A MagicMock hass whose config_entries methods read the given stubs.
    """
    hass = MagicMock()

    def _async_entries(domain=None):
        return [e for e in config_entries if domain is None or e.domain == domain]

    def _async_get_entry(entry_id):
        return next((e for e in config_entries if e.entry_id == entry_id), None)

    hass.config_entries.async_entries.side_effect = _async_entries
    hass.config_entries.async_get_entry.side_effect = _async_get_entry
    hass._reg_entries = reg_entries
    return hass


@pytest.fixture(autouse=True)
def _patch_registry(monkeypatch):
    """Patch entity-registry helpers to read hass._reg_entries set up per test."""
    monkeypatch.setattr(r.er, "async_get", lambda hass: hass)
    monkeypatch.setattr(
        r.er,
        "async_entries_for_config_entry",
        lambda hass, eid: list(hass._reg_entries.get(eid, [])),
    )


# ---------------------------------------------------------------------------
# discover_forecast_entries
# ---------------------------------------------------------------------------

def test_discover_filters_to_forecast_domains_and_sorts():
    hass = _make_hass(
        [
            _ConfigEntry("e1", "open_meteo_solar_forecast", "Zaliakalnis namai"),
            _ConfigEntry("e2", "solcast_solar", "Garazas"),
            _ConfigEntry("e3", "some_other_integration", "Not forecast"),
        ],
        {},
    )
    options = r.discover_forecast_entries(hass)
    # Other-domain entry excluded; result sorted case-insensitively by label.
    assert options == [
        {"value": "e2", "label": "Garazas"},
        {"value": "e1", "label": "Zaliakalnis namai"},
    ]


def test_discover_empty_when_no_forecast_integrations():
    hass = _make_hass([_ConfigEntry("e1", "zha", "Zigbee")], {})
    assert r.discover_forecast_entries(hass) == []


# ---------------------------------------------------------------------------
# resolve_forecast_entities
# ---------------------------------------------------------------------------

def test_resolve_open_meteo_base_by_unique_id():
    hass = _make_hass(
        [_ConfigEntry("e1", "open_meteo_solar_forecast", "Roof")],
        {
            "e1": [
                _RegEntry("om_e1_power_production_now", "sensor.roof_power_production_now"),
                _RegEntry("om_e1_energy_production_today", "sensor.roof_energy_production_today"),
            ]
        },
    )
    assert r.resolve_forecast_entities(hass, ["e1"]) == [
        "sensor.roof_energy_production_today"
    ]


def test_resolve_solcast_base_by_entity_id_tail():
    hass = _make_hass(
        [_ConfigEntry("e1", "solcast_solar", "Solcast")],
        {
            "e1": [
                # Garbled unique_id; resolution falls back to entity_id tail.
                _RegEntry("uuid-xyz", "sensor.solcast_pv_forecast_forecast_today"),
            ]
        },
    )
    assert r.resolve_forecast_entities(hass, ["e1"]) == [
        "sensor.solcast_pv_forecast_forecast_today"
    ]


def test_resolve_multiple_devices_preserves_order():
    hass = _make_hass(
        [
            _ConfigEntry("e1", "open_meteo_solar_forecast", "A"),
            _ConfigEntry("e2", "forecast_solar", "B"),
        ],
        {
            "e1": [_RegEntry("a_energy_production_today", "sensor.a_energy_production_today")],
            "e2": [_RegEntry("b_energy_production_today", "sensor.b_energy_production_today")],
        },
    )
    # Input order honoured even though discovery would sort differently.
    assert r.resolve_forecast_entities(hass, ["e2", "e1"]) == [
        "sensor.b_energy_production_today",
        "sensor.a_energy_production_today",
    ]


def test_resolve_skips_unresolved_device():
    hass = _make_hass(
        [
            _ConfigEntry("e1", "open_meteo_solar_forecast", "Good"),
            _ConfigEntry("e2", "open_meteo_solar_forecast", "Empty"),
        ],
        {
            "e1": [_RegEntry("g_energy_production_today", "sensor.g_energy_production_today")],
            "e2": [_RegEntry("nope", "sensor.unrelated")],
        },
    )
    assert r.resolve_forecast_entities(hass, ["e1", "e2"]) == [
        "sensor.g_energy_production_today"
    ]


def test_resolve_ignores_non_sensor_entities():
    hass = _make_hass(
        [_ConfigEntry("e1", "open_meteo_solar_forecast", "Roof")],
        {
            "e1": [
                _RegEntry("x_energy_production_today", "binary_sensor.x_energy_production_today"),
                _RegEntry("y_energy_production_today", "sensor.y_energy_production_today"),
            ]
        },
    )
    assert r.resolve_forecast_entities(hass, ["e1"]) == [
        "sensor.y_energy_production_today"
    ]


def test_resolve_unknown_domain_skipped():
    hass = _make_hass(
        [_ConfigEntry("e1", "some_other_integration", "X")],
        {"e1": [_RegEntry("e1_energy_production_today", "sensor.x_energy_production_today")]},
    )
    assert r.resolve_forecast_entities(hass, ["e1"]) == []


def test_resolve_missing_entry_skipped():
    hass = _make_hass([], {})
    assert r.resolve_forecast_entities(hass, ["ghost"]) == []


def test_resolve_empty_input():
    hass = _make_hass([], {})
    assert r.resolve_forecast_entities(hass, []) == []


# ---------------------------------------------------------------------------
# Sensor-level detection (the long tail no forecast integration covers)
# ---------------------------------------------------------------------------

class _State:
    """Minimal HA state stub."""

    def __init__(self, entity_id: str, **attributes) -> None:
        self.entity_id = entity_id
        self.attributes = attributes


def _states_hass(states: list[_State]) -> MagicMock:
    """Return a hass mock whose sensor states are ``states``."""
    hass = MagicMock()
    hass.states.async_all.side_effect = lambda domain: [s for s in states if s.entity_id.startswith(f"{domain}.")]
    return hass


# The shapes the live install publishes: Open-Meteo carries both `watts` and
# `wh_period`; a derived "yesterday" template sensor carries only `wh_period`.
_OPEN_METEO = {"watts": {"2026-09-16T09:00:00+00:00": 1200}, "wh_period": {"2026-09-16T09:00:00+00:00": 300}}
_SOLCAST = {"forecast": [{"time": "2026-09-16T09:00:00+00:00", "pv_estimate": 1.2}]}


def test_a_sensor_is_detected_by_the_data_its_translator_actually_parses():
    assert r.is_forecast_sensor("sensor.energy_production_today", _OPEN_METEO)
    assert r.is_forecast_sensor("sensor.pv_forecast_today", _SOLCAST)


@pytest.mark.parametrize("entity_id, attributes", [
    # Only wh_period: looks like a forecast, but SolarTranslator cannot read it.
    ("sensor.yesterday_forecast_namas", {"wh_period": {"2026-09-16T09:00:00+00:00": 300}}),
    ("sensor.energy_production_today", {"watts": []}),        # wrong type
    ("sensor.energy_production_today", {"forecast": []}),     # empty
    ("sensor.energy_production_today", {}),
    ("sensor.grid_power", {"unit_of_measurement": "W"}),
])
def test_a_sensor_the_translator_could_not_read_is_not_detected(entity_id, attributes):
    assert not r.is_forecast_sensor(entity_id, attributes)


@pytest.mark.parametrize("entity_id", [
    "sensor.energy_production_tomorrow",
    "sensor.energy_production_d3",
    "sensor.energy_production_d7_2",
])
def test_only_a_today_sensor_is_detected(entity_id):
    """The other days are reached by substituting "today", so a dN base is unusable."""
    assert not r.is_forecast_sensor(entity_id, _OPEN_METEO)


def test_detection_lists_the_today_sensors_only():
    hass = _states_hass([
        _State("sensor.energy_production_today", **_OPEN_METEO),
        _State("sensor.energy_production_tomorrow", **_OPEN_METEO),
        _State("sensor.energy_production_d3", **_OPEN_METEO),
        _State("sensor.energy_production_today_2", friendly_name="Namai today", **_OPEN_METEO),
        _State("sensor.grid_power"),
    ])
    found = r.detect_forecast_sensors(hass)
    assert [(s.entity_id, s.name) for s in found] == [
        ("sensor.energy_production_today", ""),
        ("sensor.energy_production_today_2", "Namai today"),
    ]


def test_a_sensor_a_device_already_resolves_is_excluded():
    """The form must not offer the same array as both a device and a sensor."""
    hass = _states_hass([
        _State("sensor.energy_production_today", **_OPEN_METEO),
        _State("sensor.shed_today", **_OPEN_METEO),
    ])
    found = r.detect_forecast_sensors(hass, exclude=["sensor.energy_production_today"])
    assert [s.entity_id for s in found] == ["sensor.shed_today"]


def test_detection_without_hass_finds_nothing():
    assert r.detect_forecast_sensors(None) == []
