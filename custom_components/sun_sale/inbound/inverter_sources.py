"""Find the sensors an inverter's optional source pages can be filled from.

The optional inverter sources — PV power, the AC-port and backup-port power the
derived observers need, the per-direction grid power, the daily and yesterday
energy counters — all live on the *same hardware* the control mapping already
resolved. So the setup flow should not ask the user to pick them out of every
sensor in Home Assistant; it should offer the ones on their inverter.

That is what this module does, in the spirit of
:mod:`.metered_device_resolver`: take the platform's own role map, find the HA
device(s) those entities belong to, and classify everything else on those
devices by what each source needs. The output feeds the picker lists on the
``sources`` / ``sources_energy`` pages; when a role finds no candidate the page
falls back to a plain entity picker, exactly as the price feed falls back when
no price sensor is detected.

Two boundaries worth keeping:

* **Advisory only.** Nothing here decides what the *runtime* reads —
  :mod:`.inverter_entity_resolver` still merges manual config first and
  auto-detect second. This module only decides what the *form* offers, so a
  wrong guess costs the user one dropdown change, never a wrong reading.
* **Ranked, not filtered.** Every same-kind sensor on the inverter is offered;
  the name hints only decide the order and the pre-selection. Filtering by name
  would hide the right sensor whenever an integration names things unusually,
  which is the failure mode this module exists to remove.

The same classifier serves the dedicated battery BMS, which is picked as a
device of its own rather than found on the inverter — see :data:`BMS_SPECS` and
:func:`detect_bms_sources`.

The classification core (:func:`role_candidates`, :func:`default_source`) is
pure and takes registry-entry-shaped objects, so it unit tests without Home
Assistant; :func:`detect_inverter_sources` / :func:`detect_bms_sources` are the
thin HA edge.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..contract.const import (
    CONF_BMS_BATTERY_POWER,
    CONF_BMS_BATTERY_SOC,
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER,
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY,
    CONF_INVERTER_ENTITY_GENERATION_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    CONF_INVERTER_PLATFORM,
    DOMAIN,
)
from ..outbound.inverter import InverterPlatform
from .inverter_discovery import discovery_for

_LOGGER = logging.getLogger(__name__)

# A counter's state class: what the Energy dashboard accepts as a meter, and
# what separates today's running counter from an instantaneous reading.
METER_STATE_CLASSES = frozenset({"total", "total_increasing"})
# Integrations whose sensors are never offered as a source: a ``utility_meter``
# helper is a resetting copy of a meter already listed, and sunSale's own
# sensors would make the pipeline read its own output.
EXCLUDED_PLATFORMS = frozenset({"utility_meter", DOMAIN})

Attributes = Callable[[str], Mapping[str, Any]]

# Word marking a counter as holding *yesterday's* finalised total rather than
# today's running one. The two are interchangeable to every filter HA exposes
# (same device class, same state class), so the name is the only discriminator —
# and picking the wrong one would silently feed the bake-in a day-old total.
_YESTERDAY = "yesterday"


@dataclass(frozen=True)
class SourceSpec:
    """What one optional inverter source needs, and how to recognise it.

    Attributes:
        conf_key: The ``CONF_INVERTER_ENTITY_*`` key the form writes.
        role: Role name the platform's discovery strategy resolves this source
            under, or "" when no platform resolves it. A resolved role is
            pre-selected and marked auto-detected.
        device_class: The sensor ``device_class`` a candidate must report.
        hints: Name fragments that make a candidate the likely match; they rank
            candidates and choose the pre-selection, never exclude anything.
        counter: Whether a candidate must be a running counter (a ``total`` /
            ``total_increasing`` state class), which is what separates an energy
            *counter* from an instantaneous energy reading.
        yesterday: Whether this source wants yesterday's total rather than
            today's. Candidates are split on :data:`_YESTERDAY` accordingly.
    """

    conf_key: str
    role: str
    device_class: str
    hints: tuple[str, ...]
    counter: bool = False
    yesterday: bool = False


# Every optional source the inverter section can fill, in the order its pages
# show them. Adding a source is one row here plus its form field.
SOURCE_SPECS: tuple[SourceSpec, ...] = (
    # --- Live power sensors ---
    SourceSpec(
        CONF_INVERTER_ENTITY_PV_POWER, "pv_power", "power",
        ("pv", "solar", "generation", "yield"),
    ),
    # Signed, positive = inverter→grid. Balances against the grid-net reading
    # in the derived consumption / losses formulas.
    SourceSpec(
        CONF_INVERTER_ENTITY_AC_PORT_POWER, "ac_port_power", "power",
        ("ac_grid_port", "grid_port", "ac_port", "ac_output"),
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_BACKUP_POWER, "backup_power", "power",
        ("backup", "eps", "ups"),
    ),
    # No platform resolves these: solis_modbus publishes only the signed net
    # flow, so the observers normally project onto each side themselves. An
    # install that does publish per-direction sensors can say so here.
    SourceSpec(
        CONF_INVERTER_ENTITY_GRID_IMPORT_POWER, "", "power",
        ("grid_import", "import", "from_grid", "consumed_from"),
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_GRID_EXPORT_POWER, "", "power",
        ("grid_export", "export", "fed_into", "feed_in", "to_grid"),
    ),
    # --- Energy counters ---
    SourceSpec(
        CONF_INVERTER_ENTITY_SOLAR_ENERGY, "solar_energy_today", "energy",
        ("pv", "solar", "generation", "yield"), counter=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY, "", "energy",
        ("house", "home", "load", "consumption"), counter=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, "grid_import_energy_today", "energy",
        ("grid_import", "import", "from_grid", "purchas"), counter=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, "grid_export_energy_today", "energy",
        ("grid_export", "export", "fed_into", "feed_in", "to_grid"), counter=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY, "battery_charge_energy_today", "energy",
        ("battery_charge", "charge"), counter=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY, "battery_discharge_energy_today", "energy",
        ("battery_discharge", "discharge"), counter=True,
    ),
    # --- Yesterday totals (the bake-in's preferred source) ---
    SourceSpec(
        CONF_INVERTER_ENTITY_GENERATION_YESTERDAY, "solar_energy_yesterday", "energy",
        ("pv", "solar", "generation", "yield"), yesterday=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY, "grid_import_energy_yesterday", "energy",
        ("grid_import", "import", "from_grid", "purchas"), yesterday=True,
    ),
    SourceSpec(
        CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY, "grid_export_energy_yesterday", "energy",
        ("grid_export", "export", "fed_into", "feed_in", "to_grid"), yesterday=True,
    ),
)

SPEC_BY_KEY: dict[str, SourceSpec] = {spec.conf_key: spec for spec in SOURCE_SPECS}

# A dedicated battery BMS (JK-BMS or similar) is its own piece of hardware, so
# it is picked as a *device* and its sensors are classified on that device —
# never on the inverter's. That is why these two specs sit outside
# ``SOURCE_SPECS``: including them would offer the inverter's own battery
# sensors as "the BMS", which is exactly the reading the BMS is meant to replace.
BMS_SPECS: tuple[SourceSpec, ...] = (
    SourceSpec(
        CONF_BMS_BATTERY_SOC, "", "battery",
        ("soc", "state_of_charge", "charge_level"),
    ),
    # Signed pack power, positive = charging (see inbound/battery_source.py).
    SourceSpec(
        CONF_BMS_BATTERY_POWER, "", "power",
        ("pack", "battery", "power"),
    ),
)


@dataclass(frozen=True)
class DetectedSource:
    """One candidate entity offered for an inverter source.

    Attributes:
        entity_id: The candidate's entity id.
        name: Its display name, or "" when the registry carries none.
        auto: Whether the platform's own discovery already resolved this entity
            for the role — the strongest possible hint, shown as such.
    """

    entity_id: str
    name: str
    auto: bool


@dataclass(frozen=True)
class SourceOptions:
    """The candidates for one source, with the row to pre-select.

    Attributes:
        candidates: Candidates in display order, likeliest first.
        default: Entity id to pre-select, or "" when nothing is a good guess
            and the user should choose.
    """

    candidates: tuple[DetectedSource, ...]
    default: str


# --- Classification core (pure) --------------------------------------------- #

def _text(value: Any) -> str:
    """Return an enum/str registry value as a plain string ("" for None)."""
    return str(value) if value is not None else ""


def _searchable(entry: Any) -> str:
    """Return the entry's id and names folded into one string to match hints against."""
    return f"{entry.entity_id} {entry.original_name or ''} {entry.name or ''}".casefold()


def _usable(entry: Any) -> bool:
    """Return True when a registry entry may be offered as a source at all.

    Args:
        entry: Entity-registry entry.

    Returns:
        True for an enabled, visible ``sensor`` that is not a helper copy or one
        of sunSale's own sensors.
    """
    if entry.domain != "sensor" or entry.platform in EXCLUDED_PLATFORMS:
        return False
    return entry.disabled_by is None and entry.hidden_by is None


def _fits(entry: Any, attributes: Mapping[str, Any], spec: SourceSpec) -> bool:
    """Return True when an entry reports the kind of value the source needs.

    Args:
        entry: Entity-registry entry.
        attributes: Its current state attributes (empty when it has no state
            yet); a fallback for classes the registry does not carry.
        spec: The source being filled.

    Returns:
        True when the device class matches, the counter requirement is met, and
        the entry is on the same side of the today / yesterday split.
    """
    device_class = _text(entry.device_class or entry.original_device_class or attributes.get("device_class"))
    if device_class != spec.device_class:
        return False
    if spec.counter:
        state_class = _text((entry.capabilities or {}).get("state_class") or attributes.get("state_class"))
        if state_class not in METER_STATE_CLASSES:
            return False
    return (_YESTERDAY in _searchable(entry)) == spec.yesterday


def role_candidates(
    entries: Iterable[Any], attributes: Attributes, spec: SourceSpec, auto: str = "",
) -> list[DetectedSource]:
    """Return the entries that could fill a source, likeliest first.

    Ordering puts the platform's own resolved entity first, then the ones whose
    name matches a hint, then the rest alphabetically — so the list stays
    complete while the right answer sits at the top.

    Args:
        entries: Entity-registry entries to consider (duplicates are dropped).
        attributes: Returns an entity's state attributes by entity id.
        spec: The source being filled.
        auto: Entity id the platform's discovery resolved for this role, if any.

    Returns:
        Candidates in display order.
    """
    found: dict[str, tuple[bool, str]] = {}
    for entry in entries:
        if entry.entity_id in found or not _usable(entry):
            continue
        if not _fits(entry, attributes(entry.entity_id), spec):
            continue
        hinted = any(hint in _searchable(entry) for hint in spec.hints)
        found[entry.entity_id] = (hinted, _text(entry.name or entry.original_name))

    def rank(entity_id: str) -> tuple[bool, bool, str]:
        hinted, _name = found[entity_id]
        return (entity_id != auto, not hinted, entity_id)

    return [
        DetectedSource(entity_id, found[entity_id][1], entity_id == auto)
        for entity_id in sorted(found, key=rank)
    ]


def default_source(candidates: list[DetectedSource], spec: SourceSpec, stored: str = "") -> str:
    """Return the candidate to pre-select for a source.

    Preference order: what the user already stored (while it is still a
    candidate), then the platform's auto-detected entity, then the best
    name-hint match. With none of those the row opens unset, because guessing
    from device class alone would be as likely wrong as right.

    Args:
        candidates: Output of :func:`role_candidates`.
        spec: The source being filled.
        stored: The entity id currently in the config, if any.

    Returns:
        An entity id, or "" when nothing should be pre-selected.
    """
    ids = {source.entity_id for source in candidates}
    if stored and stored in ids:
        return stored
    if auto := next((source.entity_id for source in candidates if source.auto), ""):
        return auto
    hinted = [
        source.entity_id for source in candidates
        if any(hint in source.entity_id.casefold() or hint in source.name.casefold() for hint in spec.hints)
    ]
    return hinted[0] if hinted else ""


# --- Home Assistant edge ---------------------------------------------------- #

def _resolved_roles(hass: HomeAssistant, data: dict) -> dict[str, str]:
    """Return the role → entity-id map the configured platform resolves.

    Args:
        hass: Home Assistant instance.
        data: Merged config-entry data + options (or a setup flow's accumulator).

    Returns:
        The platform's role map, or ``{}`` while the inverter section is too
        incomplete to resolve one — a half-filled flow is the normal case here,
        not an error (same guard as ``inverter_grid_meters``).
    """
    try:
        platform = InverterPlatform(data[CONF_INVERTER_PLATFORM])
        return discovery_for(platform).resolve_role_ids(hass, data)
    except (KeyError, ValueError):
        return {}


def _attributes(hass: HomeAssistant) -> Attributes:
    """Return a lookup of an entity's current state attributes."""
    def lookup(entity_id: str) -> Mapping[str, Any]:
        state = hass.states.get(entity_id)
        return state.attributes if state is not None else {}
    return lookup


def inverter_device_entries(hass: HomeAssistant, data: dict) -> tuple[list[Any], dict[str, str]]:
    """Return every entity on the inverter's device(s), plus its resolved roles.

    The inverter's devices are found the way :func:`inverter_grid_meters` finds
    them: whatever the platform's discovery resolved must itself live on the
    hardware, so those entities' ``device_id``s *are* the inverter.

    Args:
        hass: Home Assistant instance.
        data: Merged config-entry data + options.

    Returns:
        ``(entries, roles)`` — the registry entries on those devices (empty when
        the inverter cannot be located yet) and the platform's role map.
    """
    roles = _resolved_roles(hass, data)
    mapped = {entity_id for entity_id in roles.values() if entity_id and "." in entity_id}
    if not mapped:
        return [], roles
    registry = er.async_get(hass)
    mapped_entries = [entry for entity_id in mapped if (entry := registry.async_get(entity_id))]
    entries = list(mapped_entries)
    for device_id in {entry.device_id for entry in mapped_entries if entry.device_id}:
        entries.extend(er.async_entries_for_device(registry, device_id))
    return entries, roles


def _detect_on(hass: HomeAssistant, entries: list[Any], specs: tuple[SourceSpec, ...], data: dict,
               roles: dict[str, str] | None = None) -> dict[str, SourceOptions]:
    """Return the candidates among ``entries`` for each of ``specs``.

    Args:
        hass: Home Assistant instance, for the state-attribute fallback.
        entries: The registry entries to classify.
        specs: The sources to look for.
        data: Merged config-entry data + options, for the stored pre-selection.
        roles: The platform's role map, when one applies.

    Returns:
        ``{conf_key: SourceOptions}``, omitting sources with no candidate.
    """
    attributes = _attributes(hass)
    detected: dict[str, SourceOptions] = {}
    for spec in specs:
        auto = (roles or {}).get(spec.role, "") if spec.role else ""
        candidates = role_candidates(entries, attributes, spec, auto)
        if candidates:
            detected[spec.conf_key] = SourceOptions(
                tuple(candidates), default_source(candidates, spec, _text(data.get(spec.conf_key))),
            )
    return detected


def device_of(hass: HomeAssistant, entity_id: str) -> str:
    """Return the id of the HA device an entity belongs to, or "".

    Lets a page pre-fill a device picker from an entity already stored, so the
    device does not need a config key of its own.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity to look up ("" returns "").

    Returns:
        The device-registry id, or "" when the entity is unknown or standalone.
    """
    if not entity_id:
        return ""
    entry = er.async_get(hass).async_get(entity_id)
    return (entry.device_id or "") if entry is not None else ""


def detect_bms_sources(hass: HomeAssistant, device_id: str, data: dict) -> dict[str, SourceOptions]:
    """Return the SoC / pack-power candidates on a picked battery device.

    Args:
        hass: Home Assistant instance.
        device_id: The device-registry id chosen on the BMS picker.
        data: Merged config-entry data + options, for the stored pre-selection.

    Returns:
        ``{conf_key: SourceOptions}`` for the BMS sources this device can fill.
        ``CONF_BMS_BATTERY_SOC`` missing means the device publishes no state of
        charge, so it cannot serve as a battery source at all — the SoC is what
        gates the chain in ``inbound/battery_source.py``.
    """
    if not device_id:
        return {}
    entries = list(er.async_entries_for_device(er.async_get(hass), device_id))
    return _detect_on(hass, entries, BMS_SPECS, data)


def detect_inverter_sources(hass: HomeAssistant, data: dict) -> dict[str, SourceOptions]:
    """Return the candidates found on the inverter for each optional source.

    Args:
        hass: Home Assistant instance.
        data: Merged config-entry data + options (or a setup flow's accumulator).

    Returns:
        ``{conf_key: SourceOptions}`` for every source with at least one
        candidate. Sources with none are absent, which is the signal for the
        form to fall back to a plain entity picker.
    """
    entries, roles = inverter_device_entries(hass, data)
    if not entries:
        return {}
    detected = _detect_on(hass, entries, SOURCE_SPECS, data, roles)
    _LOGGER.debug(
        "Inverter source detection: %d entities on the inverter, candidates for %d of %d sources",
        len(entries), len(detected), len(SOURCE_SPECS),
    )
    return detected
