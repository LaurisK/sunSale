"""Solar-forecast form: forecast devices, detected forecast sensors, and by-hand sensors."""
from __future__ import annotations

import voluptuous as vol

from ..contract.const import CONF_SOLAR_FORECAST_DEVICE_IDS, CONF_SOLAR_FORECAST_ENTITIES
from ..inbound.forecast_resolver import DetectedForecast, manual_forecast_entities
from .fields import SENSOR_ENERGY_MULTI, list_selector, suggest

# Form-only field: the detected leftover forecast sensors. Merged into
# CONF_SOLAR_FORECAST_ENTITIES on save, so nothing stores it.
FORECAST_DETECTED = "detected_sensors"


def forecast_label(sensor: DetectedForecast) -> str:
    """Return a detected forecast sensor's row in the picker.

    Args:
        sensor: The detected sensor.

    Returns:
        ``"<name> (<entity id>)"``, or just the entity id when it publishes no
        friendly name.
    """
    return f"{sensor.name} ({sensor.entity_id})" if sensor.name else sensor.entity_id


def split_forecast_sensors(
    stored: list[str], detected: list[DetectedForecast],
) -> tuple[list[str], list[str]]:
    """Split the stored sensors into the detected ones and the rest.

    Which field a stored sensor re-opens in is decided by whether detection
    still finds it, so the form is stable across visits rather than depending
    on which field it was first typed into.

    Args:
        stored: The sensors the entry holds.
        detected: What detection found this time.

    Returns:
        ``(detected_picks, by_hand)`` — the stored sensors detection knows
        about, and the ones only the by-hand picker can hold.
    """
    known = {sensor.entity_id for sensor in detected}
    return [e for e in stored if e in known], [e for e in stored if e not in known]


def forecast_schema(options: list[dict], detected: list[DetectedForecast], d: dict) -> vol.Schema:
    """Build the solar-forecast schema: devices, detected sensors and by-hand sensors.

    Three disjoint ways to name an array, each optional:

    * the device picker, when a recognised forecast integration is installed —
      one row per integration, its sensor resolved at startup;
    * the detected-sensor list, for forecasts no device covers (an unrecognised
      integration, or a template sensor) — omitted when there are none;
    * the by-hand picker, for anything detection cannot recognise at all.

    Args:
        options: ``{"value": entry_id, "label": title}`` dicts for the
            discovered forecast-integration config entries; the device picker
            is omitted when empty.
        detected: Forecast sensors no discovered device already covers.
        d: Existing config/options dict used for the default selection.

    Returns:
        Schema with the pickers this system has something to offer for.
    """
    # suggested_value, not default: a selection the user clears must stay cleared.
    fields: dict = {}
    if options:
        devices = d.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []
        fields[vol.Optional(CONF_SOLAR_FORECAST_DEVICE_IDS, description=suggest(devices))] = list_selector(
            options, multiple=True
        )
    picks, by_hand = split_forecast_sensors(manual_forecast_entities(d), detected)
    if detected:
        rows = [{"value": sensor.entity_id, "label": forecast_label(sensor)} for sensor in detected]
        fields[vol.Optional(FORECAST_DETECTED, description=suggest(picks))] = list_selector(rows, multiple=True)
    fields[vol.Optional(CONF_SOLAR_FORECAST_ENTITIES, description=suggest(by_hand))] = SENSOR_ENERGY_MULTI
    return vol.Schema(fields)


def forecast_summary(options: list[dict], detected: list[DetectedForecast], resolved: dict[str, str]) -> str:
    """Return the forecast form's sentence about what was found and what it resolved to.

    Names each chosen device's resolved sensor, and says so when one resolved to
    nothing — previously a ``_resolve_one`` miss was only a log line, so an
    array could silently contribute no forecast at all.

    Args:
        options: The discovered forecast-integration entries.
        detected: Forecast sensors no device covers.
        resolved: ``{entry_id: entity_id}`` for the chosen devices, with ""
            for a device that resolved to nothing.

    Returns:
        The form's description text.
    """
    parts: list[str] = []
    if options:
        parts.append(f"Found {len(options)} forecast integration(s) on this system.")
    if detected:
        parts.append(
            f"{len(detected)} forecast sensor(s) no integration above covers are offered as a list."
        )
    if not options and not detected:
        parts.append(
            "No forecast integration or sensor was found on this system — pick the sensors by hand, "
            "or install a solar-forecast integration."
        )
    titles = {option["value"]: option["label"] for option in options}
    for entry_id, entity_id in resolved.items():
        name = titles.get(entry_id, entry_id)
        parts.append(
            f"**{name}** → `{entity_id}`" if entity_id
            else f"⚠ **{name}** exposes no forecast sensor sunSale recognises — it contributes nothing."
        )
    return "\n\n".join(parts)
