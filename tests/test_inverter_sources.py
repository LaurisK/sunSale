"""Tests for inbound/inverter_sources.py — which sensors an inverter source is offered."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.contract.const import (
    CONF_BMS_BATTERY_POWER,
    CONF_BMS_BATTERY_SOC,
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER,
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_GENERATION_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_PLATFORM,
)
from custom_components.sun_sale.inbound import inverter_sources as sources
from custom_components.sun_sale.inbound.inverter_sources import (
    BMS_SPECS,
    SOURCE_SPECS,
    SPEC_BY_KEY,
    DetectedSource,
    default_source,
    detect_bms_sources,
    detect_inverter_sources,
    device_of,
    role_candidates,
)
from custom_components.sun_sale.outbound.inverter import InverterPlatform


def _entry(
    entity_id: str,
    *,
    platform: str = "solis_modbus",
    device_class: str | None = None,
    original_device_class: str | None = "power",
    state_class: str | None = "measurement",
    disabled_by: str | None = None,
    hidden_by: str | None = None,
    original_name: str | None = None,
    device_id: str | None = "dev1",
) -> SimpleNamespace:
    """Return an entity-registry-entry look-alike."""
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".", 1)[0],
        platform=platform,
        device_class=device_class,
        original_device_class=original_device_class,
        capabilities={"state_class": state_class} if state_class else None,
        disabled_by=disabled_by,
        hidden_by=hidden_by,
        original_name=original_name,
        name=None,
        device_id=device_id,
    )


def _counter(entity_id: str, *, state_class: str | None = "total_increasing", **kwargs) -> SimpleNamespace:
    """Return a registry entry shaped like a daily-resetting energy counter."""
    return _entry(entity_id, original_device_class="energy", state_class=state_class, **kwargs)


def _no_attributes(entity_id: str) -> dict:
    """Return no state attributes, as for an entity without a state yet."""
    return {}


def _spec(conf_key: str):
    """Return the source spec for a config key."""
    return SPEC_BY_KEY[conf_key]


# ---------------------------------------------------------------------------
# Classification core
# ---------------------------------------------------------------------------

def test_a_source_only_offers_sensors_reporting_the_kind_of_value_it_needs():
    entries = [
        _entry("sensor.total_pv_power"),
        _counter("sensor.pv_today_energy_generation"),   # energy, not power
        _entry("number.export_limit", original_device_class=None),
        _entry("sensor.battery_soc", original_device_class="battery"),
    ]
    found = role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_PV_POWER))
    assert [source.entity_id for source in found] == ["sensor.total_pv_power"]


def test_an_energy_counter_source_rejects_an_instantaneous_energy_reading():
    entries = [
        _counter("sensor.today_energy_imported_from_grid"),
        _entry("sensor.grid_import_right_now", original_device_class="energy", state_class="measurement"),
    ]
    found = role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY))
    assert [source.entity_id for source in found] == ["sensor.today_energy_imported_from_grid"]


def test_a_hidden_disabled_helper_or_own_sensor_is_never_offered():
    entries = [
        _entry("sensor.pv_power_disabled", disabled_by="user"),
        _entry("sensor.pv_power_hidden", hidden_by="integration"),
        _entry("sensor.pv_power_helper", platform="utility_meter"),
        _entry("sensor.sunsale_pv_power", platform="sun_sale"),
        _entry("switch.pv_power", original_device_class=None),
    ]
    assert role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_PV_POWER)) == []


def test_state_attributes_stand_in_for_classes_the_registry_does_not_carry():
    entry = _entry("sensor.today_solar", original_device_class=None, state_class=None)
    attributes = {"sensor.today_solar": {"device_class": "energy", "state_class": "total_increasing"}}
    found = role_candidates(
        [entry], attributes.get, _spec(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY),
    )
    assert [source.entity_id for source in found] == ["sensor.today_solar"]


def test_today_and_yesterday_counters_never_appear_in_each_others_list():
    entries = [
        _counter("sensor.pv_today_energy_generation"),
        _counter("sensor.pv_yesterday_energy_generation", state_class="total"),
    ]
    today = role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY))
    yesterday = role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_GENERATION_YESTERDAY))
    assert [source.entity_id for source in today] == ["sensor.pv_today_energy_generation"]
    assert [source.entity_id for source in yesterday] == ["sensor.pv_yesterday_energy_generation"]


def test_a_yesterday_total_is_offered_even_without_a_counter_state_class():
    """A finalised daily total is often published as a plain measurement."""
    entry = _counter("sensor.pv_yesterday_energy_generation", state_class="measurement")
    found = role_candidates([entry], _no_attributes, _spec(CONF_INVERTER_ENTITY_GENERATION_YESTERDAY))
    assert [source.entity_id for source in found] == ["sensor.pv_yesterday_energy_generation"]


def test_every_same_kind_sensor_is_offered_with_the_likely_one_first():
    """Ranked, not filtered — an unusually named sensor must stay reachable."""
    entries = [
        _entry("sensor.zzz_unlabelled_power"),
        _entry("sensor.backup_load_power"),
        _entry("sensor.aaa_other_power"),
    ]
    found = role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_BACKUP_POWER))
    assert [source.entity_id for source in found] == [
        "sensor.backup_load_power", "sensor.aaa_other_power", "sensor.zzz_unlabelled_power",
    ]


def test_what_the_platform_resolved_outranks_even_a_name_match():
    entries = [_entry("sensor.ac_grid_port_power"), _entry("sensor.inverter_output")]
    found = role_candidates(
        entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_AC_PORT_POWER), auto="sensor.inverter_output",
    )
    assert [(source.entity_id, source.auto) for source in found] == [
        ("sensor.inverter_output", True), ("sensor.ac_grid_port_power", False),
    ]


def test_a_display_name_matches_a_hint_as_well_as_an_entity_id():
    entries = [_entry("sensor.s1", original_name="Backup load power"), _entry("sensor.s0")]
    found = role_candidates(entries, _no_attributes, _spec(CONF_INVERTER_ENTITY_BACKUP_POWER))
    assert [source.entity_id for source in found] == ["sensor.s1", "sensor.s0"]


def test_duplicate_entries_are_dropped():
    entry = _entry("sensor.total_pv_power")
    found = role_candidates([entry, entry], _no_attributes, _spec(CONF_INVERTER_ENTITY_PV_POWER))
    assert len(found) == 1


@pytest.mark.parametrize("spec", [*SOURCE_SPECS, *BMS_SPECS], ids=lambda spec: spec.conf_key)
def test_every_spec_has_hints_and_a_supported_device_class(spec):
    assert spec.hints
    assert spec.device_class in {"power", "energy", "battery"}


# ---------------------------------------------------------------------------
# Pre-selection
# ---------------------------------------------------------------------------

def test_what_the_user_stored_is_pre_selected_over_any_guess():
    candidates = [
        DetectedSource("sensor.auto_pv_power", "", True),
        DetectedSource("sensor.my_pv_power", "", False),
    ]
    spec = _spec(CONF_INVERTER_ENTITY_PV_POWER)
    assert default_source(candidates, spec, stored="sensor.my_pv_power") == "sensor.my_pv_power"


def test_a_stored_entity_that_is_no_longer_a_candidate_does_not_win():
    candidates = [DetectedSource("sensor.auto_pv_power", "", True)]
    spec = _spec(CONF_INVERTER_ENTITY_PV_POWER)
    assert default_source(candidates, spec, stored="sensor.gone") == "sensor.auto_pv_power"


def test_a_name_hint_is_pre_selected_when_the_platform_resolved_nothing():
    candidates = [
        DetectedSource("sensor.aaa_other_power", "", False),
        DetectedSource("sensor.battery_charge_today", "", False),
    ]
    spec = _spec(CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY)
    assert default_source(candidates, spec) == "sensor.battery_charge_today"


def test_nothing_is_pre_selected_when_no_candidate_looks_right():
    """Device class alone is too weak a signal to fill a source in silently."""
    candidates = [DetectedSource("sensor.aaa_other_power", "", False)]
    assert default_source(candidates, _spec(CONF_INVERTER_ENTITY_BACKUP_POWER)) == ""


# ---------------------------------------------------------------------------
# Home Assistant edge
# ---------------------------------------------------------------------------

@pytest.fixture
def inverter_device(monkeypatch):
    """Stand in for the entity registry: serve ``entries`` as the inverter's device."""
    def serve(entries: list, roles: dict[str, str]) -> MagicMock:
        by_id = {entry.entity_id: entry for entry in entries}
        registry = MagicMock()
        registry.async_get.side_effect = by_id.get
        monkeypatch.setattr(sources.er, "async_get", lambda _hass: registry)
        monkeypatch.setattr(sources.er, "async_entries_for_device", lambda _reg, _device_id: list(entries))
        monkeypatch.setattr(sources, "_resolved_roles", lambda _hass, _data: roles)
        hass = MagicMock()
        hass.states.get.return_value = None
        return hass
    return serve


def test_detection_scans_the_device_the_resolved_entities_live_on(inverter_device):
    entries = [
        _entry("sensor.solis_total_pv_power"),
        _entry("sensor.solis_ac_grid_port_power"),
        _entry("sensor.solis_backup_load_power"),
        _counter("sensor.solis_today_energy_fed_into_grid"),
    ]
    # Only one role is resolved: it is what locates the device, and everything
    # else on that device is then classified from scratch.
    hass = inverter_device(entries, {"pv_power": "sensor.solis_total_pv_power", "battery_soc": ""})

    detected = detect_inverter_sources(hass, {CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value})

    assert detected[CONF_INVERTER_ENTITY_PV_POWER].default == "sensor.solis_total_pv_power"
    assert detected[CONF_INVERTER_ENTITY_PV_POWER].candidates[0].auto is True
    assert detected[CONF_INVERTER_ENTITY_AC_PORT_POWER].default == "sensor.solis_ac_grid_port_power"
    assert detected[CONF_INVERTER_ENTITY_BACKUP_POWER].default == "sensor.solis_backup_load_power"
    assert detected[CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY].default == "sensor.solis_today_energy_fed_into_grid"
    # Nothing on this device could be a yesterday total, so the form falls back.
    assert CONF_INVERTER_ENTITY_GENERATION_YESTERDAY not in detected


def test_a_stored_source_survives_detection_that_would_guess_otherwise(inverter_device):
    entries = [_entry("sensor.solis_backup_load_power"), _entry("sensor.my_own_backup_meter")]
    hass = inverter_device(entries, {"backup_power": "sensor.solis_backup_load_power"})

    detected = detect_inverter_sources(hass, {
        CONF_INVERTER_PLATFORM: InverterPlatform.SOLIS.value,
        CONF_INVERTER_ENTITY_BACKUP_POWER: "sensor.my_own_backup_meter",
    })

    assert detected[CONF_INVERTER_ENTITY_BACKUP_POWER].default == "sensor.my_own_backup_meter"


def test_a_battery_device_resolves_its_soc_and_pack_power(inverter_device):
    entries = [
        _entry("sensor.jk_bms_battery_soc", platform="jk_bms", original_device_class="battery"),
        _entry("sensor.jk_bms_battery_power", platform="jk_bms"),
        _entry("sensor.jk_bms_cell_voltage", platform="jk_bms", original_device_class="voltage"),
    ]
    hass = inverter_device(entries, {})

    detected = detect_bms_sources(hass, "dev_bms", {})

    assert detected[CONF_BMS_BATTERY_SOC].default == "sensor.jk_bms_battery_soc"
    assert detected[CONF_BMS_BATTERY_POWER].default == "sensor.jk_bms_battery_power"


def test_a_device_with_no_state_of_charge_cannot_serve_as_a_bms(inverter_device):
    """SoC is what puts the BMS in front of the inverter, so the page refuses such a device."""
    hass = inverter_device([_entry("sensor.shelly_power", platform="shelly")], {})
    assert CONF_BMS_BATTERY_SOC not in detect_bms_sources(hass, "dev_plug", {})


def test_no_device_picked_detects_no_bms():
    assert detect_bms_sources(MagicMock(), "", {}) == {}


def test_the_bms_sources_are_never_offered_off_the_inverter():
    """Offering the inverter's own battery sensors as "the BMS" would defeat the point."""
    assert CONF_BMS_BATTERY_SOC not in SPEC_BY_KEY
    assert CONF_BMS_BATTERY_POWER not in SPEC_BY_KEY
    assert {spec.conf_key for spec in BMS_SPECS} == {CONF_BMS_BATTERY_SOC, CONF_BMS_BATTERY_POWER}


def test_a_device_is_recovered_from_the_entity_stored_on_it(monkeypatch):
    """Which device holds the BMS needs no config key of its own."""
    registry = MagicMock()
    registry.async_get.side_effect = {"sensor.jk_soc": SimpleNamespace(device_id="dev_bms")}.get
    monkeypatch.setattr(sources.er, "async_get", lambda _hass: registry)
    assert device_of(MagicMock(), "sensor.jk_soc") == "dev_bms"
    assert device_of(MagicMock(), "sensor.unknown") == ""
    assert device_of(MagicMock(), "") == ""


def test_detection_yields_nothing_while_the_inverter_cannot_be_located():
    """A half-filled setup flow is the normal case here, not an error."""
    assert detect_inverter_sources(MagicMock(), {}) == {}
    assert detect_inverter_sources(MagicMock(), {CONF_INVERTER_PLATFORM: "not-a-platform"}) == {}
