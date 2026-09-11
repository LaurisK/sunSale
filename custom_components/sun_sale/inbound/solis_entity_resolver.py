"""Auto-resolve solis_modbus entity IDs from the HA entity registry.

Roles required for the StorageMode state machine (see ``docs/solis_control.md``):

  - Telemetry sensors: SoC, battery power, grid power.
  - Register 43110 readback sensor (Storage Control word).
  - Number entities for battery currents, export limits, RC active-power setpoint.
  - Bit-switches for register 43110 bits 0/1/5/6 + the master export gate
    and the grid feed-in limit switch (regs outside 43110).

TOU slot-time entities (regs 43143/43145/43147/43149) are no longer
discovered — the new state machine does not write per-cycle TOU slots.

Unique-ID matching is *defensive* because the upstream solis_modbus
``SolisSensorGroup`` has a bug: it calls ``unique_id_generator(controller,
entity)`` with the entity *dict* instead of ``entity.get("unique")``, so
regular (non-derived) sensors end up with garbled unique_ids of the form
``solis_modbus_{serial}_{'name': '…', …, 'unique': '<suffix>', …}``. The
matcher therefore tries three patterns in order — ``endswith`` for clean
UIDs (derived sensors), a substring search for the dict-repr UIDs, and
entity_id tail matching as a final safety net — so the resolver works
regardless of which side of the upstream bug an entity falls on.
"""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..contract.const import (
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    CONF_INVERTER_SOLIS_BACKFLOW_POWER,
    CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH,
    CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    CONF_INVERTER_SOLIS_RC_GRID_ADJUSTMENT_SELECT,
    CONF_INVERTER_SOLIS_RC_SETPOINT,
    CONF_INVERTER_SOLIS_RC_TIMEOUT,
    CONF_INVERTER_SOLIS_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    DEFAULT_SOLIS_BACKFLOW_POWER,
    DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH,
    DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    DEFAULT_SOLIS_RC_GRID_ADJUSTMENT_SELECT,
    DEFAULT_SOLIS_RC_SETPOINT,
    DEFAULT_SOLIS_RC_TIMEOUT,
    DEFAULT_SOLIS_SELF_USE_SWITCH,
    DEFAULT_SOLIS_STORAGE_CONTROL_READBACK,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
)

_LOGGER = logging.getLogger(__name__)

# Each entry maps a role key to (suffix, entity_id_tail). The suffix is the
# canonical ``solis_modbus_inverter_<slug>`` token written by the integration's
# sensor data files. The entity_id tail is the slugified default object_id used
# when the entity_id wasn't overridden (matches both ``..._<tail>`` and
# ``..._<tail>_2`` to absorb the dup-suffix on second-instance entities).
_SENSOR_SUFFIXES: dict[str, tuple[str, str]] = {
    # Telemetry — read every cycle.
    "battery_soc":                 ("solis_modbus_inverter_battery_soc", "battery_soc"),
    # Battery flow magnitude (always ≥ 0, carries no direction). Kept as the
    # fall-back source; ``battery_power_signed`` below is preferred because the
    # bare magnitude would pin the panel to "charging" forever.
    "battery_power":               ("solis_modbus_inverter_battery_power", "battery_power"),
    # Signed net battery flow — the derived ``Battery Power Net`` sensor (clean
    # UID). solis_modbus's convention here is discharge-positive (negative while
    # charging), the opposite of sunSale's positive=charging, so
    # ``InverterController.get_battery_power`` flips its sign. Distinct entity
    # tail (``battery_power_net``) keeps it from colliding with ``battery_power``.
    "battery_power_signed":        ("solis_modbus_inverter_battery_power_net", "battery_power_net"),
    # Net grid flow — prefer the derived ``Grid Power Net`` sensor (clean UID,
    # already in sunSale's positive=import convention; no sign-flip needed).
    # The matcher handles its clean UID via the endswith pattern.
    "grid_power":                  ("solis_modbus_inverter_grid_power_net", "grid_power_net"),
    # Fallback for installs where the meter chain is empty / stale. AC port
    # power uses the opposite (positive=inverter→grid) sign convention, so
    # the boundary sign-flip is applied to this slot in the read path.
    "grid_power_fallback":         ("solis_modbus_inverter_ac_grid_port_power", "ac_grid_port_power"),
    # Daily-resetting energy counters — authoritative totals for the
    # ObservedGridSeries end-of-day correction (see inbound/grid.py).
    "grid_import_energy_today":    (
        "solis_modbus_inverter_today_energy_imported_from_grid",
        "today_energy_imported_from_grid",
    ),
    "grid_export_energy_today":    ("solis_modbus_inverter_today_energy_fed_into_grid", "today_energy_fed_into_grid"),
    # Yesterday-total energy counters — preferred bake-in source when the
    # inverter exposes them directly (avoids dependence on the pre-rollover
    # snapshot path). Mapped into raw_config under the keys expected by
    # ``yesterday_total_resolver.DEDICATED_ENTITY_CONFIG_KEY``.
    "grid_import_energy_yesterday": (
        "solis_modbus_inverter_yesterday_energy_imported_from_grid",
        "yesterday_energy_imported_from_grid",
    ),
    "grid_export_energy_yesterday": (
        "solis_modbus_inverter_yesterday_energy_fed_into_grid",
        "yesterday_energy_fed_into_grid",
    ),
    # Daily-resetting battery charge/discharge energy counters — feed the
    # capacity estimator's energy integral (inbound/battery.py + the anchor
    # accumulator in coordinator._build_capacity_observation).
    "battery_charge_energy_today":    (
        "solis_modbus_inverter_today_battery_charge_energy",
        "today_battery_charge_energy",
    ),
    "battery_discharge_energy_today": (
        "solis_modbus_inverter_today_battery_discharge_energy",
        "today_battery_discharge_energy",
    ),
    # PV power + daily-resetting solar-energy counter — feed the generation
    # observer (PvPowerTranslator) and bake-in (GenerationTranslator).
    "pv_power":                    ("solis_modbus_inverter_total_pv_power", "total_pv_power"),
    "solar_energy_today":          ("solis_modbus_inverter_pv_today_energy_generation", "pv_today_energy_generation"),
    "solar_energy_yesterday":      (
        "solis_modbus_inverter_pv_yesterday_energy_generation",
        "pv_yesterday_energy_generation",
    ),
    # AC grid-port signed power (positive = inverter→grid) — distinct from
    # ``grid_power_fallback`` (which the controller uses with a sign flip
    # when meter-side reads are stale): the derived observers want the raw
    # signed value as-is to balance against ``grid_power`` (positive = import).
    "ac_port_power":               ("solis_modbus_inverter_ac_grid_port_power", "ac_grid_port_power"),
    # Backup-port output power (magnitude). 0 unless the inverter is
    # bridging backup-protected loads during a grid outage.
    "backup_power":                ("solis_modbus_inverter_backup_load_power", "backup_load_power"),
    # Storage Control word readback (register 43110).
    "storage_control_readback":    (
        "solis_modbus_inverter_storage_control_switch_value",
        "storage_control_switch_value",
    ),
    # Battery max charge / discharge currents (per-slot; configured via numbers).
    "battery_max_charge_current":    ("solis_modbus_inverter_battery_max_charge_current", "battery_max_charge_current"),
    "battery_max_discharge_current": (
        "solis_modbus_inverter_battery_max_discharge_current",
        "battery_max_discharge_current",
    ),
    # Remote-Control AC active-power setpoint (register 43128, S16 signed W).
    "rc_setpoint":                 (
        "solis_modbus_inverter_rc_inverter_ac_grid_active_power",
        "rc_inverter_ac_grid_active_power",
    ),
    # RC deadman timeout in minutes (register 43282, 1..30). Refreshed every
    # beat while an RC-backed mode is held — see InverterController.refresh_rc.
    "rc_timeout":                  ("solis_modbus_inverter_rc_timeout", "rc_timeout"),
    # Remote Dispatch (holding 44100-44112) read-back view, plus the capability
    # gate at input 34502. Read-only sensors: the block itself is written by
    # ``solis_modbus``'s dispatch services, so these are purely what the verify
    # loop compares against — see ``outbound/solis_dispatch_driver.py``.
    "dispatch_capability":         (
        "solis_modbus_inverter_remote_dispatch_capability",
        "remote_dispatch_capability",
    ),
    "dispatch_active":             (
        "solis_modbus_inverter_dispatch_active", "dispatch_active",
    ),
    "dispatch_control_mode":       (
        "solis_modbus_inverter_dispatch_control_mode", "dispatch_control_mode",
    ),
    "dispatch_power_target":       (
        "solis_modbus_inverter_dispatch_power_target", "dispatch_power_target",
    ),
    "dispatch_failsafe_interval":  (
        "solis_modbus_inverter_dispatch_failsafe_interval",
        "dispatch_failsafe_interval",
    ),
    # Diagnostics only — captured by the battery-export guard when it trips.
    # 33122 is undocumented ("Reserved" in the public protocol) but tracks the
    # working mode (2 self-use, 8 feed-in, 2048 dispatch, 4096 the 2026-09-09
    # export state); 34504 says whether anything is dispatching the inverter.
    "operating_mode":              ("solis_modbus_inverter_operating_mode", "operating_mode"),
    "dispatch_running_status":     (
        "solis_modbus_inverter_remote_dispatch_running_status",
        "remote_dispatch_running_status",
    ),
    # Export-side numbers (regs 43073/43074 area).
    "backflow_power":              ("solis_modbus_inverter_backflow_power", "backflow_power"),
    "peak_max_usable_grid_power":  ("solis_modbus_inverter_peak_max_usable_grid_power", "peak_max_usable_grid_power"),
}

# Bit switches at register 43110: ``solis_modbus_{serial}_43110_{bit}``.
# Mapping table — see ``docs/solis_control.md`` §2 for the bit assignments.
_REG_43110 = 43110
_BIT_SWITCHES: dict[str, int] = {
    "self_use_switch":         0,
    "tou_mode_switch":         1,
    "allow_grid_charge_switch": 5,
    "feed_in_priority_switch": 6,
}

# RC Grid Adjustment selector (register 43132) — the enable gate for the RC
# active-power setpoint. Select entities carry a register-based unique_id
# (``solis_modbus_{serial}_43132_select``), unlike the slug-keyed sensors and
# numbers above, so it gets its own match in the resolution loop. The
# entity_id tail is a fallback; the domain guard (``select.``) keeps the
# hidden 43132 readback *sensor* (same slug) from stealing the role.
_RC_ADJUSTMENT_SELECT_ROLE = "rc_grid_adjustment_select"
_RC_ADJUSTMENT_SELECT_UID_TAIL = "_43132_select"
_RC_ADJUSTMENT_SELECT_ENTITY_TAIL = "rc_grid_adjustment"

# Switches that participate in dispatch but are not bits of 43110.
# ``(suffix, entity_id_tail)`` — same shape as ``_SENSOR_SUFFIXES``.
_SWITCH_SUFFIXES: dict[str, tuple[str, str]] = {
    "allow_export_under_self_use_switch":
        ("solis_modbus_inverter_allow_export_switch_under_self_generation_and_self_use",
         "allow_export_switch_under_self_generation_and_self_use"),
    "grid_feed_in_power_limit_switch":
        ("solis_modbus_inverter_grid_feed_in_power_limit_switch",
         "grid_feed_in_power_limit_switch"),
}


# Roles whose entity IDs must be ``number.*`` entities so that
# ``InverterController._set_number`` can call ``number.set_value``.
# The main resolution loop may pick a ``sensor.*`` readback entity first
# (both domains share the same entity_id tail); the second pass in
# ``resolve_solis_entities`` re-scans and overwrites with the number entity.
_WRITABLE_ROLES: frozenset[str] = frozenset({
    "battery_max_charge_current",
    "battery_max_discharge_current",
    "rc_setpoint",
    "rc_timeout",
    "backflow_power",
})

# Roles that ``InverterController`` handles gracefully when their entity ID
# is empty (the write is logged at debug-level and skipped — see
# ``outbound/inverter.py`` class docstring). Listed here so the resolver
# distinguishes "genuinely required for sunSale to work" from "use when
# available". Missing optional roles log at DEBUG, not WARNING.
_OPTIONAL_ROLES: frozenset[str] = frozenset({
    # Some Solis firmware versions / hybrid models don't expose these. Their
    # absence only constrains the storage-mode dispatcher's export-control
    # options; cycle telemetry + bake-in still work.
    "allow_export_under_self_use_switch",
    "grid_feed_in_power_limit_switch",
    # The AC-port fallback exists only when the user has configured it as
    # a fallback for the meter chain — most installs use ``grid_power_net``
    # alone.
    "grid_power_fallback",
    # The signed net-battery sensor is a derived solis_modbus sensor (newer
    # versions only). Its absence degrades get_battery_power to the unsigned
    # magnitude — telemetry still works, only charge/discharge direction is lost.
    "battery_power_signed",
    # Yesterday-total roles are only used by the bake-in's dedicated-sensor
    # path; the snapshot fallback covers their absence.
    "grid_import_energy_yesterday",
    "grid_export_energy_yesterday",
    "solar_energy_yesterday",
    # Derived-observer inputs — absence only disables the consumption /
    # losses observed series; the rest of the pipeline still runs.
    "ac_port_power",
    "backup_power",
    # Battery energy counters — absence only disables capacity *learning*
    # (the estimator falls back to nominal); dispatch + telemetry still work.
    "battery_charge_energy_today",
    "battery_discharge_energy_today",
    # RC function entities only exist on solis_modbus v4+. Without them the
    # RC-backed modes (GridCharge / Discharge) cannot engage — apply_mode
    # logs a WARNING at dispatch time, which is the actionable signal.
    "rc_timeout",
    _RC_ADJUSTMENT_SELECT_ROLE,
    # Remote Dispatch read-backs exist only on solis_modbus versions that
    # expose the 44100 block, and only matter on inverters that advertise the
    # capability. Their absence simply keeps the driver on the RC path —
    # ``solis_dispatch_driver.dispatch_supported`` gates on exactly this.
    "dispatch_capability",
    "dispatch_active",
    "dispatch_control_mode",
    "dispatch_power_target",
    "dispatch_failsafe_interval",
    # Diagnostic captures for the battery-export guard; nothing reads them for
    # control, so an older solis_modbus without them loses only detail.
    "operating_mode",
    "dispatch_running_status",
})


def _matches(uid: str, entity_id: str, suffix: str, entity_id_tail: str) -> bool:
    """Return True when an entry's identifiers match ``suffix``/``entity_id_tail``.

    Three independent patterns are tried — any one is enough — to absorb the
    upstream solis_modbus bug where regular-sensor unique_ids embed the full
    dict-repr instead of just the ``unique`` slug.

    Patterns:
      * ``uid.endswith(_<suffix>)`` (with optional ``_2``) — clean UIDs as
        emitted by derived sensors.
      * ``"'unique': '<suffix>'"`` substring of UID — the garbled dict-repr
        form for regular sensors. The single-quoted form is what
        ``str(dict)`` produces in CPython.
      * ``entity_id`` ends in ``_<entity_id_tail>`` (with optional ``_2``) —
        last-resort match against the integration's default object_id so the
        resolver still finds an entity if both UID forms ever drift.

    Args:
        uid: Entity registry unique_id (may be empty for orphaned entries).
        entity_id: Home Assistant entity_id (e.g. ``sensor.<slug>``).
        suffix: Canonical ``solis_modbus_inverter_<slug>`` token from the
                integration's sensor data files.
        entity_id_tail: Slugified default object_id suffix (e.g.
                ``meter_total_active_power``) used for the entity_id match.

    Returns:
        True when any pattern matches; False otherwise.
    """
    if uid:
        if uid.endswith(f"_{suffix}") or uid.endswith(f"_{suffix}_2"):
            return True
        if f"'unique': '{suffix}'" in uid:
            return True
    if entity_id and entity_id_tail:
        if entity_id.endswith(f"_{entity_id_tail}") or entity_id.endswith(f"_{entity_id_tail}_2"):
            return True
    return False


def resolve_solis_entities(hass: HomeAssistant, config_entry_id: str) -> dict[str, str]:
    """Resolve entity IDs for a solis_modbus config entry via the entity registry.

    Matches each required role to its entity_id by inspecting both the
    unique_id and the entity_id of every entry attached to the given
    config entry. See module docstring for the matching patterns.

    Args:
        hass: Home Assistant instance.
        config_entry_id: The solis_modbus config entry ID whose entities to scan.

    Returns:
        Dict mapping role keys to resolved entity IDs. Roles not found are
        omitted; the caller falls back to the manual-mapping config-flow form.
    """
    registry = er.async_get(hass)
    result: dict[str, str] = {}

    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        uid = entry.unique_id or ""
        entity_id = entry.entity_id or ""
        for role, (suffix, tail) in _SENSOR_SUFFIXES.items():
            if role in result:
                continue
            if _matches(uid, entity_id, suffix, tail):
                result[role] = entity_id
        for role, (suffix, tail) in _SWITCH_SUFFIXES.items():
            if role in result:
                continue
            if _matches(uid, entity_id, suffix, tail):
                result[role] = entity_id
        for role, bit in _BIT_SWITCHES.items():
            if role in result:
                continue
            if uid.endswith(f"_{_REG_43110}_{bit}"):
                result[role] = entity_id
        if (
            _RC_ADJUSTMENT_SELECT_ROLE not in result
            and entity_id.startswith("select.")
            and (
                uid.endswith(_RC_ADJUSTMENT_SELECT_UID_TAIL)
                or entity_id.endswith(f"_{_RC_ADJUSTMENT_SELECT_ENTITY_TAIL}")
                or entity_id.endswith(f"_{_RC_ADJUSTMENT_SELECT_ENTITY_TAIL}_2")
                or entity_id == f"select.{_RC_ADJUSTMENT_SELECT_ENTITY_TAIL}"
            )
        ):
            result[_RC_ADJUSTMENT_SELECT_ROLE] = entity_id

    # Second pass: for writable roles the first pass may have resolved a
    # sensor.* readback entity (both domains share the same entity_id tail).
    # Re-scan and override with the number.* entity so that
    # InverterController._set_number can call number.set_value successfully.
    for entry in er.async_entries_for_config_entry(registry, config_entry_id):
        uid = entry.unique_id or ""
        entity_id = entry.entity_id or ""
        if not entity_id.startswith("number."):
            continue
        for role in _WRITABLE_ROLES:
            suffix, tail = _SENSOR_SUFFIXES[role]
            if _matches(uid, entity_id, suffix, tail):
                if result.get(role, "").startswith("sensor."):
                    _LOGGER.debug(
                        "solis_modbus resolver: overriding %s with number entity %s",
                        role, entity_id,
                    )
                    result[role] = entity_id

    all_roles = (
        set(_SENSOR_SUFFIXES) | set(_SWITCH_SUFFIXES) | set(_BIT_SWITCHES)
        | {_RC_ADJUSTMENT_SELECT_ROLE}
    )
    missing = sorted(all_roles - result.keys())
    missing_required = [r for r in missing if r not in _OPTIONAL_ROLES]
    missing_optional = [r for r in missing if r in _OPTIONAL_ROLES]
    if missing_required:
        _LOGGER.warning(
            "solis_modbus auto-discovery: missing required roles: %s",
            missing_required,
        )
    if missing_optional:
        _LOGGER.debug(
            "solis_modbus auto-discovery: missing optional roles (graceful skip): %s",
            missing_optional,
        )

    return result


class SolisEntityDiscovery:
    """Role→entity-ID discovery for the Solis platform.

    Implements the ``InverterEntityDiscovery`` protocol structurally (no import
    of it — avoids a cycle back to the registry). Prefers the auto-detect path
    when a ``CONF_SOLIS_CONFIG_ENTRY_ID`` is stored, scanning the entity
    registry via :func:`resolve_solis_entities` and backfilling any telemetry
    role the scan missed from the manual config; otherwise it reads the full
    manual mapping, applying the Solis switch / number defaults.
    """

    def resolve_role_ids(self, hass: HomeAssistant, data: dict) -> dict[str, str]:
        """Build the Solis telemetry + control role → entity-ID mapping.

        Args:
            hass: Home Assistant instance, used for the solis_modbus registry scan.
            data: Merged config-entry data + options.

        Returns:
            Role-keyed entity-ID dict ready for ``InverterController``.
        """
        solis_entry_id = data.get(CONF_SOLIS_CONFIG_ENTRY_ID)
        if solis_entry_id:
            # Auto-detected path: resolve all entity IDs from the entity
            # registry, then ensure the shared telemetry roles survive even
            # when the scan missed one (config / defaults still work).
            inverter_entity_ids = resolve_solis_entities(hass, solis_entry_id)
            inverter_entity_ids.setdefault(
                "battery_soc", data.get(CONF_INVERTER_ENTITY_BATTERY_SOC, ""),
            )
            inverter_entity_ids.setdefault(
                "battery_power", data.get(CONF_INVERTER_ENTITY_BATTERY_POWER, ""),
            )
            inverter_entity_ids.setdefault(
                "grid_power", data.get(CONF_INVERTER_ENTITY_GRID_POWER, ""),
            )
            inverter_entity_ids.setdefault(
                "grid_import_energy_today",
                data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, ""),
            )
            inverter_entity_ids.setdefault(
                "grid_export_energy_today",
                data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, ""),
            )
            inverter_entity_ids.setdefault(
                "battery_charge_energy_today",
                data.get(CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY, ""),
            )
            inverter_entity_ids.setdefault(
                "battery_discharge_energy_today",
                data.get(CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY, ""),
            )
            return inverter_entity_ids

        # Manual-mapping fallback: entity IDs stored directly in config-entry data.
        return {
            "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
            "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
            "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
            "grid_import_energy_today":
                data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, ""),
            "grid_export_energy_today":
                data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, ""),
            "battery_charge_energy_today":
                data.get(CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY, ""),
            "battery_discharge_energy_today":
                data.get(CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY, ""),
            "storage_control_readback":      data.get(
                CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK,
                DEFAULT_SOLIS_STORAGE_CONTROL_READBACK,
            ),
            "battery_max_charge_current":    data.get(
                CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
                DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
            ),
            "battery_max_discharge_current": data.get(
                CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
                DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
            ),
            "rc_setpoint":                   data.get(CONF_INVERTER_SOLIS_RC_SETPOINT, DEFAULT_SOLIS_RC_SETPOINT),
            "rc_grid_adjustment_select":     data.get(
                CONF_INVERTER_SOLIS_RC_GRID_ADJUSTMENT_SELECT,
                DEFAULT_SOLIS_RC_GRID_ADJUSTMENT_SELECT,
            ),
            "rc_timeout":                    data.get(CONF_INVERTER_SOLIS_RC_TIMEOUT, DEFAULT_SOLIS_RC_TIMEOUT),
            "backflow_power":                data.get(CONF_INVERTER_SOLIS_BACKFLOW_POWER, DEFAULT_SOLIS_BACKFLOW_POWER),
            "peak_max_usable_grid_power":    data.get(
                CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
                DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
            ),
            "self_use_switch":               data.get(
                CONF_INVERTER_SOLIS_SELF_USE_SWITCH,
                DEFAULT_SOLIS_SELF_USE_SWITCH,
            ),
            "tou_mode_switch":               data.get(
                CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
                DEFAULT_SOLIS_TOU_MODE_SWITCH,
            ),
            "allow_grid_charge_switch":      data.get(
                CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
                DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
            ),
            "feed_in_priority_switch":       data.get(
                CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH,
                DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH,
            ),
            "allow_export_under_self_use_switch": data.get(
                CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
                DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
            ),
            "grid_feed_in_power_limit_switch": data.get(
                CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
                DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
            ),
        }
