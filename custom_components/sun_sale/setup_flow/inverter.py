"""Inverter-and-battery forms: platform, battery, entity mapping and energy counters."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from ..contract.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_BMS_BATTERY_POWER,
    CONF_BMS_BATTERY_SOC,
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER,
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GENERATION_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    CONF_INVERTER_EXPORT_LIMIT_KW,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_INVERTER_PLATFORM,
    CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    CONF_INVERTER_SOLIS_BACKFLOW_POWER,
    CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH,
    CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    CONF_INVERTER_SOLIS_RC_SETPOINT,
    CONF_INVERTER_SOLIS_SELF_USE_SWITCH,
    CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK,
    CONF_INVERTER_SOLIS_TOU_MODE_SWITCH,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_BATTERY_RATED_CYCLE_LIFE,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY,
    DEFAULT_INVERTER_EXPORT_LIMIT_KW,
    DEFAULT_INVERTER_MAX_POWER_KW,
    DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
    DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH,
    DEFAULT_SOLIS_BACKFLOW_POWER,
    DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT,
    DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
    DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH,
    DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
    DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER,
    DEFAULT_SOLIS_RC_SETPOINT,
    DEFAULT_SOLIS_SELF_USE_SWITCH,
    DEFAULT_SOLIS_STORAGE_CONTROL_READBACK,
    DEFAULT_SOLIS_TOU_MODE_SWITCH,
)
from ..inbound.inverter_sources import SPEC_BY_KEY, DetectedSource, SourceOptions
from ..inbound.platform_profiles import ControlProfile, config_entry_key, manual_role_key
from ..outbound.inverter import InverterPlatform
from .fields import (
    ANY_ENTITY,
    BATTERY_DEVICE,
    DEVICE,
    NONE,
    NUMBER,
    SELECT,
    SENSOR,
    SENSOR_ENERGY,
    SENSOR_POWER,
    SENSOR_SOC,
    SWITCH,
    detected_selector,
    list_selector,
    missing_required,
    opt,
    req,
    suggest,
)

# Platforms wired end-to-end: a control driver + a registered entity-resolution
# profile (auto-detect from the vendor config entry, or manual mapping). Solis
# has its bespoke path; Huawei / SolaX / Sungrow / GoodWe / Deye go through the
# generic profile step (untested against hardware — patterns may need a tweak on
# first connect, but the flow is complete). Fronius / SMA / Kostal stay gated
# (notes-only drivers).
PROFILE_PLATFORMS = {
    InverterPlatform.HUAWEI_SOLAR.value,
    InverterPlatform.SOLAX.value,
    InverterPlatform.SUNGROW.value,
    InverterPlatform.GOODWE.value,
    InverterPlatform.DEYE.value,
}

# Only platforms with a control driver AND a wired entity-resolution path are
# selectable: Solis and the profile platforms above. The others are listed so
# the supported-hardware roadmap is visible but are rejected on submit — HA's
# SelectSelector has no per-option disable, so the guard in
# ``async_step_inverter_platform`` is what makes them effectively unselectable.
IMPLEMENTED_INVERTER_PLATFORMS = {InverterPlatform.SOLIS.value} | PROFILE_PLATFORMS

INVERTER_PLATFORMS = [
    {"value": InverterPlatform.SOLIS.value, "label": "Solis (solis_modbus)"},
    {"value": InverterPlatform.HUAWEI_SOLAR.value, "label": "Huawei Solar (untested — verify on first connect)"},
    {"value": InverterPlatform.SOLAX.value, "label": "SolaX (untested — verify on first connect)"},
    {"value": InverterPlatform.SUNGROW.value, "label": "Sungrow SHx (manual mapping — untested)"},
    {"value": InverterPlatform.GOODWE.value, "label": "GoodWe (untested — verify on first connect)"},
    {"value": InverterPlatform.DEYE.value, "label": "Deye / Sunsynk (untested — verify on first connect)"},
    {"value": InverterPlatform.FRONIUS.value, "label": "Fronius Gen24 (notes only — not implemented)"},
    {"value": InverterPlatform.SMA.value, "label": "SMA Smart Energy (notes only — not implemented)"},
    {"value": InverterPlatform.KOSTAL.value, "label": "Kostal Plenticore (notes only — not implemented)"},
    {"value": InverterPlatform.SOLAREDGE.value, "label": "SolarEdge (not yet supported)"},
    {"value": InverterPlatform.GENERIC.value, "label": "Generic (not yet supported)"},
]

# Maps a profile RoleSpec.kind to the entity/device selector used in the
# generated manual-mapping form for the entity-driven platforms.
_PROFILE_KIND_SELECTORS = {
    "sensor": SENSOR,
    "select": SELECT,
    "number": NUMBER,
    "switch": SWITCH,
    "device": DEVICE,
}

# The four entities the plain manual mapping (non-Solis, non-profile) needs.
INVERTER_ENTITY_KEYS = (
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
)

# The optional inverter sources, split over the pages that collect them:
# instantaneous readings on "Inverter power sensors", running totals on "Energy
# counters". Both are optional throughout — every one of these only sharpens a
# chart or a correction, and the pipeline degrades to its own estimate without.
SOURCE_POWER_KEYS = (
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_EXPORT_POWER,
)
# The dedicated battery BMS has its own page: it is separate hardware, picked as
# a device (see ``inbound/inverter_sources.py``). SoC is what puts the BMS in
# front of the inverter at all, so pack power alone is not a usable BMS.
SOURCE_BMS_KEYS = (
    CONF_BMS_BATTERY_SOC,
    CONF_BMS_BATTERY_POWER,
)
SOURCE_ENERGY_KEYS = (
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY,
    CONF_INVERTER_ENTITY_GENERATION_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY,
)

# The picker each source falls back to when nothing on the inverter matched it.
# ``suggested_value`` rather than a default, so a source the user clears really
# does clear (a defaulted optional field is re-filled by HA when submitted empty).
_SOURCE_FALLBACKS = {
    CONF_INVERTER_ENTITY_PV_POWER: SENSOR_POWER,
    CONF_INVERTER_ENTITY_AC_PORT_POWER: SENSOR_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER: SENSOR_POWER,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER: SENSOR_POWER,
    CONF_INVERTER_ENTITY_GRID_EXPORT_POWER: SENSOR_POWER,
    CONF_BMS_BATTERY_SOC: SENSOR,
    CONF_BMS_BATTERY_POWER: SENSOR_POWER,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_GENERATION_YESTERDAY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY: SENSOR_ENERGY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY: SENSOR_ENERGY,
}

# Form-only field: the battery device whose sensors the BMS page resolves. It
# is never stored — ``device_of`` recovers it from the stored SoC sensor, so
# there is no second copy of "which device" to drift.
BMS_DEVICE = "bms_device"

_SOURCE_OTHER_LABEL = "Other — pick any sensor on the next page"
_SOURCE_NONE_LABEL = "Not set"


def platform_label(platform: str) -> str:
    """Return the short display name of an inverter platform value."""
    label = next((o["label"] for o in INVERTER_PLATFORMS if o["value"] == platform), platform)
    return label.split(" (")[0]


def inverter_platform_schema(d: dict) -> vol.Schema:
    """Build the inverter platform + power-rating schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema for selecting the inverter integration platform and its rated
        AC output / export-power cap (kW). The power ratings drive the
        force-discharge RC setpoint and the backflow/export limit, so they must
        match the actual hardware rather than assume 10 kW.
    """
    return vol.Schema({
        req(CONF_INVERTER_PLATFORM, d, InverterPlatform.SOLIS.value): list_selector(INVERTER_PLATFORMS),
        req(CONF_INVERTER_MAX_POWER_KW, d, DEFAULT_INVERTER_MAX_POWER_KW): vol.Coerce(float),
        req(CONF_INVERTER_EXPORT_LIMIT_KW, d, DEFAULT_INVERTER_EXPORT_LIMIT_KW): vol.Coerce(float),
    })


def validate_inverter_power(user_input: dict) -> dict[str, str]:
    """Return field-keyed errors for the inverter power-rating inputs.

    Args:
        user_input: Submitted inverter-step values.

    Returns:
        Mapping of field key to error code; empty when all values are valid.
        Missing fields are treated as valid (the coordinator supplies defaults).
    """
    errors: dict[str, str] = {}
    max_power = user_input.get(CONF_INVERTER_MAX_POWER_KW)
    if max_power is not None and max_power <= 0:
        errors[CONF_INVERTER_MAX_POWER_KW] = "invalid_inverter_power"
    export_limit = user_input.get(CONF_INVERTER_EXPORT_LIMIT_KW)
    if export_limit is not None and export_limit < 0:
        errors[CONF_INVERTER_EXPORT_LIMIT_KW] = "invalid_export_limit"
    return errors


def battery_schema(d: dict) -> vol.Schema:
    """Build the voluptuous battery parameters schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering capacity, price, cycle life, charge/discharge power, SoC limits,
        round-trip efficiency, and nominal voltage.
    """
    return vol.Schema({
        req(CONF_BATTERY_NOMINAL_CAPACITY, d): vol.Coerce(float),
        req(CONF_BATTERY_PURCHASE_PRICE, d): vol.Coerce(float),
        req(CONF_BATTERY_RATED_CYCLE_LIFE, d, DEFAULT_BATTERY_RATED_CYCLE_LIFE): vol.Coerce(int),
        req(CONF_BATTERY_MAX_CHARGE_POWER, d, 5.0): vol.Coerce(float),
        req(CONF_BATTERY_MAX_DISCHARGE_POWER, d, 5.0): vol.Coerce(float),
        req(CONF_BATTERY_MIN_SOC, d, DEFAULT_BATTERY_MIN_SOC): vol.Coerce(float),
        req(CONF_BATTERY_MAX_SOC, d, DEFAULT_BATTERY_MAX_SOC): vol.Coerce(float),
        req(CONF_BATTERY_ROUND_TRIP_EFFICIENCY, d, DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY): vol.Coerce(float),
        req(CONF_BATTERY_NOMINAL_VOLTAGE, d, DEFAULT_BATTERY_NOMINAL_VOLTAGE): vol.Coerce(float),
    })


def validate_battery(user_input: dict) -> dict[str, str]:
    """Return field-keyed errors for the battery form.

    Args:
        user_input: Submitted battery-step values.

    Returns:
        Mapping of field key to error code; empty when every value is usable.
    """
    errors = missing_required(user_input, (CONF_BATTERY_NOMINAL_CAPACITY, CONF_BATTERY_PURCHASE_PRICE))
    for key, code in (
        (CONF_BATTERY_NOMINAL_CAPACITY, "invalid_capacity"),
        (CONF_BATTERY_PURCHASE_PRICE, "invalid_price"),
        (CONF_BATTERY_RATED_CYCLE_LIFE, "invalid_cycle_life"),
        (CONF_BATTERY_MAX_CHARGE_POWER, "invalid_charge_power"),
        (CONF_BATTERY_MAX_DISCHARGE_POWER, "invalid_discharge_power"),
        (CONF_BATTERY_NOMINAL_VOLTAGE, "invalid_voltage"),
    ):
        if key in user_input and user_input[key] <= 0:
            errors[key] = code
    min_soc = user_input.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)
    max_soc = user_input.get(CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)
    if not 0.0 <= min_soc < 100.0:
        errors[CONF_BATTERY_MIN_SOC] = "invalid_min_soc"
    if not 0.0 < max_soc <= 100.0:
        errors[CONF_BATTERY_MAX_SOC] = "invalid_max_soc"
    if not errors.keys() & {CONF_BATTERY_MIN_SOC, CONF_BATTERY_MAX_SOC} and min_soc >= max_soc:
        errors[CONF_BATTERY_MAX_SOC] = "soc_range_inverted"
    efficiency = user_input.get(CONF_BATTERY_ROUND_TRIP_EFFICIENCY, DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY)
    if not 0.0 < efficiency <= 100.0:
        errors[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] = "invalid_efficiency"
    return errors


def inverter_entities_schema(d: dict) -> vol.Schema:
    """Build the generic inverter entity mapping schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering SoC, battery power, grid power, and charge control entity selectors.
    """
    return vol.Schema({
        req(CONF_INVERTER_ENTITY_BATTERY_SOC, d): SENSOR_SOC,
        req(CONF_INVERTER_ENTITY_BATTERY_POWER, d): SENSOR_POWER,
        req(CONF_INVERTER_ENTITY_GRID_POWER, d): SENSOR_POWER,
        req(CONF_INVERTER_ENTITY_CHARGE_CONTROL, d): ANY_ENTITY,
    })


def inverter_solis_schema(d: dict) -> vol.Schema:
    """Build the Solis register-level entity mapping schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering all entities required by the StorageMode state
        machine: telemetry sensors, the register-43110 readback, battery
        current numbers, RC setpoint, export-limit numbers, and the bit
        switches that compose register 43110 + the export-master switches.
    """
    return vol.Schema({
        req(CONF_INVERTER_ENTITY_BATTERY_SOC, d, "sensor.solis_battery_soc"): SENSOR_SOC,
        req(CONF_INVERTER_ENTITY_BATTERY_POWER, d, "sensor.solis_battery_power"): SENSOR_POWER,
        req(CONF_INVERTER_ENTITY_GRID_POWER, d, "sensor.solis_grid_power_net"): SENSOR_POWER,
        req(CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK, d, DEFAULT_SOLIS_STORAGE_CONTROL_READBACK): SENSOR,
        req(CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT, d, DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT): NUMBER,
        req(
            CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
            d,
            DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
        ): NUMBER,
        req(CONF_INVERTER_SOLIS_RC_SETPOINT, d, DEFAULT_SOLIS_RC_SETPOINT): NUMBER,
        req(CONF_INVERTER_SOLIS_BACKFLOW_POWER, d, DEFAULT_SOLIS_BACKFLOW_POWER): NUMBER,
        req(CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER, d, DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER): NUMBER,
        req(CONF_INVERTER_SOLIS_SELF_USE_SWITCH, d, DEFAULT_SOLIS_SELF_USE_SWITCH): SWITCH,
        req(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, d, DEFAULT_SOLIS_TOU_MODE_SWITCH): SWITCH,
        req(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, d, DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH): SWITCH,
        req(CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH, d, DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH): SWITCH,
        req(
            CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
            d,
            DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
        ): SWITCH,
        req(
            CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
            d,
            DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
        ): SWITCH,
    })


def inverter_solis_pick_schema(options: list[dict], d: dict) -> vol.Schema:
    """Build a single-field schema for selecting which solis_modbus config entry to use.

    Args:
        options: List of ``{"value": entry_id, "label": title}`` dicts from discovered entries.
        d: Existing config/options dict used for the default value.

    Returns:
        Schema with one SelectSelector field for the solis_modbus config entry ID.
    """
    return vol.Schema({req(CONF_SOLIS_CONFIG_ENTRY_ID, d, options[0]["value"]): list_selector(options)})


def profile_manual_schema(profile: ControlProfile, d: dict) -> vol.Schema:
    """Build the manual entity-mapping schema for an entity-driven platform.

    Generates one selector per profile role (sensor / select / number / switch /
    device), keyed by the deterministic ``manual_role_key`` so the resolver finds
    the same value. Required roles use ``req``; optional roles use ``opt``.

    Args:
        profile: The platform's control profile.
        d: Existing config/options dict for default values.

    Returns:
        A voluptuous schema covering every role the driver consumes.
    """
    fields: dict = {}
    for spec in profile.roles:
        key = manual_role_key(profile.platform, spec.role)
        selector = _PROFILE_KIND_SELECTORS[spec.kind]
        marker = req(key, d) if spec.required else opt(key, d)
        fields[marker] = selector
    return vol.Schema(fields)


def profile_pick_schema(profile: ControlProfile, options: list[dict], d: dict) -> vol.Schema:
    """Build the "which vendor config entry" picker schema for a profile platform.

    Args:
        profile: The platform's control profile.
        options: ``{"value": entry_id, "label": title}`` dicts for each entry.
        d: Existing config/options dict for the default selection.

    Returns:
        A single-field schema selecting the vendor config entry id.
    """
    return vol.Schema({req(config_entry_key(profile.platform), d, options[0]["value"]): list_selector(options)})


def source_label(source: DetectedSource) -> str:
    """Return a detected source's row in a source picker.

    Args:
        source: The detected candidate.

    Returns:
        ``"<name> (<entity id>)"`` — or just the entity id when the registry
        carries no name — marked when the platform itself resolved it, or when
        its name says it mirrors a control register rather than measuring.
    """
    label = f"{source.name} ({source.entity_id})" if source.name else source.entity_id
    if source.auto:
        return f"{label} — detected"
    return f"{label} — a limit or setpoint, not a meter" if source.setpoint else label


def source_rows(key: str, d: dict, options: SourceOptions) -> tuple[list[dict[str, str]], str]:
    """Return a detected source's picker rows and the one to pre-select.

    A sensor the user picked by hand is usually *not* on the inverter's device —
    that is why they picked it by hand — so it gets a row of its own and keeps
    the pre-selection. Without that, re-opening the page would show the detected
    guess and confirming would quietly overwrite their choice.

    Args:
        key: The source's config key.
        d: Existing config/options dict.
        options: The source's detected candidates.

    Returns:
        ``(rows, default)`` for :func:`detected_selector`.
    """
    rows = [{"value": source.entity_id, "label": source_label(source)} for source in options.candidates]
    stored = str(d.get(key) or "")
    if stored and stored not in {source.entity_id for source in options.candidates}:
        rows.append({"value": stored, "label": f"{stored} (picked by hand)"})
        return rows, stored
    return rows, options.default


def _source_field(key: str, d: dict, detected: dict[str, SourceOptions]) -> tuple[vol.Marker, Any]:
    """Return one source row: a list of what was found, or a plain entity picker.

    Args:
        key: The source's config key.
        d: Existing config/options dict used for the pre-filled value.
        detected: Candidates per config key from ``detect_inverter_sources``.

    Returns:
        ``(marker, selector)`` for the schema. With candidates the row is a
        dropdown of them plus *Other* and *Not set*, pre-selected to the best
        guess; without, it degrades to the plain picker over every matching
        sensor — the same fallback the price feed uses when it detects nothing.
    """
    options = detected.get(key)
    if options is None:
        return vol.Optional(key, description=suggest(d.get(key))), _SOURCE_FALLBACKS[key]
    rows, default = source_rows(key, d, options)
    selector = detected_selector(rows, other=_SOURCE_OTHER_LABEL, none=_SOURCE_NONE_LABEL, dropdown=True)
    # A list always submits one of its rows, so "nothing chosen" needs the
    # explicit None row as the default rather than an absent value.
    return vol.Required(key, default=default or NONE), selector


def sources_schema(keys: tuple[str, ...], d: dict, detected: dict[str, SourceOptions]) -> vol.Schema:
    """Build one optional-sources page, offering what was found on the inverter.

    Args:
        keys: The page's source keys, in display order (``SOURCE_POWER_KEYS``
            or ``SOURCE_ENERGY_KEYS``).
        d: Existing config/options dict used for default values.
        detected: Candidates per config key from ``detect_inverter_sources``.

    Returns:
        Schema with one row per source. Every row is optional: household
        baseload is derived from the AC-port / backup / grid composition, and a
        dedicated BMS (``CONF_BMS_*``) is preferred over the inverter per field
        only where it is set — see ``inbound/battery_source.py``.
    """
    return vol.Schema(dict(_source_field(key, d, detected) for key in keys))


def bms_device_schema(device_id: str) -> vol.Schema:
    """Build the BMS page's first form: which battery device the pack reports through.

    Args:
        device_id: The device to pre-fill — recovered from the stored SoC
            sensor, so an already-configured BMS opens on its own device.

    Returns:
        Schema with one device picker, limited to devices that publish a
        battery-class sensor. It is optional, and confirming it empty is how a
        BMS is taken away again: the inverter is then the only battery source.
    """
    return vol.Schema({vol.Optional(BMS_DEVICE, description=suggest(device_id)): BATTERY_DEVICE})


def sources_manual_schema(keys: list[str], d: dict) -> vol.Schema:
    """Build the by-hand form for the sources whose row chose *Other*.

    Args:
        keys: The config keys to ask for, in the page's display order.
        d: Existing config/options dict used for the pre-filled values.

    Returns:
        Schema with a plain entity picker per key, over every sensor of the
        right kind rather than only the inverter's own.
    """
    return vol.Schema({
        vol.Optional(key, description=suggest(d.get(key))): _SOURCE_FALLBACKS[key] for key in keys
    })


def sources_summary(keys: tuple[str, ...], detected: dict[str, SourceOptions]) -> str:
    """Return a sources page's sentence about what was found on the inverter.

    Counted over the page's *detectable* keys only. The BMS pair is deliberately
    absent from ``SOURCE_SPECS`` — a battery's own sensors do not live on the
    inverter's device — so counting it would make detection look worse than it is.

    Args:
        keys: The page's source keys.
        detected: Candidates per config key from ``detect_inverter_sources``.

    Returns:
        What detection managed for this page, phrased for the form description.
    """
    detectable = [key for key in keys if key in SPEC_BY_KEY]
    found = sum(1 for key in detectable if key in detected)
    if not found:
        return (
            "No sensors could be read off the inverter, so each source below lists "
            "every matching sensor on this system."
        )
    filled = sum(1 for key in detectable if (options := detected.get(key)) and options.default)
    return (
        f"Found candidates for {found} of {len(detectable)} sources on your inverter, "
        f"{filled} already filled in — check them and correct any that look wrong."
    )
