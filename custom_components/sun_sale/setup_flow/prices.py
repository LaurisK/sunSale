"""Electricity-prices forms: price feed and price level.

The buy / sell price formulas have their own module, :mod:`.tariffs`.
"""
from __future__ import annotations

import voluptuous as vol

from ..contract.const import (
    CONF_CURRENCY,
    CONF_NORDPOOL_ENTITY,
    CONF_NORDPOOL_RESOLUTION,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_LEVEL_CHEAP_BELOW,
    CONF_PRICE_LEVEL_CHEAP_SHARE,
    CONF_PRICE_LEVEL_EXPENSIVE_ABOVE,
    CONF_PRICE_LEVEL_EXPENSIVE_SHARE,
    CONF_PRICE_SOURCE,
    CONF_WEATHER_ENTITY,
    DEFAULT_CURRENCY,
    DEFAULT_NORDPOOL_RESOLUTION,
    DEFAULT_PRICE_LEVEL_SHARE_PCT,
    DEFAULT_PRICE_SOURCE,
    PRICE_SOURCE_NORDPOOL,
)
from ..inbound.pricing import DetectedPriceSensor
from ..inbound.weather import DetectedWeather
from .fields import (
    NONE,
    OTHER,
    PRICE_ENTITY,
    WEATHER,
    detected_options,
    detected_selector,
    list_selector,
    opt,
    req,
    suggest,
)

_NORDPOOL_RESOLUTION_SELECTOR = list_selector([
    {"value": "15min", "label": "15-minute (recommended)"},
    {"value": "hourly", "label": "Hourly"},
])

# Only Nord Pool is validated against a live install. The other live sources are
# built to each integration's documented attribute schema but are NOT
# hardware-verified — flag them in the picker so the user knows to confirm.
_PRICE_SOURCE_SELECTOR = list_selector([
    {"value": "nordpool", "label": "Nord Pool (Nordics/Baltics)"},
    {"value": "entsoe", "label": "ENTSO-e (EU day-ahead) — untested, verify entity attributes"},
    {"value": "octopus", "label": "Octopus Energy Agile (UK) — untested, verify entity attributes"},
    {"value": "amber", "label": "Amber Electric (Australia) — untested, verify entity attributes"},
])

PRICE_SOURCE_SHORT_LABELS = {
    "nordpool": "Nord Pool",
    "entsoe": "ENTSO-e",
    "octopus": "Octopus Agile",
    "amber": "Amber Electric",
}


# The price-feed form's lists of detected price sensors and export-price feeds
# (form-only, never stored). The reserved rows are the shared ones
# (:mod:`.fields`): Other opens the by-hand source + sensor form, None means no
# separate export feed.
PRICE_SENSOR_CHOICE = "price_sensor"
PRICE_EXPORT_CHOICE = "price_export_sensor"
PRICE_SENSOR_OTHER = OTHER
PRICE_EXPORT_NONE = NONE


def _source_and_sensor_fields(d: dict) -> dict:
    """Return the by-hand price source and sensor fields, pre-filled from d.

    The sensor is optional in the schema because it is only needed while a
    price follows the market; the step checks that.
    """
    return {
        req(CONF_PRICE_SOURCE, d, DEFAULT_PRICE_SOURCE): _PRICE_SOURCE_SELECTOR,
        # Generic price sensor (Nord Pool / ENTSO-e / Octopus / Amber).
        opt(CONF_NORDPOOL_ENTITY, d): PRICE_ENTITY,
    }


def _export_field(d: dict) -> dict:
    """Return the by-hand export-price sensor field, pre-filled from d.

    It uses ``suggested_value`` so a cleared sensor stays cleared: every form
    showing it stores an absent value as no export feed.
    """
    return {
        # Optional separate live export-price sensor (Octopus Outgoing, Amber
        # feed-in); a dynamic sell price uses it where it has a value.
        vol.Optional(CONF_PRICE_EXPORT_ENTITY, description=suggest(d.get(CONF_PRICE_EXPORT_ENTITY))): PRICE_ENTITY,
    }


def _feed_setting_fields(d: dict) -> dict:
    """Return the resolution and currency fields, pre-filled from d."""
    return {
        req(CONF_NORDPOOL_RESOLUTION, d, DEFAULT_NORDPOOL_RESOLUTION): _NORDPOOL_RESOLUTION_SELECTOR,
        req(CONF_CURRENCY, d, DEFAULT_CURRENCY): str,
    }


def prices_schema(d: dict) -> vol.Schema:
    """Build the price-feed schema for a system with no detected price sensor.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema covering the price source, the market price sensor, the
        optional separate export sensor, the slot resolution and the display
        currency.
    """
    return vol.Schema({**_source_and_sensor_fields(d), **_export_field(d), **_feed_setting_fields(d)})


def price_manual_schema(d: dict) -> vol.Schema:
    """Build the by-hand price source, sensor and export sensor schema (the picker's Other row)."""
    return vol.Schema({**_source_and_sensor_fields(d), **_export_field(d)})


def detected_label(sensor: DetectedPriceSensor) -> str:
    """Return a detected price sensor's row in the picker.

    Args:
        sensor: The detected sensor.

    Returns:
        ``"<source>: <name> (<entity id>)"``, marked untested for every
        source but Nord Pool.
    """
    label = f"{PRICE_SOURCE_SHORT_LABELS.get(sensor.source, sensor.source)}: {sensor.name} ({sensor.entity_id})"
    return label if sensor.source == PRICE_SOURCE_NORDPOOL else f"{label} — untested"


def price_choice_default(d: dict, detected: list[DetectedPriceSensor], chosen: str | None = None) -> str:
    """Return the price-sensor row to pre-select.

    Args:
        d: Existing config/options dict.
        detected: Detected sensors.
        chosen: The row the user just submitted, when the form is re-shown.

    Returns:
        ``chosen`` when given; else the stored sensor when it was detected,
        Other when a stored sensor was not, and the first detected import
        sensor when none is stored yet.
    """
    if chosen:
        return chosen
    stored = d.get(CONF_NORDPOOL_ENTITY)
    ids = [sensor.entity_id for sensor in detected if not sensor.export]
    if stored:
        return stored if stored in ids else PRICE_SENSOR_OTHER
    return ids[0] if ids else PRICE_SENSOR_OTHER


def export_choice_default(
    d: dict, detected: list[DetectedPriceSensor], import_choice: str, chosen: str | None = None,
) -> str:
    """Return the export-feed row to pre-select.

    Args:
        d: Existing config/options dict.
        detected: Detected sensors.
        import_choice: The price-sensor row pre-selected alongside.
        chosen: The row the user just submitted, when the form is re-shown.

    Returns:
        ``chosen`` when given; else the stored export sensor (None when it was
        stored empty); with nothing stored yet, the first export feed from the
        pre-selected price sensor's integration, or None.
    """
    if chosen:
        return chosen
    if CONF_PRICE_EXPORT_ENTITY in d:
        return d[CONF_PRICE_EXPORT_ENTITY] or PRICE_EXPORT_NONE
    source = next((sensor.source for sensor in detected if sensor.entity_id == import_choice), None)
    return next(
        (sensor.entity_id for sensor in detected if sensor.export and sensor.source == source), PRICE_EXPORT_NONE,
    )


def _export_options(d: dict, exports: list[DetectedPriceSensor]) -> list[dict[str, str]]:
    """Return the export-feed list's rows: the detected feeds, a stored by-hand sensor, None.

    A stored export sensor that is not detected gets its own row so confirming
    the form never drops it.
    """
    options = [{"value": sensor.entity_id, "label": detected_label(sensor)} for sensor in exports]
    stored = d.get(CONF_PRICE_EXPORT_ENTITY)
    if stored and stored not in {sensor.entity_id for sensor in exports}:
        options.append({"value": stored, "label": f"{stored} (picked by hand)"})
    return detected_options(options, none="None — a market sell price uses the price sensor")


def price_picker_schema(
    d: dict, detected: list[DetectedPriceSensor], chosen: str | None = None, chosen_export: str | None = None,
) -> vol.Schema:
    """Build the price-feed schema that offers the detected price sensors.

    Args:
        d: Existing config/options dict used for default values.
        detected: Detected sensors; at least one is an import sensor.
        chosen: The price-sensor row the user just submitted, when re-shown.
        chosen_export: The export-feed row the user just submitted, when re-shown.

    Returns:
        Schema with the detected price-sensor list (plus Other) instead of the
        source and sensor fields, then the detected export feeds (plus None) —
        or the by-hand export sensor when no feed was detected — then the
        resolution and currency.
    """
    options = detected_options(
        [{"value": sensor.entity_id, "label": detected_label(sensor)} for sensor in detected if not sensor.export],
        other="Other — choose the source and sensor on the next page",
    )
    import_default = price_choice_default(d, detected, chosen)
    fields = {vol.Required(PRICE_SENSOR_CHOICE, default=import_default): list_selector(options)}
    if exports := [sensor for sensor in detected if sensor.export]:
        export_default = export_choice_default(d, detected, import_default, chosen_export)
        fields[vol.Required(PRICE_EXPORT_CHOICE, default=export_default)] = list_selector(_export_options(d, exports))
    else:
        fields.update(_export_field(d))
    return vol.Schema({**fields, **_feed_setting_fields(d)})


def validate_export_choice(
    detected: list[DetectedPriceSensor], import_choice: str | None, export_choice: str | None,
) -> dict[str, str]:
    """Return an error when the picked export feed belongs to another integration than the price sensor.

    The translator reads the export feed only for its own integration, so a
    mismatched feed would be ignored silently.

    Args:
        detected: Detected sensors.
        import_choice: The submitted price-sensor row.
        export_choice: The submitted export-feed row.

    Returns:
        ``{PRICE_EXPORT_CHOICE: "export_feed_mismatch"}``, or empty when the
        pair fits (or either side was not a detected sensor).
    """
    sources = {sensor.entity_id: sensor.source for sensor in detected}
    export_source = sources.get(export_choice or "")
    import_source = sources.get(import_choice or "")
    if export_source is not None and import_source is not None and export_source != import_source:
        return {PRICE_EXPORT_CHOICE: "export_feed_mismatch"}
    return {}


def _count(n: int, noun: str) -> str:
    """Return ``"<n> <noun>"`` with the noun pluralised when n is not 1."""
    return f"{n} {noun}{'' if n == 1 else 's'}"


def detected_summary(detected: list[DetectedPriceSensor]) -> str:
    """Return the price-feed form's sentence about the price sensors found on this system."""
    exports = sum(sensor.export for sensor in detected)
    imports = len(detected) - exports
    if not imports:
        return "No price sensor sunSale can read was found on this system — choose the source and its sensor below."
    found = _count(imports, "price sensor") + (f" and {_count(exports, 'export-price feed')}" if exports else "")
    return f"Found {found} on this system — pick yours, or **Other** to choose the source and sensor yourself."


def validate_price_feed(user_input: dict, needs_sensor: bool) -> dict[str, str]:
    """Return field-keyed errors for the price-feed form.

    Args:
        user_input: Submitted price-feed values.
        needs_sensor: Whether the buy or sell price follows the market price.

    Returns:
        Mapping of field key to error code; empty when the page is valid.
    """
    errors: dict[str, str] = {}
    if needs_sensor and not user_input.get(CONF_NORDPOOL_ENTITY):
        errors[CONF_NORDPOOL_ENTITY] = "price_entity_required"
    currency = str(user_input.get(CONF_CURRENCY, DEFAULT_CURRENCY)).strip()
    if not (len(currency) == 3 and currency.isalpha()):
        errors[CONF_CURRENCY] = "invalid_currency"
    return errors


def weather_label(entity: DetectedWeather) -> str:
    """Return a detected weather entity's row in the price-forecast picker.

    Args:
        entity: The detected weather entity.

    Returns:
        ``"<name> (<entity id>)"`` — or just the entity id when it publishes no
        friendly name — marked when it is the one already used by default.
    """
    label = f"{entity.name} ({entity.entity_id})" if entity.name else entity.entity_id
    return f"{label} — in use now" if entity.auto else label


def price_forecast_schema(d: dict, detected: list[DetectedWeather]) -> vol.Schema:
    """Build the price-forecast schema: which weather entity the model reads.

    Args:
        d: Existing config/options dict used for the default value.
        detected: The weather entities on this system; with none the row
            degrades to a plain weather-entity picker.

    Returns:
        Schema with one row for the weather entity, pre-selected to the stored
        entity, else the one the translator already auto-detects. A *None* row
        turns the weather correction off, leaving the forecast on its own
        day-class climatology.
    """
    if not detected:
        return vol.Schema({
            vol.Optional(CONF_WEATHER_ENTITY, description=suggest(d.get(CONF_WEATHER_ENTITY))): WEATHER,
        })
    rows = [{"value": entity.entity_id, "label": weather_label(entity)} for entity in detected]
    stored = str(d.get(CONF_WEATHER_ENTITY) or "")
    known = {entity.entity_id for entity in detected}
    if stored and stored not in known:
        rows.append({"value": stored, "label": f"{stored} (picked by hand)"})
    default = stored or next((entity.entity_id for entity in detected if entity.auto), NONE)
    if CONF_WEATHER_ENTITY in d and not stored:
        # Stored empty means the user turned the weather correction off; that
        # must survive re-opening the page, so it is not re-filled from detection.
        default = NONE
    selector = detected_selector(rows, none="None — predict from this install's own history only")
    return vol.Schema({vol.Required(CONF_WEATHER_ENTITY, default=default): selector})


def weather_summary(detected: list[DetectedWeather]) -> str:
    """Return the price-forecast form's sentence about the weather entities found."""
    if not detected:
        return (
            "No weather entity was found on this system, so the forecast predicts from this "
            "install's own price history alone. Add a weather integration to improve it."
        )
    found = "1 weather entity" if len(detected) == 1 else f"{len(detected)} weather entities"
    return (
        f"Found {found} on this system — the one marked **in use now** is what the "
        "forecast already reads."
    )


def price_levels_schema(d: dict) -> vol.Schema:
    """Build the price-level (cheap / expensive) schema, pre-filled from d.

    Args:
        d: Existing config/options dict used for default values.

    Returns:
        Schema with the cheap / expensive day shares (%) and the optional
        absolute always-cheap / always-expensive buy-price limits. The limits
        use ``suggested_value`` so a cleared limit stays cleared.
    """
    return vol.Schema({
        req(CONF_PRICE_LEVEL_CHEAP_SHARE, d, DEFAULT_PRICE_LEVEL_SHARE_PCT): vol.Coerce(float),
        req(CONF_PRICE_LEVEL_EXPENSIVE_SHARE, d, DEFAULT_PRICE_LEVEL_SHARE_PCT): vol.Coerce(float),
        vol.Optional(
            CONF_PRICE_LEVEL_CHEAP_BELOW, description=suggest(d.get(CONF_PRICE_LEVEL_CHEAP_BELOW)),
        ): vol.Coerce(float),
        vol.Optional(
            CONF_PRICE_LEVEL_EXPENSIVE_ABOVE, description=suggest(d.get(CONF_PRICE_LEVEL_EXPENSIVE_ABOVE)),
        ): vol.Coerce(float),
    })


def validate_price_levels(user_input: dict) -> dict[str, str]:
    """Return field-keyed errors for the price-level form.

    Args:
        user_input: Submitted price-level values.

    Returns:
        Mapping of field key to error code; empty when the settings are usable.
    """
    errors: dict[str, str] = {}
    cheap = user_input.get(CONF_PRICE_LEVEL_CHEAP_SHARE, DEFAULT_PRICE_LEVEL_SHARE_PCT)
    expensive = user_input.get(CONF_PRICE_LEVEL_EXPENSIVE_SHARE, DEFAULT_PRICE_LEVEL_SHARE_PCT)
    for key, value in ((CONF_PRICE_LEVEL_CHEAP_SHARE, cheap), (CONF_PRICE_LEVEL_EXPENSIVE_SHARE, expensive)):
        if not 0.0 <= value <= 100.0:
            errors[key] = "invalid_level_share"
    if not errors and cheap + expensive > 100.0:
        errors[CONF_PRICE_LEVEL_EXPENSIVE_SHARE] = "level_shares_overlap"
    below = user_input.get(CONF_PRICE_LEVEL_CHEAP_BELOW)
    above = user_input.get(CONF_PRICE_LEVEL_EXPENSIVE_ABOVE)
    if below is not None and above is not None and below >= above:
        errors[CONF_PRICE_LEVEL_EXPENSIVE_ABOVE] = "level_limits_inverted"
    return errors
