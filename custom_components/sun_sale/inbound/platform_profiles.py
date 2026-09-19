"""Declarative entity-resolution profiles for the entity-driven platforms.

The Solis read-side wiring (``solis_entity_resolver.py``) is bespoke: a
hand-written role→pattern table plus a dedicated discovery strategy. Rather than
copy that five times for Huawei / SolaX / Sungrow / GoodWe / Deye — all of which
resolve the *same way* (scan a config entry's registry entries, match each role
by unique-id suffix or entity-id tail) — this module makes the resolver
**declarative**: each platform is a :class:`ControlProfile` (a list of
:class:`RoleSpec` rows), and one generic resolver + one generic discovery
strategy (:class:`ProfileEntityDiscovery`) serve them all.

A profile carries everything the read side and the config flow need:
  * which HA config-entry domain to auto-detect (``None`` ⇒ manual mapping only,
    for the YAML-modbus platforms that have no config entry);
  * the telemetry + control roles, each with the unique-id / entity-id patterns
    used for auto-detect and the HA entity domain used to build the manual form.

=========================  UNTESTED — patterns are the debug surface  ==========
The role **patterns below are best-effort** from each integration's
documentation, not from a live install. They are exactly the part that needs
confirming against hardware — everything *around* them (the scan, the manual
fallback, the config-flow routing, the driver dispatch) is generic and tested.
"Connecting and debugging" means: watch the resolver's "missing roles" log, open
the entity that didn't match, and adjust that role's ``uid_patterns`` /
``entity_tails`` here. No other code should need to change.
================================================================================
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)

from ..contract.const import (
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_POWER,
)
from ..outbound.inverter import InverterPlatform

_LOGGER = logging.getLogger(__name__)

# HA entity domain of a role's target entity. ``device`` is special — the role
# resolves to a *device id* (Huawei's battery, the target of its forcible
# services), not an entity id.
RoleKind = str  # one of: "sensor" | "select" | "number" | "switch" | "device"


@dataclass(frozen=True)
class RoleSpec:
    """One resolvable role: how to auto-detect it and how to ask for it manually.

    Attributes:
        role: The role key the driver / controller consumes (e.g.
            ``work_mode_select``).
        kind: HA entity domain of the target (``sensor`` / ``select`` /
            ``number`` / ``switch``) or ``device`` for a device-id role.
        required: Whether absence should warn (required) or stay quiet
            (optional, gracefully skipped downstream).
        uid_patterns: Unique-id suffixes to match (any one), for auto-detect.
        entity_tails: Entity-id tails to match (any one), as a fallback.
        label: Human label for the manual-mapping form / logs.
    """

    role: str
    kind: RoleKind
    required: bool
    uid_patterns: tuple[str, ...] = ()
    entity_tails: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class ControlProfile:
    """Declarative read-side profile for one entity-driven platform.

    Attributes:
        platform: The platform this profile serves.
        config_entry_domain: HA config-entry domain to auto-detect, or ``None``
            when the platform has no config entry (YAML-modbus packages) and is
            manual-mapping only.
        roles: All resolvable roles (telemetry + control).
    """

    platform: InverterPlatform
    config_entry_domain: str | None
    roles: tuple[RoleSpec, ...] = field(default_factory=tuple)

    def role(self, name: str) -> RoleSpec | None:
        """Return the :class:`RoleSpec` named ``name``, or ``None``."""
        return next((r for r in self.roles if r.role == name), None)

    def role_names(self) -> frozenset[str]:
        """Return the set of role keys this profile can resolve (the supply side).

        The counterpart to a driver's
        :meth:`..outbound.entity_control.EntityControlDriver.referenced_roles`;
        :func:`unbacked_roles` checks the demand side fits inside this.
        """
        return frozenset(r.role for r in self.roles)


# --- Generated config keys (no const.py churn per platform/role) ------------- #


def config_entry_key(platform: InverterPlatform) -> str:
    """Return the config-entry-id storage key for ``platform`` (e.g. ``huawei_solar_config_entry_id``)."""
    return f"{platform.value}_config_entry_id"


def manual_role_key(platform: InverterPlatform, role: str) -> str:
    """Return the manual-mapping storage key for one ``(platform, role)``.

    Args:
        platform: The platform.
        role: The role key.

    Returns:
        A deterministic config key both the form and the resolver agree on.
    """
    return f"inverter_{platform.value}_{role}"


# --- Profiles ---------------------------------------------------------------- #
#
# Shared telemetry roles. The energy-counter / PV roles stay in the platform-
# neutral "sources" config step (already optional there), so a profile only
# needs the three core live signals the control loop + battery reading consume.

def _telemetry_roles(
    soc_tail: str, batt_tail: str, grid_tail: str,
) -> tuple[RoleSpec, ...]:
    """Return the three core telemetry RoleSpecs with platform-specific tails.

    Args:
        soc_tail: Entity-id tail for the battery-SoC sensor.
        batt_tail: Entity-id tail for the (signed) battery-power sensor.
        grid_tail: Entity-id tail for the grid net-power sensor.

    Returns:
        ``(battery_soc, battery_power, grid_power)`` RoleSpecs.
    """
    return (
        RoleSpec("battery_soc", "sensor", True, (soc_tail,), (soc_tail,), "Battery SoC sensor"),
        RoleSpec("battery_power", "sensor", True, (batt_tail,), (batt_tail,), "Battery power sensor"),
        RoleSpec("grid_power", "sensor", True, (grid_tail,), (grid_tail,), "Grid power sensor"),
    )


_HUAWEI = ControlProfile(
    platform=InverterPlatform.HUAWEI_SOLAR,
    config_entry_domain="huawei_solar",
    roles=(
        *_telemetry_roles("state_of_capacity", "charge_discharge_power", "power_meter_active_power"),
        RoleSpec("work_mode_select", "select", True,
                 ("working_mode",), ("working_mode",), "Working mode select"),
        RoleSpec("charge_from_grid_switch", "switch", False,
                 ("charge_from_grid",), ("charge_from_grid",), "Charge-from-grid switch"),
        RoleSpec("max_feed_grid_power", "number", False,
                 ("maximum_feed_grid_power_watt", "maximum_feed_grid_power"),
                 ("maximum_feed_grid_power_watt", "maximum_feed_grid_power"),
                 "Max feed-in power (W)"),
        # Target of the forcible_charge/discharge services — a DEVICE id.
        RoleSpec("battery_device", "device", False, (), (), "Battery device"),
    ),
)

_SOLAX = ControlProfile(
    platform=InverterPlatform.SOLAX,
    config_entry_domain="solax_modbus",
    roles=(
        *_telemetry_roles("battery_capacity", "battery_power_charge", "measured_power"),
        RoleSpec("use_mode_select", "select", True,
                 ("charger_use_mode",), ("charger_use_mode",), "Charger use mode select"),
        RoleSpec("manual_mode_select", "select", True,
                 ("manual_mode_select", "manual_mode"), ("manual_mode_select", "manual_mode"),
                 "Manual mode select"),
        RoleSpec("export_limit", "number", False,
                 ("export_control_user_limit",), ("export_control_user_limit",),
                 "Export control user limit (W)"),
    ),
)

_GOODWE = ControlProfile(
    platform=InverterPlatform.GOODWE,
    config_entry_domain="goodwe",
    roles=(
        *_telemetry_roles("battery_state_of_charge", "battery_power", "active_power"),
        RoleSpec("operation_mode_select", "select", True,
                 ("operation_mode",), ("operation_mode",), "Operation mode select"),
        RoleSpec("eco_power", "number", False,
                 ("eco_mode_power",), ("eco_mode_power", "eco_power"), "Eco mode power (W)"),
        RoleSpec("export_limit", "number", False,
                 ("grid_export_limit",), ("grid_export_limit",), "Grid export limit (W)"),
    ),
)

# Sungrow (mkaiser YAML package) and Deye/Sunsynk have NO single canonical
# config-entry domain we can rely on (YAML modbus has none; Deye varies between
# solarman / sunsynk). ``config_entry_domain`` left None → manual mapping only.
# Auto-detect patterns are still listed so that, if a future config-entry
# integration is adopted, only ``config_entry_domain`` needs setting.
_SUNGROW = ControlProfile(
    platform=InverterPlatform.SUNGROW,
    config_entry_domain=None,
    roles=(
        *_telemetry_roles("battery_level", "battery_power", "export_power_raw"),
        RoleSpec("ems_mode_select", "select", True,
                 ("ems_mode_selection",), ("ems_mode_selection", "ems_mode"), "EMS mode select"),
        RoleSpec("forced_cmd_select", "select", True,
                 ("battery_forced_charge_discharge_cmd",),
                 ("battery_forced_charge_discharge_cmd",), "Forced charge/discharge cmd"),
        RoleSpec("forced_power", "number", False,
                 ("forced_charge_discharge_power",), ("forced_charge_discharge_power",),
                 "Forced charge/discharge power (W)"),
        RoleSpec("export_power", "number", False,
                 ("export_power_limit",), ("export_power_limit",), "Export power limit (W)"),
    ),
)

_DEYE = ControlProfile(
    platform=InverterPlatform.DEYE,
    config_entry_domain="solarman",
    roles=(
        *_telemetry_roles("battery_soc", "battery_power", "grid_power"),
        RoleSpec("work_mode_select", "select", True,
                 ("energy_management_model", "work_mode"),
                 ("energy_management_model", "work_mode"), "Work mode select"),
        RoleSpec("grid_charge_switch", "switch", False,
                 ("grid_charge",), ("grid_charge",), "Grid charge switch"),
        RoleSpec("max_sell_power", "number", False,
                 ("max_sell_power", "max_solar_sell_power"),
                 ("max_sell_power", "max_solar_sell_power"), "Max sell power (W)"),
    ),
)

_PROFILES: dict[InverterPlatform, ControlProfile] = {
    p.platform: p for p in (_HUAWEI, _SOLAX, _GOODWE, _SUNGROW, _DEYE)
}


def profile_for(platform: InverterPlatform) -> ControlProfile | None:
    """Return the declarative profile for ``platform``, or ``None`` if it has none."""
    return _PROFILES.get(platform)


def unbacked_roles(
    profile: ControlProfile, referenced: Iterable[str],
) -> frozenset[str]:
    """Return the referenced roles ``profile`` does not declare — the contract gap.

    The role contract between a write-side driver and its read-side profile: every
    role a driver references (``referenced``, from
    :meth:`..outbound.entity_control.EntityControlDriver.referenced_roles`) must
    be a role the profile can resolve (``profile.role_names()``). Any role in the
    result is one the driver will write but the resolver can never populate, so
    the write silently degrades to a logged skip. An empty result means the
    contract holds. Validated for every platform by the profile tests, and
    re-checked at coordinator setup so a field misconfiguration is logged.

    Args:
        profile: The platform's declarative profile (the supply side).
        referenced: The role keys the driver depends on (the demand side).

    Returns:
        The referenced roles absent from the profile; empty when the contract holds.
    """
    return frozenset(referenced) - profile.role_names()


# --- Generic resolver -------------------------------------------------------- #


def _entity_matches(uid: str, entity_id: str, spec: RoleSpec) -> bool:
    """Return whether a registry entry matches ``spec`` by uid suffix or entity tail.

    Args:
        uid: Registry unique_id (possibly empty).
        entity_id: Registry entity_id.
        spec: The role being matched.

    Returns:
        True when the unique_id ends with any ``uid_patterns`` entry or the
        entity_id ends with any ``entity_tails`` entry (with optional ``_2``
        multi-instance suffix).
    """
    if uid:
        for pat in spec.uid_patterns:
            if uid.endswith(pat) or uid.endswith(f"{pat}_2"):
                return True
    if entity_id:
        for tail in spec.entity_tails:
            if entity_id.endswith(f"_{tail}") or entity_id.endswith(f"_{tail}_2"):
                return True
    return False


def _resolve_battery_device(hass: HomeAssistant, config_entry_id: str) -> str | None:
    """Return the best-guess battery device id for a config entry, or ``None``.

    Picks the first device whose name/model hints at a battery (``battery`` /
    ``luna``), else the entry's first device. Used for Huawei's forcible-charge
    service target.

    Args:
        hass: Home Assistant instance.
        config_entry_id: The platform config entry to scan devices of.

    Returns:
        A device id, or ``None`` when the entry has no devices.
    """
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, config_entry_id)
    if not devices:
        return None
    for device in devices:
        hint = f"{device.name or ''} {device.model or ''}".lower()
        if "battery" in hint or "luna" in hint:
            return device.id
    return devices[0].id


def resolve_profile_entities(
    hass: HomeAssistant, config_entry_id: str, profile: ControlProfile,
) -> dict[str, str]:
    """Resolve a profile's roles from a config entry's registry entries.

    Scans every entity registered to ``config_entry_id`` once, matching each
    role by domain + unique-id/entity-id pattern (first match wins). Device-kind
    roles are resolved from the device registry. Logs the missing required /
    optional roles split so the "connect and debug" step has an actionable
    signal.

    Args:
        hass: Home Assistant instance.
        config_entry_id: The platform's config entry id.
        profile: The platform's :class:`ControlProfile`.

    Returns:
        Role → entity-id (or device-id) map; unresolved roles are omitted.
    """
    registry = er.async_get(hass)
    entity_roles = [r for r in profile.roles if r.kind != "device"]
    result: dict[str, str] = {}

    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        uid = entry.unique_id or ""
        entity_id = entry.entity_id or ""
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        for spec in entity_roles:
            if spec.role in result or domain != spec.kind:
                continue
            if _entity_matches(uid, entity_id, spec):
                result[spec.role] = entity_id

    for spec in profile.roles:
        if spec.kind == "device" and spec.role not in result:
            device_id = _resolve_battery_device(hass, config_entry_id)
            if device_id:
                result[spec.role] = device_id

    missing_required = sorted(
        r.role for r in profile.roles if r.required and r.role not in result
    )
    missing_optional = sorted(
        r.role for r in profile.roles if not r.required and r.role not in result
    )
    if missing_required:
        _LOGGER.warning(
            "%s auto-discovery: missing required roles %s — map them manually or "
            "fix the pattern in platform_profiles.py",
            profile.platform.value, missing_required,
        )
    if missing_optional:
        _LOGGER.debug(
            "%s auto-discovery: missing optional roles (graceful skip): %s",
            profile.platform.value, missing_optional,
        )
    return result


# --- Discovery strategy ------------------------------------------------------ #


class ProfileEntityDiscovery:
    """Generic ``InverterEntityDiscovery`` driven by a :class:`ControlProfile`.

    Auto-detects from the platform's config entry when its id is stored
    (``config_entry_key``); otherwise reads the manual mapping the config flow
    saved under ``manual_role_key``. Either way it appends the platform-neutral
    energy counters collected on the shared ``sources_energy`` page, mirroring
    :class:`..inverter_discovery.GenericEntityDiscovery`.
    """

    def __init__(self, profile: ControlProfile) -> None:
        """Store the profile this strategy resolves.

        Args:
            profile: The platform's declarative profile.
        """
        self._profile = profile

    def resolve_role_ids(self, hass: HomeAssistant, data: dict) -> dict[str, str]:
        """Return the role→entity-id map for ``InverterController`` + the driver.

        Args:
            hass: Home Assistant instance (for the registry scan).
            data: Merged config-entry data + options.

        Returns:
            Role-keyed entity/device-id map.
        """
        entry_id = data.get(config_entry_key(self._profile.platform))
        if entry_id:
            resolved = resolve_profile_entities(hass, entry_id, self._profile)
            # Backfill core telemetry the scan missed from any manual config.
            resolved.setdefault("battery_soc", data.get(CONF_INVERTER_ENTITY_BATTERY_SOC, ""))
            resolved.setdefault("battery_power", data.get(CONF_INVERTER_ENTITY_BATTERY_POWER, ""))
            resolved.setdefault("grid_power", data.get(CONF_INVERTER_ENTITY_GRID_POWER, ""))
        else:
            resolved = {
                spec.role: data.get(manual_role_key(self._profile.platform, spec.role), "")
                for spec in self._profile.roles
            }
            resolved = {k: v for k, v in resolved.items() if v}

        # Platform-neutral energy counters from the shared sources step.
        for role, conf in (
            ("grid_import_energy_today", CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY),
            ("grid_export_energy_today", CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY),
            ("battery_charge_energy_today", CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY),
            ("battery_discharge_energy_today", CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY),
        ):
            value = data.get(conf, "")
            if value:
                resolved.setdefault(role, value)
        return resolved
