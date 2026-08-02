"""Multi-step config flow for sunSale."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .contract.const import (
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
    CONF_CURRENCY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
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
    CONF_NORDPOOL_ENTITY,
    CONF_NORDPOOL_RESOLUTION,
    CONF_PANEL_TITLE,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_SOURCE,
    CONF_PRICE_TOU_BANDS,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_SOLIS_CONFIG_ENTRY_ID,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_FIXED_SELL_PRICE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_MODE,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CONF_TARIFF_WEEKDAY_BANDS,
    CONF_TARIFF_WEEKEND_BANDS,
    DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_BATTERY_RATED_CYCLE_LIFE,
    DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY,
    DEFAULT_CURRENCY,
    DEFAULT_INVERTER_EXPORT_LIMIT_KW,
    DEFAULT_INVERTER_MAX_POWER_KW,
    DEFAULT_NORDPOOL_RESOLUTION,
    DEFAULT_PRICE_SOURCE,
    DEFAULT_SELL_MODE,
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
    DOMAIN,
    MAX_TARIFF_BANDS,
    PRICE_SOURCE_TOU,
)
from .inbound.forecast_resolver import discover_forecast_entries
from .inbound.platform_profiles import (
    ControlProfile,
    config_entry_key,
    manual_role_key,
    profile_for,
)
from .outbound.inverter import InverterPlatform

# Platforms wired end-to-end: a control driver + a registered entity-resolution
# profile (auto-detect from the vendor config entry, or manual mapping). Solis
# has its bespoke path; Huawei / SolaX / Sungrow / GoodWe / Deye go through the
# generic profile step (untested against hardware — patterns may need a tweak on
# first connect, but the flow is complete). Fronius / SMA / Kostal stay gated
# (notes-only drivers).
_PROFILE_PLATFORMS = {
    InverterPlatform.HUAWEI_SOLAR.value,
    InverterPlatform.SOLAX.value,
    InverterPlatform.SUNGROW.value,
    InverterPlatform.GOODWE.value,
    InverterPlatform.DEYE.value,
}

# Only platforms with a real control-driver implementation AND a wired
# entity-resolution path are selectable. Solis is the only one that satisfies
# both today: Huawei / SolaX / Sungrow / GoodWe / Deye have control drivers
# (``outbound/*_driver.py``, entity-driven, untested against hardware) but no
# per-platform entity resolver or manual-mapping form yet, so they stay gated.
# The others are listed so the supported-hardware roadmap is visible but are
# rejected on submit — HA's SelectSelector has no per-option disable, so the
# guard in ``async_step_inverter`` is what makes them effectively unselectable.
IMPLEMENTED_INVERTER_PLATFORMS = {InverterPlatform.SOLIS.value} | _PROFILE_PLATFORMS

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
_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor"))
_SENSOR_POWER = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
_SENSOR_SOC = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="battery"))
_SENSOR_ENERGY = EntitySelector(EntitySelectorConfig(domain="sensor", device_class=["energy", "power"]))
_SWITCH = EntitySelector(EntitySelectorConfig(domain="switch"))
_NUMBER = EntitySelector(EntitySelectorConfig(domain="number"))
_SELECT = EntitySelector(EntitySelectorConfig(domain="select"))
_TIME = EntitySelector(EntitySelectorConfig(domain="time"))
_ANY_ENTITY = EntitySelector(EntitySelectorConfig())
_DEVICE = DeviceSelector(DeviceSelectorConfig())

# Maps a profile RoleSpec.kind to the entity/device selector used in the
# generated manual-mapping form for the entity-driven platforms.
_PROFILE_KIND_SELECTORS = {
    "sensor": _SENSOR,
    "select": _SELECT,
    "number": _NUMBER,
    "switch": _SWITCH,
    "device": _DEVICE,
}


def _platform_selector(options: list[dict[str, str]]) -> SelectSelector:
    """Build a list-mode SelectSelector from a list of option dicts.

    Args:
        options: List of ``{"value": ..., "label": ...}`` dicts.

    Returns:
        Configured SelectSelector widget.
    """
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))


def _req(key: str, d: dict, fallback: Any = vol.UNDEFINED) -> vol.Required:
    """Return vol.Required with a default only when a value is available."""
    v = d.get(key, fallback)
    return vol.Required(key, default=v) if v is not vol.UNDEFINED else vol.Required(key)


def _opt(key: str, d: dict, fallback: Any = vol.UNDEFINED) -> vol.Optional:
    """Return vol.Optional with a default only when a value is available."""
    v = d.get(key, fallback)
    return vol.Optional(key, default=v) if v is not vol.UNDEFINED else vol.Optional(key)


# ---------------------------------------------------------------------------
# Schema builders (shared between config flow and options flow)
# ---------------------------------------------------------------------------

def _tariff_schema(d: dict) -> vol.Schema:
    """Build the voluptuous tariff parameters schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering distribution fee, tax rate, markup, sell-side
        equivalents, and the display currency code.
    """
    return vol.Schema({
        _req(CONF_CURRENCY, d, DEFAULT_CURRENCY): str,
        _req(CONF_TARIFF_DISTRIBUTION_FEE, d, 0.03): vol.Coerce(float),
        _req(CONF_TARIFF_TAX_RATE, d, 21.0): vol.Coerce(float),
        _req(CONF_TARIFF_MARKUP, d, 0.005): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_DISTRIBUTION_FEE, d, 0.01): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_TAX_RATE, d, 0.0): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_MARKUP, d, 0.0): vol.Coerce(float),
        _req(CONF_TARIFF_SELL_MODE, d, DEFAULT_SELL_MODE): _SELL_MODE_SELECTOR,
        _req(CONF_TARIFF_FIXED_SELL_PRICE, d, 0.0): vol.Coerce(float),
    })


def _band_field_keys(i: int) -> tuple[str, str, str]:
    """Return the (start, buy_fee, sell_fee) form field keys for band row i."""
    return f"band{i}_start", f"band{i}_buy_fee", f"band{i}_sell_fee"


def _tariff_bands_schema(d: dict, conf_key: str) -> vol.Schema:
    """Build a MAX_TARIFF_BANDS-row TOU distribution-band schema.

    Each row is a start time ("HH:MM", 24h, local) plus buy/sell distribution
    fees. Rows are pre-filled from the stored band list under ``conf_key``;
    leaving a row's start blank drops it.

    Args:
        d: Existing config/options dict used for default values.
        conf_key: CONF key (weekday/weekend) whose stored band list pre-fills rows.

    Returns:
        Schema with MAX_TARIFF_BANDS × (start, buy fee, sell fee) optional fields.
    """
    existing = d.get(conf_key) or []
    fields: dict = {}
    for i in range(MAX_TARIFF_BANDS):
        start_k, buy_k, sell_k = _band_field_keys(i)
        row = existing[i] if i < len(existing) and isinstance(existing[i], dict) else {}
        fields[vol.Optional(start_k, default=row.get("start", ""))] = str
        fields[vol.Optional(buy_k, default=row.get("buy_fee", 0.0))] = vol.Coerce(float)
        fields[vol.Optional(sell_k, default=row.get("sell_fee", 0.0))] = vol.Coerce(float)
    return vol.Schema(fields)


def _valid_hhmm(value: str) -> bool:
    """Return True when value is a valid 24h "HH:MM" time-of-day string."""
    if ":" not in value:
        return False
    hh, _, mm = value.partition(":")
    if not (hh.isdigit() and mm.isdigit()):
        return False
    return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59


def _validate_bands(user_input: dict) -> tuple[list[dict], dict[str, str]]:
    """Parse a TOU band form into stored band dicts, collecting errors.

    Rows with a blank start are skipped. A non-blank but malformed start, or a
    negative fee, yields a per-field error keyed by that row's start field.

    Args:
        user_input: Submitted band-form values.

    Returns:
        (bands, errors) where bands is the list of valid non-blank rows sorted
        by start time, and errors maps field key → error code.
    """
    bands: list[dict] = []
    errors: dict[str, str] = {}
    for i in range(MAX_TARIFF_BANDS):
        start_k, buy_k, sell_k = _band_field_keys(i)
        start = str(user_input.get(start_k, "")).strip()
        if not start:
            continue
        if not _valid_hhmm(start):
            errors[start_k] = "invalid_band_time"
            continue
        buy = float(user_input.get(buy_k, 0.0))
        sell = float(user_input.get(sell_k, 0.0))
        if buy < 0 or sell < 0:
            errors[start_k] = "negative_fee"
            continue
        # Normalise to zero-padded HH:MM so band selection sorts lexically too.
        hh, _, mm = start.partition(":")
        bands.append({
            "start": f"{int(hh):02d}:{int(mm):02d}",
            "buy_fee": buy,
            "sell_fee": sell,
        })
    bands.sort(key=lambda b: b["start"])
    return bands, errors


def _battery_schema(d: dict) -> vol.Schema:
    """Build the voluptuous battery parameters schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering capacity, price, cycle life, charge/discharge power, SoC limits,
        round-trip efficiency, and nominal voltage.
    """
    return vol.Schema({
        _req(CONF_BATTERY_NOMINAL_CAPACITY, d): vol.Coerce(float),
        _req(CONF_BATTERY_PURCHASE_PRICE, d): vol.Coerce(float),
        _req(CONF_BATTERY_RATED_CYCLE_LIFE, d, DEFAULT_BATTERY_RATED_CYCLE_LIFE): vol.Coerce(int),
        _req(CONF_BATTERY_MAX_CHARGE_POWER, d, 5.0): vol.Coerce(float),
        _req(CONF_BATTERY_MAX_DISCHARGE_POWER, d, 5.0): vol.Coerce(float),
        _req(CONF_BATTERY_MIN_SOC, d, DEFAULT_BATTERY_MIN_SOC): vol.Coerce(float),
        _req(CONF_BATTERY_MAX_SOC, d, DEFAULT_BATTERY_MAX_SOC): vol.Coerce(float),
        _req(CONF_BATTERY_ROUND_TRIP_EFFICIENCY, d, DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY): vol.Coerce(float),
        _req(CONF_BATTERY_NOMINAL_VOLTAGE, d, DEFAULT_BATTERY_NOMINAL_VOLTAGE): vol.Coerce(float),
    })


def _inverter_platform_schema(d: dict) -> vol.Schema:
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
        _req(CONF_INVERTER_PLATFORM, d, InverterPlatform.SOLIS.value): _platform_selector(INVERTER_PLATFORMS),
        _req(CONF_INVERTER_MAX_POWER_KW, d, DEFAULT_INVERTER_MAX_POWER_KW): vol.Coerce(float),
        _req(CONF_INVERTER_EXPORT_LIMIT_KW, d, DEFAULT_INVERTER_EXPORT_LIMIT_KW): vol.Coerce(float),
    })


def _validate_inverter_power(user_input: dict) -> dict[str, str]:
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


def _inverter_entities_schema(d: dict) -> vol.Schema:
    """Build the generic inverter entity mapping schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering SoC, battery power, grid power, and charge control entity selectors.
    """
    return vol.Schema({
        _req(CONF_INVERTER_ENTITY_BATTERY_SOC, d): _SENSOR_SOC,
        _req(CONF_INVERTER_ENTITY_BATTERY_POWER, d): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_GRID_POWER, d): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_CHARGE_CONTROL, d): _ANY_ENTITY,
    })


def _inverter_solis_schema(d: dict) -> vol.Schema:
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
        _req(CONF_INVERTER_ENTITY_BATTERY_SOC, d, "sensor.solis_battery_soc"): _SENSOR_SOC,
        _req(CONF_INVERTER_ENTITY_BATTERY_POWER, d, "sensor.solis_battery_power"): _SENSOR_POWER,
        _req(CONF_INVERTER_ENTITY_GRID_POWER, d, "sensor.solis_grid_power_net"): _SENSOR_POWER,
        _req(CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK, d, DEFAULT_SOLIS_STORAGE_CONTROL_READBACK): _SENSOR,
        _req(CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT, d, DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT): _NUMBER,
        _req(
            CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
            d,
            DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT,
        ): _NUMBER,
        _req(CONF_INVERTER_SOLIS_RC_SETPOINT, d, DEFAULT_SOLIS_RC_SETPOINT): _NUMBER,
        _req(CONF_INVERTER_SOLIS_BACKFLOW_POWER, d, DEFAULT_SOLIS_BACKFLOW_POWER): _NUMBER,
        _req(CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER, d, DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER): _NUMBER,
        _req(CONF_INVERTER_SOLIS_SELF_USE_SWITCH, d, DEFAULT_SOLIS_SELF_USE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_TOU_MODE_SWITCH, d, DEFAULT_SOLIS_TOU_MODE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH, d, DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH): _SWITCH,
        _req(CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH, d, DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH): _SWITCH,
        _req(
            CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
            d,
            DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH,
        ): _SWITCH,
        _req(
            CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
            d,
            DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH,
        ): _SWITCH,
    })


def _inverter_solis_pick_schema(options: list[dict], d: dict) -> vol.Schema:
    """Build a single-field schema for selecting which solis_modbus config entry to use.

    Args:
        options: List of ``{"value": entry_id, "label": title}`` dicts from discovered entries.
        d: Existing config/options dict used for the default value.

    Returns:
        Schema with one SelectSelector field for the solis_modbus config entry ID.
    """
    return vol.Schema({
        _req(CONF_SOLIS_CONFIG_ENTRY_ID, d): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
        )
    })


def _profile_manual_schema(profile: ControlProfile, d: dict) -> vol.Schema:
    """Build the manual entity-mapping schema for an entity-driven platform.

    Generates one selector per profile role (sensor / select / number / switch /
    device), keyed by the deterministic ``manual_role_key`` so the resolver finds
    the same value. Required roles use ``_req``; optional roles use ``_opt``.

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
        marker = _req(key, d) if spec.required else _opt(key, d)
        fields[marker] = selector
    return vol.Schema(fields)


def _profile_pick_schema(
    profile: ControlProfile, options: list[dict], d: dict,
) -> vol.Schema:
    """Build the "which vendor config entry" picker schema for a profile platform.

    Args:
        profile: The platform's control profile.
        options: ``{"value": entry_id, "label": title}`` dicts for each entry.
        d: Existing config/options dict for the default selection.

    Returns:
        A single-field schema selecting the vendor config entry id.
    """
    return vol.Schema({
        _req(config_entry_key(profile.platform), d): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
        )
    })


async def _run_inverter_generic_step(
    flow, user_input: dict | None, d: dict,
) -> FlowResult:
    """Drive the shared entity-driven-platform inverter step for either flow.

    Mirrors the Solis step's structure but is profile-driven: auto-detect the
    vendor config entry (single → proceed, multiple → picker, none → manual), or
    go straight to manual mapping for profiles with no config-entry domain
    (Sungrow). The submitted values (a picked config-entry id, or the per-role
    entity ids) are stored verbatim; resolution happens at coordinator setup.

    Args:
        flow: The active config or options flow instance.
        user_input: The submitted form values, or ``None`` on first entry.
        d: Defaults dict for pre-filling the form.

    Returns:
        The next form or step result.
    """
    profile = profile_for(InverterPlatform(flow._inverter_platform))
    if profile is None:  # defensive — routing only sends profile platforms here
        return await flow.async_step_inverter_entities()
    if user_input is not None:
        flow._data.update(user_input)
        return await flow.async_step_forecast()
    if profile.config_entry_domain:
        entries = flow.hass.config_entries.async_entries(profile.config_entry_domain)
        if len(entries) == 1:
            flow._data[config_entry_key(profile.platform)] = entries[0].entry_id
            return await flow.async_step_forecast()
        if len(entries) > 1:
            options = [{"value": e.entry_id, "label": e.title} for e in entries]
            return flow.async_show_form(
                step_id="inverter_generic",
                data_schema=_profile_pick_schema(profile, options, d),
            )
    return flow.async_show_form(
        step_id="inverter_generic",
        data_schema=_profile_manual_schema(profile, d),
    )


_NORDPOOL_RESOLUTION_SELECTOR = SelectSelector(SelectSelectorConfig(
    options=[
        {"value": "15min", "label": "15-minute (recommended)"},
        {"value": "hourly", "label": "Hourly"},
    ],
    mode=SelectSelectorMode.LIST,
))

# Only Nord Pool is validated against a live install. The other live sources are
# built to each integration's documented attribute schema but are NOT
# hardware-verified — flag them in the picker so the user knows to confirm.
_PRICE_SOURCE_SELECTOR = SelectSelector(SelectSelectorConfig(
    options=[
        {"value": "nordpool", "label": "Nord Pool (Nordics/Baltics)"},
        {"value": "entsoe", "label": "ENTSO-e (EU day-ahead) — untested, verify entity attributes"},
        {"value": "octopus", "label": "Octopus Energy Agile (UK) — untested, verify entity attributes"},
        {"value": "amber", "label": "Amber Electric (Australia) — untested, verify entity attributes"},
        {"value": "tou", "label": "Time-of-use schedule (no live feed) — untested"},
    ],
    mode=SelectSelectorMode.LIST,
))

_SELL_MODE_SELECTOR = SelectSelector(SelectSelectorConfig(
    options=[
        {"value": "spot", "label": "Spot-derived (sell = spot − fees)"},
        {"value": "fixed", "label": "Fixed feed-in tariff"},
        {"value": "schedule", "label": "TOU schedule (avoided cost, e.g. NEM 3.0)"},
        {"value": "feed", "label": "Separate live export feed (Octopus/Amber)"},
    ],
    mode=SelectSelectorMode.LIST,
))


def _price_tou_field_keys(i: int) -> tuple[str, str]:
    """Return the (start, price) form field keys for TOU import-band row i."""
    return f"tou{i}_start", f"tou{i}_price"


def _price_tou_schema(d: dict) -> vol.Schema:
    """Build a MAX_TARIFF_BANDS-row absolute TOU import-price schema.

    Each row is a start time ("HH:MM", 24h, local) plus the import price active
    from that time. Rows are pre-filled from the stored list under
    ``CONF_PRICE_TOU_BANDS``; leaving a row's start blank drops it.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema with MAX_TARIFF_BANDS × (start, price) optional fields.
    """
    existing = d.get(CONF_PRICE_TOU_BANDS) or []
    fields: dict = {}
    for i in range(MAX_TARIFF_BANDS):
        start_k, price_k = _price_tou_field_keys(i)
        row = existing[i] if i < len(existing) and isinstance(existing[i], dict) else {}
        fields[vol.Optional(start_k, default=row.get("start", ""))] = str
        fields[vol.Optional(price_k, default=row.get("price", 0.0))] = vol.Coerce(float)
    return vol.Schema(fields)


def _validate_price_tou(user_input: dict) -> tuple[list[dict], dict[str, str]]:
    """Parse a TOU import-price form into stored band dicts, collecting errors.

    Rows with a blank start are skipped; a malformed start yields a per-field
    error. Mirrors :func:`_validate_bands` but for absolute import prices.

    Args:
        user_input: Submitted TOU-form values.

    Returns:
        (bands, errors) — bands sorted by start; errors map field key → code.
    """
    bands: list[dict] = []
    errors: dict[str, str] = {}
    for i in range(MAX_TARIFF_BANDS):
        start_k, price_k = _price_tou_field_keys(i)
        start = str(user_input.get(start_k, "")).strip()
        if not start:
            continue
        if not _valid_hhmm(start):
            errors[start_k] = "invalid_band_time"
            continue
        hh, _, mm = start.partition(":")
        bands.append({
            "start": f"{int(hh):02d}:{int(mm):02d}",
            "price": float(user_input.get(price_k, 0.0)),
        })
    bands.sort(key=lambda b: b["start"])
    return bands, errors


def _sources_schema(d: dict) -> vol.Schema:
    """Build the data-sources entity selection schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering Nordpool entity, resolution, solar energy, and household
        consumption energy selectors. Solar forecast selection is a separate
        step (``async_step_forecast``). Household baseload is derived from the
        AC-port / backup / grid composition — no dedicated load sensor is asked
        for.
    """
    return vol.Schema({
        _req(CONF_PRICE_SOURCE, d, DEFAULT_PRICE_SOURCE): _PRICE_SOURCE_SELECTOR,
        # Generic price sensor (Nord Pool / ENTSO-e / Octopus / Amber). Optional
        # so the TOU source, which needs no live sensor, can leave it blank.
        _opt(CONF_NORDPOOL_ENTITY, d): _SENSOR,
        # Optional separate live export-price sensor (Octopus Outgoing, Amber
        # feed-in); used only by sell_mode="feed".
        _opt(CONF_PRICE_EXPORT_ENTITY, d): _SENSOR,
        _req(CONF_NORDPOOL_RESOLUTION, d, DEFAULT_NORDPOOL_RESOLUTION): _NORDPOOL_RESOLUTION_SELECTOR,
        _opt(CONF_INVERTER_ENTITY_SOLAR_ENERGY, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_PV_POWER, d): _SENSOR_POWER,
        _opt(CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, d): _SENSOR_ENERGY,
        _opt(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, d): _SENSOR_ENERGY,
        # Optional dedicated battery BMS (JK-BMS): SoC + signed pack power.
        # When SoC is set, it is preferred over the inverter, with per-field
        # fallback to the inverter (see inbound/battery_source.py).
        _opt(CONF_BMS_BATTERY_SOC, d): _SENSOR,
        _opt(CONF_BMS_BATTERY_POWER, d): _SENSOR_POWER,
        # Optional custom sidebar-panel title (blank → automatic default).
        _opt(CONF_PANEL_TITLE, d): str,
    })


def _forecast_device_schema(options: list[dict], d: dict) -> vol.Schema:
    """Build the device-based forecast selection schema, pre-filled from d.

    Args:
        options: List of ``{"value": entry_id, "label": title}`` dicts from
            discovered forecast-integration config entries.
        d: Existing config/options dict used for the default selection.

    Returns:
        Single-field schema with a multi-select picker over forecast devices.
    """
    return vol.Schema({
        _opt(CONF_SOLAR_FORECAST_DEVICE_IDS, d): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST, multiple=True)
        ),
    })


def _forecast_manual_schema(d: dict) -> vol.Schema:
    """Build the manual forecast-entity mapping schema, pre-filled from d.

    Shown only as the fallback when no forecast-integration devices are
    discovered.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema with two optional energy-sensor entity selectors.
    """
    return vol.Schema({
        _opt(CONF_SOLAR_FORECAST_ENTITY, d): _SENSOR_ENERGY,
        _opt(CONF_SOLAR_FORECAST_ENTITY_2, d): _SENSOR_ENERGY,
    })


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class SunSaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Multi-step config flow: tariff → battery → inverter platform → inverter entities → forecast → sources."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise config flow with empty data accumulator and generic inverter default."""
        self._data: dict[str, Any] = {}
        self._inverter_platform: str = InverterPlatform.GENERIC.value

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Step 1: Tariff parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_TARIFF_DISTRIBUTION_FEE] < 0:
                errors[CONF_TARIFF_DISTRIBUTION_FEE] = "negative_fee"
            if not 0.0 <= user_input[CONF_TARIFF_TAX_RATE] <= 100.0:
                errors[CONF_TARIFF_TAX_RATE] = "invalid_tax_rate"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_tariff_weekday()

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=_tariff_schema({}),
        )

    async def async_step_tariff_weekday(self, user_input: dict | None = None) -> FlowResult:
        """Step 1b: Weekday time-of-use distribution-fee bands (optional)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bands, errors = _validate_bands(user_input)
            if not errors:
                self._data[CONF_TARIFF_WEEKDAY_BANDS] = bands
                return await self.async_step_tariff_weekend()

        return self.async_show_form(
            step_id="tariff_weekday",
            errors=errors,
            data_schema=_tariff_bands_schema({}, CONF_TARIFF_WEEKDAY_BANDS),
        )

    async def async_step_tariff_weekend(self, user_input: dict | None = None) -> FlowResult:
        """Step 1c: Weekend time-of-use distribution-fee bands (optional)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bands, errors = _validate_bands(user_input)
            if not errors:
                self._data[CONF_TARIFF_WEEKEND_BANDS] = bands
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="tariff_weekend",
            errors=errors,
            data_schema=_tariff_bands_schema({}, CONF_TARIFF_WEEKEND_BANDS),
        )

    async def async_step_battery(self, user_input: dict | None = None) -> FlowResult:
        """Step 2: Battery parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_BATTERY_NOMINAL_CAPACITY] <= 0:
                errors[CONF_BATTERY_NOMINAL_CAPACITY] = "invalid_capacity"
            if user_input[CONF_BATTERY_PURCHASE_PRICE] <= 0:
                errors[CONF_BATTERY_PURCHASE_PRICE] = "invalid_price"
            if not 0.0 < user_input[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] <= 100.0:
                errors[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] = "invalid_efficiency"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter()

        return self.async_show_form(
            step_id="battery",
            errors=errors,
            data_schema=_battery_schema({}),
        )

    async def async_step_inverter(self, user_input: dict | None = None) -> FlowResult:
        """Step 3a: Inverter platform + power ratings."""
        if user_input is not None:
            platform = user_input[CONF_INVERTER_PLATFORM]
            errors = _validate_inverter_power(user_input)
            if platform not in IMPLEMENTED_INVERTER_PLATFORMS:
                errors["base"] = "platform_not_implemented"
            if errors:
                return self.async_show_form(
                    step_id="inverter",
                    data_schema=_inverter_platform_schema({}),
                    errors=errors,
                )
            self._inverter_platform = platform
            self._data.update(user_input)
            self._data[CONF_INVERTER_PLATFORM] = self._inverter_platform
            if self._inverter_platform == InverterPlatform.SOLIS.value:
                return await self.async_step_inverter_solis()
            if self._inverter_platform in _PROFILE_PLATFORMS:
                return await self.async_step_inverter_generic()
            return await self.async_step_inverter_entities()

        return self.async_show_form(
            step_id="inverter",
            data_schema=_inverter_platform_schema({}),
        )

    async def async_step_inverter_generic(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Huawei/SolaX/Sungrow/GoodWe/Deye): auto-detect or map manually."""
        return await _run_inverter_generic_step(self, user_input, {})

    async def async_step_inverter_entities(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b: Standard inverter entity mapping (non-Solis platforms)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_forecast()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=_inverter_entities_schema({}),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): Auto-detect solis_modbus or show entity mapping.

        When exactly one solis_modbus config entry exists the step auto-proceeds
        without showing a form. When multiple entries exist a compact picker is
        shown. When none exist the full manual entity mapping form is shown as a
        fallback.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_forecast()

        solis_entries = self.hass.config_entries.async_entries("solis_modbus")

        if len(solis_entries) == 1:
            self._data[CONF_SOLIS_CONFIG_ENTRY_ID] = solis_entries[0].entry_id
            return await self.async_step_forecast()

        if len(solis_entries) > 1:
            options = [{"value": e.entry_id, "label": e.title} for e in solis_entries]
            return self.async_show_form(
                step_id="inverter_solis",
                data_schema=_inverter_solis_pick_schema(options, {}),
            )

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=_inverter_solis_schema({}),
        )

    async def async_step_forecast(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: Solar forecast device selection (or manual entity fallback).

        When forecast-integration config entries are discovered a multi-select
        device picker is shown; otherwise the manual two-entity form is shown as
        a fallback.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        options = discover_forecast_entries(self.hass)
        if options:
            return self.async_show_form(
                step_id="forecast",
                data_schema=_forecast_device_schema(options, {}),
            )
        return self.async_show_form(
            step_id="forecast_manual",
            data_schema=_forecast_manual_schema({}),
        )

    async def async_step_forecast_manual(self, user_input: dict | None = None) -> FlowResult:
        """Handle submission of the manual forecast-entity fallback form."""
        return await self.async_step_forecast(user_input)

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 5: price source, energy-counter, and BMS entity selection."""
        if user_input is not None:
            self._data.update(user_input)
            if self._data.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_TOU:
                return await self.async_step_price_tou()
            return self.async_create_entry(title="sunSale", data=self._data)

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema({}),
        )

    async def async_step_price_tou(self, user_input: dict | None = None) -> FlowResult:
        """Step 5b (TOU source only): collect the absolute TOU import schedule."""
        if user_input is not None:
            bands, errors = _validate_price_tou(user_input)
            if errors:
                return self.async_show_form(
                    step_id="price_tou",
                    data_schema=_price_tou_schema(user_input),
                    errors=errors,
                )
            self._data[CONF_PRICE_TOU_BANDS] = bands
            return self.async_create_entry(title="sunSale", data=self._data)

        return self.async_show_form(
            step_id="price_tou",
            data_schema=_price_tou_schema({}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SunSaleOptionsFlow:
        """Return the options flow handler for this config entry.

        Args:
            config_entry: The existing config entry to reconfigure.

        Returns:
            SunSaleOptionsFlow instance pre-loaded with the current entry.
        """
        return SunSaleOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow (full reconfiguration)
# ---------------------------------------------------------------------------

class SunSaleOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguring all settings post-setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow, storing the existing entry for default values.

        Args:
            config_entry: The config entry being reconfigured.
        """
        self._entry = config_entry
        self._data: dict[str, Any] = {}
        self._inverter_platform: str = InverterPlatform.GENERIC.value

    def _defaults(self) -> dict[str, Any]:
        """Return merged entry data + options as a single defaults dict.

        Returns:
            Combined dict of entry.data and entry.options (options take precedence).
        """
        return {**self._entry.data, **self._entry.options}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Step 1: Tariff parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_TARIFF_DISTRIBUTION_FEE] < 0:
                errors[CONF_TARIFF_DISTRIBUTION_FEE] = "negative_fee"
            if not 0.0 <= user_input[CONF_TARIFF_TAX_RATE] <= 100.0:
                errors[CONF_TARIFF_TAX_RATE] = "invalid_tax_rate"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_tariff_weekday()

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=_tariff_schema(self._defaults()),
        )

    async def async_step_tariff_weekday(self, user_input: dict | None = None) -> FlowResult:
        """Step 1b: Weekday time-of-use distribution-fee bands (optional)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bands, errors = _validate_bands(user_input)
            if not errors:
                self._data[CONF_TARIFF_WEEKDAY_BANDS] = bands
                return await self.async_step_tariff_weekend()

        return self.async_show_form(
            step_id="tariff_weekday",
            errors=errors,
            data_schema=_tariff_bands_schema(self._defaults(), CONF_TARIFF_WEEKDAY_BANDS),
        )

    async def async_step_tariff_weekend(self, user_input: dict | None = None) -> FlowResult:
        """Step 1c: Weekend time-of-use distribution-fee bands (optional)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bands, errors = _validate_bands(user_input)
            if not errors:
                self._data[CONF_TARIFF_WEEKEND_BANDS] = bands
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="tariff_weekend",
            errors=errors,
            data_schema=_tariff_bands_schema(self._defaults(), CONF_TARIFF_WEEKEND_BANDS),
        )

    async def async_step_battery(self, user_input: dict | None = None) -> FlowResult:
        """Step 2: Battery parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_BATTERY_NOMINAL_CAPACITY] <= 0:
                errors[CONF_BATTERY_NOMINAL_CAPACITY] = "invalid_capacity"
            if user_input[CONF_BATTERY_PURCHASE_PRICE] <= 0:
                errors[CONF_BATTERY_PURCHASE_PRICE] = "invalid_price"
            if not 0.0 < user_input[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] <= 100.0:
                errors[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] = "invalid_efficiency"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_inverter()

        return self.async_show_form(
            step_id="battery",
            errors=errors,
            data_schema=_battery_schema(self._defaults()),
        )

    async def async_step_inverter(self, user_input: dict | None = None) -> FlowResult:
        """Step 3a: Inverter platform + power ratings."""
        if user_input is not None:
            platform = user_input[CONF_INVERTER_PLATFORM]
            errors = _validate_inverter_power(user_input)
            if platform not in IMPLEMENTED_INVERTER_PLATFORMS:
                errors["base"] = "platform_not_implemented"
            if errors:
                return self.async_show_form(
                    step_id="inverter",
                    data_schema=_inverter_platform_schema(self._defaults()),
                    errors=errors,
                )
            self._inverter_platform = platform
            self._data.update(user_input)
            self._data[CONF_INVERTER_PLATFORM] = self._inverter_platform
            if self._inverter_platform == InverterPlatform.SOLIS.value:
                return await self.async_step_inverter_solis()
            if self._inverter_platform in _PROFILE_PLATFORMS:
                return await self.async_step_inverter_generic()
            return await self.async_step_inverter_entities()

        return self.async_show_form(
            step_id="inverter",
            data_schema=_inverter_platform_schema(self._defaults()),
        )

    async def async_step_inverter_generic(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Huawei/SolaX/Sungrow/GoodWe/Deye): auto-detect or map manually."""
        return await _run_inverter_generic_step(self, user_input, self._defaults())

    async def async_step_inverter_entities(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b: Standard inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_forecast()

        return self.async_show_form(
            step_id="inverter_entities",
            data_schema=_inverter_entities_schema(self._defaults()),
        )

    async def async_step_inverter_solis(self, user_input: dict | None = None) -> FlowResult:
        """Step 3b (Solis): Auto-detect solis_modbus or show entity mapping.

        Mirrors the config-flow Solis step. Single-entry detection auto-proceeds;
        multiple entries show a picker; no entries fall back to manual mapping.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_forecast()

        solis_entries = self.hass.config_entries.async_entries("solis_modbus")
        d = self._defaults()

        if len(solis_entries) == 1:
            self._data[CONF_SOLIS_CONFIG_ENTRY_ID] = solis_entries[0].entry_id
            return await self.async_step_forecast()

        if len(solis_entries) > 1:
            options = [{"value": e.entry_id, "label": e.title} for e in solis_entries]
            return self.async_show_form(
                step_id="inverter_solis",
                data_schema=_inverter_solis_pick_schema(options, d),
            )

        return self.async_show_form(
            step_id="inverter_solis",
            data_schema=_inverter_solis_schema(d),
        )

    async def async_step_forecast(self, user_input: dict | None = None) -> FlowResult:
        """Step 4: Solar forecast device selection (or manual entity fallback).

        Mirrors the config-flow forecast step: a multi-select device picker when
        forecast-integration entries are discovered, else the manual two-entity
        form.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sources()

        d = self._defaults()
        options = discover_forecast_entries(self.hass)
        if options:
            return self.async_show_form(
                step_id="forecast",
                data_schema=_forecast_device_schema(options, d),
            )
        return self.async_show_form(
            step_id="forecast_manual",
            data_schema=_forecast_manual_schema(d),
        )

    async def async_step_forecast_manual(self, user_input: dict | None = None) -> FlowResult:
        """Handle submission of the manual forecast-entity fallback form."""
        return await self.async_step_forecast(user_input)

    async def async_step_sources(self, user_input: dict | None = None) -> FlowResult:
        """Step 5: price source, energy-counter, and BMS entities."""
        if user_input is not None:
            self._data.update(user_input)
            if self._data.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_TOU:
                return await self.async_step_price_tou()
            return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id="sources",
            data_schema=_sources_schema(self._defaults()),
        )

    async def async_step_price_tou(self, user_input: dict | None = None) -> FlowResult:
        """Step 5b (TOU source only): collect the absolute TOU import schedule."""
        if user_input is not None:
            bands, errors = _validate_price_tou(user_input)
            if errors:
                return self.async_show_form(
                    step_id="price_tou",
                    data_schema=_price_tou_schema(user_input),
                    errors=errors,
                )
            self._data[CONF_PRICE_TOU_BANDS] = bands
            return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id="price_tou",
            data_schema=_price_tou_schema(self._defaults()),
        )
