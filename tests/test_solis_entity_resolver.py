"""Tests for inbound.solis_entity_resolver — RC-role entity matching."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.inbound import solis_entity_resolver as r


class _Entry:
    """Minimal entity-registry entry stub."""

    def __init__(self, unique_id: str, entity_id: str) -> None:
        self.unique_id = unique_id
        self.entity_id = entity_id


@pytest.fixture
def registry_scan(monkeypatch):
    """Patch the registry helpers; returns a setter for the scanned entries."""
    entries: list[_Entry] = []
    monkeypatch.setattr(r.er, "async_get", lambda hass: MagicMock())
    monkeypatch.setattr(
        r.er, "async_entries_for_config_entry", lambda reg, eid: list(entries),
    )
    return entries


def test_rc_select_resolved_by_register_unique_id(registry_scan):
    registry_scan.append(
        _Entry("solis_modbus_SN123_43132_select", "select.rc_grid_adjustment"),
    )
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["rc_grid_adjustment_select"] == "select.rc_grid_adjustment"


def test_rc_select_not_stolen_by_hidden_readback_sensor(registry_scan):
    # The 43132 readback sensor shares the entity_id tail — the domain guard
    # must keep it from claiming the select role.
    registry_scan.extend([
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_rc_grid_adjustment",
            "sensor.namai_inv_rc_grid_adjustment",
        ),
        _Entry("solis_modbus_SN123_43132_select", "select.rc_grid_adjustment"),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["rc_grid_adjustment_select"] == "select.rc_grid_adjustment"


def test_rc_timeout_prefers_number_over_sensor(registry_scan):
    # Both domains exist for the editable 43282 register; the writable role
    # must resolve to the number entity (second pass override).
    registry_scan.extend([
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_rc_timeout",
            "sensor.namai_inv_rc_timeout_2",
        ),
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_rc_timeout_number",
            "number.namai_inv_rc_timeout_2",
        ),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["rc_timeout"] == "number.namai_inv_rc_timeout_2"


def test_missing_rc_roles_are_optional(registry_scan, caplog):
    # Old solis_modbus versions don't expose the RC entities at all — their
    # absence must not be reported as missing *required* roles.
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert "rc_grid_adjustment_select" not in result
    assert "rc_timeout" not in result
    for record in caplog.records:
        if record.levelname == "WARNING":
            assert "rc_grid_adjustment_select" not in record.getMessage()
            assert "rc_timeout" not in record.getMessage()


def test_battery_energy_counters_resolved_by_entity_tail(registry_scan):
    # The capacity estimator's charge/discharge energy counters resolve via the
    # entity_id tail even when the unique_id is the upstream garbled dict-repr.
    registry_scan.extend([
        _Entry("garbled1", "sensor.namai_inv_today_battery_charge_energy_2"),
        _Entry("garbled2", "sensor.namai_inv_today_battery_discharge_energy_2"),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["battery_charge_energy_today"] == \
        "sensor.namai_inv_today_battery_charge_energy_2"
    assert result["battery_discharge_energy_today"] == \
        "sensor.namai_inv_today_battery_discharge_energy_2"


def test_battery_power_magnitude_and_signed_net_do_not_collide(registry_scan):
    # The magnitude sensor (garbled dict-repr UID) and the derived signed
    # ``battery_power_net`` sensor (clean UID) share the ``battery_power`` stem.
    # Each must resolve to its own role — a cross-match would either pin the
    # panel to "charging" (magnitude stealing the signed role) or flip every
    # reading (net stealing the magnitude role).
    registry_scan.extend([
        _Entry(
            "solis_modbus_SN123_{'name': 'Battery Power', "
            "'unique': 'solis_modbus_inverter_battery_power'}",
            "sensor.namai_inv_battery_power_2",
        ),
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_battery_power_net",
            "sensor.namai_inv_battery_power_net",
        ),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["battery_power"] == "sensor.namai_inv_battery_power_2"
    assert result["battery_power_signed"] == "sensor.namai_inv_battery_power_net"


def test_missing_signed_battery_power_is_optional(registry_scan, caplog):
    # Older solis_modbus has no derived net sensor — only the magnitude. Its
    # absence must not be reported as a missing *required* role (get_battery_power
    # degrades to the unsigned magnitude).
    registry_scan.append(
        _Entry("garbled", "sensor.namai_inv_battery_power_2"),
    )
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["battery_power"] == "sensor.namai_inv_battery_power_2"
    assert "battery_power_signed" not in result
    for record in caplog.records:
        if record.levelname == "WARNING":
            assert "battery_power_signed" not in record.getMessage()


def test_missing_battery_energy_counters_are_optional(registry_scan, caplog):
    # Absent counters only disable capacity *learning*, so they must not be
    # reported as missing *required* roles.
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert "battery_charge_energy_today" not in result
    assert "battery_discharge_energy_today" not in result
    for record in caplog.records:
        if record.levelname == "WARNING":
            assert "battery_charge_energy_today" not in record.getMessage()
            assert "battery_discharge_energy_today" not in record.getMessage()


# --- Disabled-by-default canonical sensors --------------------------------- #


def test_storage_control_falls_back_to_the_33132_mirror(registry_scan):
    # The canonical 43110 readback is ``hidden: True`` upstream, so a config
    # entry created after that flag landed never registers an enabled entity
    # for it. Without the fallback the role resolves to nothing and the
    # observed mode reads ``unknown`` forever (live: sodas, 2026-09-22).
    registry_scan.append(
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_storage_control_switching_value",
            "sensor.solis_s6_eh3p_storage_control_switching_value",
        ),
    )
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["storage_control_readback"] == \
        "sensor.solis_s6_eh3p_storage_control_switching_value"


def test_storage_control_prefers_the_canonical_43110_sensor(registry_scan):
    # Where an older entry still carries both, the primary map wins — and must
    # do so regardless of which order the registry hands them back.
    registry_scan.extend([
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_storage_control_switching_value",
            "sensor.namai_inv_storage_control_switching_value_2",
        ),
        _Entry(
            "solis_modbus_SN123_solis_modbus_inverter_storage_control_switch_value",
            "sensor.namai_inv_storage_control_switch_value_2",
        ),
    ])
    result = r.resolve_solis_entities(MagicMock(), "entry")
    assert result["storage_control_readback"] == \
        "sensor.namai_inv_storage_control_switch_value_2"
