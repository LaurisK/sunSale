"""Auto-resolve solar-forecast entity IDs from forecast-integration devices.

The config flow lets the operator pick one or more *forecast devices* (config
entries of a recognised forecast integration — e.g. ``zaliakalnis_garazas`` /
``zaliakalnis_namai``) instead of hunting for individual sensor entities. At
coordinator startup this module turns each chosen config-entry ID into the base
"today" forecast sensor that ``SolarTranslator`` consumes; the translator then
derives the tomorrow / d2..d6 entities by string substitution and sums every
device at each timestamp (one-to-many is handled there).

Resolution mirrors ``solis_entity_resolver`` in spirit: scan the entity
registry for the entities attached to a config entry and match the base sensor
by ``unique_id`` / ``entity_id`` tail. Unlike solis_modbus there is no single
naming standard, so matching is per-integration-domain and best-effort — when a
device exposes nothing recognised it is skipped (logged), and the operator can
still fall back to manual entity mapping in the config flow.
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import Any, NamedTuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..contract.const import (
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
)

_LOGGER = logging.getLogger(__name__)


def manual_forecast_entities(config: Mapping[str, Any]) -> list[str]:
    """Return the manually picked base forecast sensors from a config dict.

    The ``CONF_SOLAR_FORECAST_ENTITIES`` list wins whenever it is stored — even
    empty — because the options flow cannot delete the two legacy keys from an
    entry's ``data``; without this rule they would resurface after the user
    cleared or replaced them. Entries without the list fall back to the pair.

    Args:
        config: Merged config-entry data + options (or a flow's accumulator).

    Returns:
        The non-empty sensor entity IDs, in stored order.
    """
    if CONF_SOLAR_FORECAST_ENTITIES in config:
        return [e for e in config.get(CONF_SOLAR_FORECAST_ENTITIES) or [] if e]
    return [e for e in (config.get(CONF_SOLAR_FORECAST_ENTITY), config.get(CONF_SOLAR_FORECAST_ENTITY_2)) if e]


def combine_forecast_entities(resolved: list[str], config: Mapping[str, Any]) -> list[str]:
    """Merge device-resolved and manually picked base forecast sensors.

    Entries saved with the sensor list use both sources together — one array
    per device or sensor, de-duplicated in order. Older entries keep their exact
    previous behaviour (resolved devices if any, else the legacy pair): their
    leftover manual keys must not be added on top of a device selection, or
    the same array would be counted twice.

    Args:
        resolved: Base sensors resolved from the chosen forecast devices.
        config: Merged config-entry data + options.

    Returns:
        Base forecast entity IDs for ``SolarTranslator`` (possibly empty).
    """
    manual = manual_forecast_entities(config)
    if CONF_SOLAR_FORECAST_ENTITIES not in config:
        return list(resolved) or manual
    return list(dict.fromkeys([*resolved, *manual]))

# Recognised forecast-integration domains. Adding support for another
# integration is a one-line edit here plus its base-sensor tails below.
FORECAST_DOMAINS: tuple[str, ...] = (
    "open_meteo_solar_forecast",
    "forecast_solar",
    "solcast_solar",
)

# Per-domain candidate base-sensor tails, in preference order. The base sensor
# must be a "today" sensor so ``SolarTranslator._tomorrow_entity`` /
# ``_day_entity`` can substitute "today" → "tomorrow"/"dN". The translator reads
# the ``watts`` attribute first (Open Meteo / Forecast.Solar) and falls back to
# the ``forecast`` attribute (Solcast), so either base entity works downstream.
_BASE_TAILS: dict[str, tuple[str, ...]] = {
    "open_meteo_solar_forecast": ("energy_production_today",),
    "forecast_solar": ("energy_production_today",),
    # Solcast names its daily forecast sensor ``..._forecast_today`` and carries
    # the per-slot ``detailedForecast`` / ``forecast`` attribute.
    "solcast_solar": ("forecast_today",),
}


def discover_forecast_entries(hass: HomeAssistant) -> list[dict[str, str]]:
    """List forecast-integration config entries as picker options.

    Args:
        hass: Home Assistant instance.

    Returns:
        List of ``{"value": entry_id, "label": title}`` dicts, one per config
        entry belonging to a recognised forecast integration domain, sorted by
        title for a stable picker order. Empty when none are installed.
    """
    options: list[dict[str, str]] = []
    for domain in FORECAST_DOMAINS:
        for entry in hass.config_entries.async_entries(domain):
            options.append({"value": entry.entry_id, "label": entry.title or domain})
    options.sort(key=lambda o: o["label"].casefold())
    return options


class DetectedForecast(NamedTuple):
    """A forecast sensor on this system that ``SolarTranslator`` could read."""

    entity_id: str
    name: str


# The word the translator substitutes to reach the other forecast days. A sensor
# without it is unusable as a base even when its attributes parse.
_TODAY = "today"


def is_forecast_sensor(entity_id: str, attributes: Mapping[str, Any]) -> bool:
    """Return True when ``SolarTranslator`` could use this sensor as a base entity.

    Two conditions, both load-bearing:

    * The attributes carry a shape the translator actually parses — a ``watts``
      dict (Open Meteo / Forecast.Solar) or a ``forecast`` list (Solcast). A
      sensor carrying only ``wh_period`` is *not* readable, however much it
      looks like a forecast.
    * The entity id says "today". ``_tomorrow_entity`` / ``_day_entity`` reach
      the other days by substituting that word, so a "d3" sensor would send the
      translator hunting for entities that cannot exist.

    Keep this in sync with what :class:`..forecast.SolarTranslator` reads, the
    way ``pricing.price_source_of`` tracks the price translators.

    Args:
        entity_id: The sensor's entity id.
        attributes: Its state attributes.

    Returns:
        True when the sensor is usable as a base "today" forecast entity.
    """
    if _TODAY not in entity_id.casefold():
        return False
    if isinstance(attributes.get("watts"), dict):
        return True
    forecast = attributes.get("forecast")
    return isinstance(forecast, list) and bool(forecast)


def detect_forecast_sensors(hass: HomeAssistant, exclude: Collection[str] = ()) -> list[DetectedForecast]:
    """Return the forecast sensors on this system, minus the ones already covered.

    Complements :func:`discover_forecast_entries`: that finds whole
    integrations, this finds the leftovers — a forecast integration sunSale does
    not recognise, or a template sensor built by hand. Sensors a discovered
    device already resolves to are excluded so the form never offers the same
    array twice, once as a device and once as a sensor.

    Args:
        hass: Home Assistant instance, or None (nothing is detected then).
        exclude: Entity ids already reachable another way.

    Returns:
        The usable base sensors, by entity id. Empty when every forecast on the
        system is already covered by a device.
    """
    if hass is None:
        return []
    found = [
        DetectedForecast(state.entity_id, str(state.attributes.get("friendly_name") or ""))
        for state in hass.states.async_all("sensor")
        if state.entity_id not in exclude and is_forecast_sensor(state.entity_id, state.attributes)
    ]
    return sorted(found, key=lambda sensor: sensor.entity_id)


def _matches(uid: str, entity_id: str, tail: str) -> bool:
    """Return True when an entry's identifiers end with ``tail``.

    Matches the clean ``unique_id`` form first, then the entity_id object-id
    tail as a fallback, so resolution survives integrations that don't expose a
    tail-suffixed unique_id.

    Args:
        uid: Entity registry unique_id (may be empty).
        entity_id: Home Assistant entity_id (e.g. ``sensor.<slug>``).
        tail: Candidate base-sensor suffix (e.g. ``energy_production_today``).

    Returns:
        True when either identifier ends with ``_<tail>`` / ``<tail>``.
    """
    if uid and (uid.endswith(f"_{tail}") or uid == tail):
        return True
    if entity_id and entity_id.endswith(f"_{tail}"):
        return True
    return False


def _resolve_one(hass: HomeAssistant, registry: er.EntityRegistry, entry_id: str) -> str:
    """Resolve a single forecast config entry to its base "today" sensor.

    Args:
        hass: Home Assistant instance.
        registry: The entity registry (passed in to avoid re-fetching per entry).
        entry_id: A forecast-integration config-entry ID.

    Returns:
        The resolved base entity_id, or an empty string when the entry is
        unknown, belongs to an unrecognised domain, or exposes no matching
        sensor.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        _LOGGER.warning("Forecast device %s not found; skipping", entry_id)
        return ""
    tails = _BASE_TAILS.get(entry.domain)
    if not tails:
        _LOGGER.warning(
            "Forecast device %s has unsupported domain %r; skipping",
            entry_id,
            entry.domain,
        )
        return ""

    entities = er.async_entries_for_config_entry(registry, entry_id)
    # Honour tail preference order: a tail earlier in the tuple wins even if a
    # later-tail entity is encountered first in the registry scan.
    for tail in tails:
        for ent in entities:
            if ent.domain != "sensor":
                continue
            if _matches(ent.unique_id or "", ent.entity_id or "", tail):
                return ent.entity_id

    _LOGGER.warning(
        "Forecast device %s (%s) exposed no recognised base sensor; "
        "tried tails %s",
        entry_id,
        entry.domain,
        tails,
    )
    return ""


def resolve_forecast_entities(hass: HomeAssistant, device_ids: list[str]) -> list[str]:
    """Resolve forecast device IDs to the base entity IDs SolarTranslator reads.

    Args:
        hass: Home Assistant instance.
        device_ids: Forecast-integration config-entry IDs chosen in the config
            flow.

    Returns:
        List of resolved base "today" entity IDs (one per device that resolved),
        in input order, with unresolved devices omitted. Empty when nothing
        resolves — the caller then falls back to the legacy entity keys.
    """
    if not device_ids:
        return []
    registry = er.async_get(hass)
    resolved: list[str] = []
    for entry_id in device_ids:
        eid = _resolve_one(hass, registry, entry_id)
        if eid:
            resolved.append(eid)
    return resolved
