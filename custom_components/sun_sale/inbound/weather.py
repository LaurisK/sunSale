"""Weather stage: daily forecast reader feeding the week-ahead price forecast.

The price model needs one row of weather per forecast day — wind, temperature
and (where published) cloud cover — out to roughly a week. That is exactly what
a Home Assistant ``weather`` entity's **daily** forecast provides, so this
translator reads one rather than calling any external API. Three consequences,
all deliberate:

* No new cloud dependency and no API key, so the integration keeps its
  ``local_polling`` character and works wherever the user's existing weather
  integration works — which is the whole world, not one market.
* Whatever weather source the user already trusts is the one used.
* The horizon is whatever that integration publishes (met.no gives six days).
  A short horizon is not an error: days beyond it simply fall back to the
  climatology baseline, which is the same graceful degradation the price
  forecast applies when the model has no skill.

Hourly forecasts are deliberately not used. The published targets are daily
statistics, the study behind them was fitted on daily means, and hourly
coverage is typically only ~48 h — far short of the week this feeds.

Entity selection prefers the explicitly configured entity, then auto-detects.
Auto-detection exists because requiring configuration for an optional accuracy
improvement would leave most installs on the baseline forever.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from typing import Any

from ..contract.models import WeatherDaily, WeatherForecastData

_LOGGER = logging.getLogger(__name__)

# Service used to pull a forecast from a weather entity.
_WEATHER_DOMAIN = "weather"
_FORECAST_SERVICE = "get_forecasts"

# Preferred auto-detect candidates, in order. These are the entity IDs Home
# Assistant creates for the default met.no setup; anything else falls through
# to "first weather entity that reports a state".
_PREFERRED_ENTITIES: tuple[str, ...] = (
    "weather.forecast_home",
    "weather.home",
)

# Wind speed is published in whatever unit the source uses; normalise to km/h.
_WIND_UNIT_SCALERS: dict[str, float] = {
    "km/h": 1.0,
    "kmh": 1.0,
    "m/s": 3.6,
    "mph": 1.609344,
    "kn": 1.852,
}


def detect_weather_entity(hass: Any, configured: str = "") -> str:
    """Return the weather entity to read, or an empty string when none exists.

    Args:
        hass: Home Assistant instance.
        configured: Explicitly configured entity ID; wins whenever it is
            present in the state machine.

    Returns:
        An entity ID, or "" when no usable weather entity is available.
    """
    if configured and hass.states.get(configured) is not None:
        return configured
    if configured:
        _LOGGER.debug("Configured weather entity %s not found; auto-detecting", configured)

    for candidate in _PREFERRED_ENTITIES:
        if hass.states.get(candidate) is not None:
            return candidate

    for state in hass.states.async_all(_WEATHER_DOMAIN):
        if state.state not in ("unavailable", "unknown", "", None):
            return state.entity_id
    return ""


def _to_kmh(value: Any, unit: Any) -> float | None:
    """Convert a wind speed in the source's unit to km/h.

    Args:
        value: Raw wind speed.
        unit: Unit string from the weather entity's attributes.

    Returns:
        Speed in km/h, or None when the value is not numeric.
    """
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    return raw * _WIND_UNIT_SCALERS.get(str(unit or "").strip(), 1.0)


def _as_float(value: Any) -> float | None:
    """Return value as a float, or None when it is missing or non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_daily_forecast(
    entries: list[dict],
    wind_unit: Any = None,
    local_tz: tzinfo | None = None,
) -> tuple[WeatherDaily, ...]:
    """Turn a weather integration's daily forecast list into WeatherDaily rows.

    Args:
        entries: Raw forecast dicts as returned by ``weather.get_forecasts``.
        wind_unit: The entity's ``wind_speed_unit`` attribute, used to
            normalise to km/h.
        local_tz: Timezone used to bucket each entry to a local date, so the
            days line up with the price forecast's local-midnight boundaries.

    Returns:
        Daily rows sorted ascending by date, one per date; later duplicates for
        the same date are ignored.
    """
    tz = local_tz or UTC
    seen: dict[Any, WeatherDaily] = {}
    for entry in entries:
        stamp = entry.get("datetime")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        day = when.astimezone(tz).date()
        if day in seen:
            continue
        seen[day] = WeatherDaily(
            day=day,
            wind_speed_kmh=_to_kmh(entry.get("wind_speed"), wind_unit),
            temperature_c=_as_float(entry.get("temperature")),
            cloud_coverage_pct=_as_float(entry.get("cloud_coverage")),
        )
    return tuple(seen[d] for d in sorted(seen))


class WeatherTranslator:
    """Reads a HA weather entity's daily forecast; produces WeatherForecastData.

    The forecast is fetched through the ``weather.get_forecasts`` service
    because daily forecasts are response data rather than entity attributes.
    Every failure path yields empty data rather than raising: weather is an
    optional accuracy input, and the price forecast is required to work without
    it.
    """

    output_type = WeatherForecastData

    def __init__(self, configured_entity: str = "") -> None:
        """Initialise with the optionally-configured weather entity ID.

        Args:
            configured_entity: Entity ID from config; blank enables
                auto-detection.
        """
        self._configured = configured_entity or ""
        self._resolved = ""

    @property
    def resolved_entity(self) -> str:
        """Return the entity resolved on the most recent fetch."""
        return self._resolved

    async def fetch(
        self,
        hass: Any,
        local_tz: tzinfo | None = None,
    ) -> WeatherForecastData:
        """Fetch and translate the daily forecast.

        Args:
            hass: Home Assistant instance.
            local_tz: Local timezone used to bucket entries to dates.

        Returns:
            WeatherForecastData; empty when no weather entity is available or
            the service call fails.
        """
        entity_id = detect_weather_entity(hass, self._configured)
        self._resolved = entity_id
        if not entity_id:
            return WeatherForecastData()

        try:
            response = await hass.services.async_call(
                _WEATHER_DOMAIN,
                _FORECAST_SERVICE,
                {"entity_id": entity_id, "type": "daily"},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - optional input, never fatal
            _LOGGER.debug("Weather forecast unavailable from %s: %s", entity_id, err)
            return WeatherForecastData()

        entries = ((response or {}).get(entity_id) or {}).get("forecast") or []
        if not entries:
            _LOGGER.debug("Weather entity %s returned no daily forecast", entity_id)
            return WeatherForecastData()

        state = hass.states.get(entity_id)
        wind_unit = state.attributes.get("wind_speed_unit") if state else None
        days = parse_daily_forecast(list(entries), wind_unit, local_tz)
        return WeatherForecastData(days=days, source_entity=entity_id)
