"""Tests for the declarative entity-resolution profiles (layer-2 wiring).

Covers the generic resolver's pattern matching + domain guard, the device-role
resolution, and the discovery strategy's auto-detect vs manual paths. The role
*patterns* themselves are untested-against-hardware best guesses; these tests
pin the machinery that consumes them so a pattern tweak is the only thing left
to do on first connect.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.sun_sale.inbound import platform_profiles as pp
from custom_components.sun_sale.inbound.platform_profiles import (
    ControlProfile,
    ProfileEntityDiscovery,
    RoleSpec,
    config_entry_key,
    manual_role_key,
    profile_for,
    resolve_profile_entities,
    unbacked_roles,
)
from custom_components.sun_sale.outbound.deye_driver import make_deye_driver
from custom_components.sun_sale.outbound.entity_control import InverterContext
from custom_components.sun_sale.outbound.goodwe_driver import make_goodwe_driver
from custom_components.sun_sale.outbound.huawei_driver import make_huawei_driver
from custom_components.sun_sale.outbound.inverter import InverterPlatform
from custom_components.sun_sale.outbound.solax_driver import make_solax_driver
from custom_components.sun_sale.outbound.sungrow_driver import make_sungrow_driver
from tests.conftest import default_battery_config

# --- Fakes ------------------------------------------------------------------ #


class _RegEntry:
    def __init__(self, entity_id: str, unique_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id


class _Device:
    def __init__(self, id_: str, name: str = "", model: str = "") -> None:
        self.id = id_
        self.name = name
        self.model = model


def _patch_registries(entities: list[_RegEntry], devices: list[_Device]):
    """Patch the entity + device registry helpers used by the resolver."""
    er = patch.object(
        pp.er, "async_entries_for_config_entry", return_value=entities,
    )
    er2 = patch.object(pp.er, "async_get", return_value=MagicMock())
    dr = patch.object(
        pp.dr, "async_entries_for_config_entry", return_value=devices,
    )
    dr2 = patch.object(pp.dr, "async_get", return_value=MagicMock())
    return er, er2, dr, dr2


# --- profile_for / key generators ------------------------------------------- #


@pytest.mark.parametrize("platform", [
    InverterPlatform.HUAWEI_SOLAR, InverterPlatform.SOLAX,
    InverterPlatform.SUNGROW, InverterPlatform.GOODWE, InverterPlatform.DEYE,
])
def test_every_entity_driven_platform_has_a_profile(platform):
    prof = profile_for(platform)
    assert prof is not None
    # Each profile carries the three core telemetry roles + a mode select.
    role_names = {r.role for r in prof.roles}
    assert {"battery_soc", "battery_power", "grid_power"} <= role_names


def test_unprofiled_platform_returns_none():
    assert profile_for(InverterPlatform.FRONIUS) is None


def test_key_generators_are_deterministic_and_distinct():
    assert config_entry_key(InverterPlatform.HUAWEI_SOLAR) == "huawei_solar_config_entry_id"
    assert manual_role_key(InverterPlatform.SOLAX, "use_mode_select") == "inverter_solax_modbus_use_mode_select"


# --- resolver --------------------------------------------------------------- #


def _profile() -> ControlProfile:
    return ControlProfile(
        platform=InverterPlatform.GOODWE,
        config_entry_domain="goodwe",
        roles=(
            RoleSpec("operation_mode_select", "select", True, ("operation_mode",), ("operation_mode",)),
            RoleSpec("eco_power", "number", False, ("eco_mode_power",), ("eco_power",)),
            RoleSpec("battery_soc", "sensor", True, ("state_of_charge",), ("soc",)),
        ),
    )


def test_resolver_matches_by_uid_suffix_and_respects_domain():
    entities = [
        _RegEntry("select.gw_operation_mode", "gw_xyz_operation_mode"),
        _RegEntry("number.gw_eco_mode_power", "gw_xyz_eco_mode_power"),
        _RegEntry("sensor.gw_state_of_charge", "gw_xyz_state_of_charge"),
        # A select with the same tail in a different domain must NOT win the
        # number role, and vice-versa.
        _RegEntry("sensor.gw_operation_mode_readback", "gw_xyz_operation_mode"),
    ]
    er, er2, dr, dr2 = _patch_registries(entities, [])
    with er, er2, dr, dr2:
        result = resolve_profile_entities(MagicMock(), "entry-1", _profile())
    assert result["operation_mode_select"] == "select.gw_operation_mode"
    assert result["eco_power"] == "number.gw_eco_mode_power"
    assert result["battery_soc"] == "sensor.gw_state_of_charge"


def test_resolver_matches_by_entity_tail_fallback():
    # No uid match; entity_id tail is the fallback.
    entities = [_RegEntry("number.x_eco_power", "")]
    er, er2, dr, dr2 = _patch_registries(entities, [])
    with er, er2, dr, dr2:
        result = resolve_profile_entities(MagicMock(), "entry-1", _profile())
    assert result["eco_power"] == "number.x_eco_power"


def test_resolver_resolves_device_role_preferring_battery():
    profile = ControlProfile(
        platform=InverterPlatform.HUAWEI_SOLAR,
        config_entry_domain="huawei_solar",
        roles=(RoleSpec("battery_device", "device", False),),
    )
    devices = [_Device("dev-inv", "Inverter"), _Device("dev-bat", "LUNA2000 Battery")]
    er, er2, dr, dr2 = _patch_registries([], devices)
    with er, er2, dr, dr2:
        result = resolve_profile_entities(MagicMock(), "entry-1", profile)
    assert result["battery_device"] == "dev-bat"


def test_resolver_omits_unmatched_roles():
    er, er2, dr, dr2 = _patch_registries([], [])
    with er, er2, dr, dr2:
        result = resolve_profile_entities(MagicMock(), "entry-1", _profile())
    assert "operation_mode_select" not in result


# --- discovery strategy ----------------------------------------------------- #


def test_discovery_auto_detect_path_uses_resolver():
    profile = profile_for(InverterPlatform.GOODWE)
    disco = ProfileEntityDiscovery(profile)
    data = {config_entry_key(InverterPlatform.GOODWE): "entry-9"}
    with patch.object(
        pp, "resolve_profile_entities", return_value={"operation_mode_select": "select.x"},
    ) as resolve:
        result = disco.resolve_role_ids(MagicMock(), data)
    resolve.assert_called_once()
    assert result["operation_mode_select"] == "select.x"


def test_discovery_manual_path_reads_generated_keys():
    platform = InverterPlatform.SOLAX
    profile = profile_for(platform)
    disco = ProfileEntityDiscovery(profile)
    data = {
        manual_role_key(platform, "use_mode_select"): "select.solax_use_mode",
        manual_role_key(platform, "export_limit"): "number.solax_export",
    }
    result = disco.resolve_role_ids(MagicMock(), data)
    assert result["use_mode_select"] == "select.solax_use_mode"
    assert result["export_limit"] == "number.solax_export"
    # Unmapped roles are dropped (empty values filtered).
    assert "manual_mode_select" not in result


def test_discovery_appends_shared_energy_counters():
    platform = InverterPlatform.DEYE
    disco = ProfileEntityDiscovery(profile_for(platform))
    data = {
        manual_role_key(platform, "work_mode_select"): "select.deye_work_mode",
        "inverter_entity_grid_import_energy": "sensor.grid_import",
    }
    result = disco.resolve_role_ids(MagicMock(), data)
    assert result["grid_import_energy_today"] == "sensor.grid_import"


# --- Driver ↔ profile role contract ----------------------------------------- #


def _build_driver(platform: InverterPlatform, factory):
    """Build a platform's real control driver over an empty entity context."""
    context = InverterContext(
        hass=MagicMock(), entity_ids={}, get_grid_power=lambda: 0.0,
    )
    return factory(context, default_battery_config(), 6000, 10000)


@pytest.mark.parametrize("platform, factory", [
    (InverterPlatform.HUAWEI_SOLAR, make_huawei_driver),
    (InverterPlatform.SOLAX, make_solax_driver),
    (InverterPlatform.SUNGROW, make_sungrow_driver),
    (InverterPlatform.GOODWE, make_goodwe_driver),
    (InverterPlatform.DEYE, make_deye_driver),
])
def test_every_role_a_driver_references_is_declared_in_its_profile(platform, factory):
    # The role contract: a renamed/typo'd role in a driver plan (or a role the
    # profile forgot to declare) must fail here, not silently no-op on hardware.
    profile = profile_for(platform)
    driver = _build_driver(platform, factory)
    missing = unbacked_roles(profile, driver.referenced_roles())
    assert not missing, (
        f"{platform.value} driver references roles absent from its profile: "
        f"{sorted(missing)}"
    )


def test_unbacked_roles_flags_a_missing_role():
    # A driver role the profile doesn't declare is reported (guards the guard).
    profile = profile_for(InverterPlatform.HUAWEI_SOLAR)
    missing = unbacked_roles(profile, {"work_mode_select", "totally_made_up_role"})
    assert missing == frozenset({"totally_made_up_role"})
