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

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

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
