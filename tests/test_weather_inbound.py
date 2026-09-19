"""Tests for the weather translator (``inbound/weather.py``).

Weather is an optional accuracy input to the week-ahead price forecast, so the
translator's contract is that it degrades quietly: every failure path must
yield empty data rather than raise, and unit normalisation must be right
because the model consumes km/h regardless of what the source publishes.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import WeatherForecastData
from custom_components.sun_sale.inbound.weather import (
    WeatherTranslator,
    detect_weather_entities,
    detect_weather_entity,
    parse_daily_forecast,
)


class _State:
    """Minimal HA state stub."""

    def __init__(self, entity_id: str, state: str = "sunny", **attributes) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes


class _States:
    """Minimal HA state-machine stub."""

    def __init__(self, states: list[_State]) -> None:
        self._by_id = {s.entity_id: s for s in states}

    def get(self, entity_id: str):
        """Return the state for an entity, or None."""
        return self._by_id.get(entity_id)

    def async_all(self, domain: str):
        """Return every state in a domain."""
        return [s for s in self._by_id.values() if s.entity_id.startswith(f"{domain}.")]


class _Services:
    """Service-call stub that returns a canned response or raises."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple] = []

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        """Record the call and return the canned response."""
        self.calls.append((domain, service, data))
        if self._error is not None:
            raise self._error
        return self._response


class _Hass:
    """Minimal HA stub exposing states and services."""

    def __init__(self, states: list[_State], services: _Services) -> None:
        self.states = _States(states)
        self.services = services


def _forecast_entry(day: str, wind: float = 20.0, temp: float = 15.0) -> dict:
    """Build one raw daily-forecast entry."""
    return {"datetime": f"{day}T09:00:00+00:00", "wind_speed": wind, "temperature": temp}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_normalises_wind_units():
    """Wind is converted to km/h from whatever the source publishes."""
    entries = [_forecast_entry("2026-08-01", wind=10.0)]
    assert parse_daily_forecast(entries, "km/h")[0].wind_speed_kmh == pytest.approx(10.0)
    assert parse_daily_forecast(entries, "m/s")[0].wind_speed_kmh == pytest.approx(36.0)
    assert parse_daily_forecast(entries, "mph")[0].wind_speed_kmh == pytest.approx(16.09344)
    # An unknown unit is assumed to already be the internal one rather than
    # discarding an otherwise usable reading.
    assert parse_daily_forecast(entries, "furlongs")[0].wind_speed_kmh == pytest.approx(10.0)


def test_parse_buckets_to_local_dates():
    """Entries are bucketed by local date so days align with the price forecast."""
    # 23:00 UTC is already the next day in a UTC+3 zone.
    entries = [{"datetime": "2026-08-01T23:00:00+00:00", "wind_speed": 5.0}]
    days = parse_daily_forecast(entries, "km/h", ZoneInfo("Europe/Vilnius"))
    assert days[0].day == date(2026, 8, 2)


def test_parse_keeps_first_entry_per_day_and_sorts():
    """Duplicate dates collapse to the first, and output is date-ordered."""
    entries = [
        _forecast_entry("2026-08-03", wind=3.0),
        _forecast_entry("2026-08-01", wind=1.0),
        {"datetime": "2026-08-01T21:00:00+00:00", "wind_speed": 99.0},
    ]
    days = parse_daily_forecast(entries, "km/h")
    assert [d.day for d in days] == [date(2026, 8, 1), date(2026, 8, 3)]
    assert days[0].wind_speed_kmh == pytest.approx(1.0)


def test_parse_skips_malformed_entries():
    """Entries without a usable timestamp are dropped, not fatal."""
    entries = [
        {"wind_speed": 5.0},                                  # no datetime
        {"datetime": "not-a-date", "wind_speed": 5.0},        # unparseable
        _forecast_entry("2026-08-01"),
    ]
    days = parse_daily_forecast(entries, "km/h")
    assert len(days) == 1


def test_parse_tolerates_missing_fields():
    """A source publishing no wind or temperature yields None, not an error."""
    days = parse_daily_forecast([{"datetime": "2026-08-01T09:00:00+00:00"}], "km/h")
    assert days[0].wind_speed_kmh is None
    assert days[0].temperature_c is None


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def test_configured_entity_wins():
    """An explicitly configured entity is used when it exists."""
    hass = _Hass([_State("weather.forecast_home"), _State("weather.custom")], _Services())
    assert detect_weather_entity(hass, "weather.custom") == "weather.custom"


def test_falls_back_to_preferred_then_any():
    """Auto-detection prefers the default entities, then any available one."""
    hass = _Hass([_State("weather.forecast_home"), _State("weather.other")], _Services())
    assert detect_weather_entity(hass, "") == "weather.forecast_home"

    hass = _Hass([_State("weather.somewhere_else")], _Services())
    assert detect_weather_entity(hass, "") == "weather.somewhere_else"


def test_missing_configured_entity_falls_back_rather_than_failing():
    """A stale configured entity does not disable the forecast."""
    hass = _Hass([_State("weather.forecast_home")], _Services())
    assert detect_weather_entity(hass, "weather.deleted") == "weather.forecast_home"


def test_no_weather_entity_returns_empty():
    """With no weather integration at all, resolution yields nothing."""
    assert detect_weather_entity(_Hass([], _Services()), "") == ""


def test_unavailable_entities_are_not_selected():
    """An unavailable entity is not a usable source."""
    hass = _Hass([_State("weather.broken", state="unavailable")], _Services())
    assert detect_weather_entity(hass, "") == ""


def test_the_candidate_list_offers_every_entity_with_the_one_in_use_first():
    """The setup page shows which entity the forecast already reads — it was invisible before."""
    hass = _Hass([
        _State("weather.zzz_last", friendly_name="Last"),
        _State("weather.forecast_home", friendly_name="Home"),
        _State("weather.aaa_first"),
    ], _Services())
    found = detect_weather_entities(hass)
    assert [(e.entity_id, e.name, e.auto) for e in found] == [
        ("weather.forecast_home", "Home", True),
        ("weather.aaa_first", "", False),
        ("weather.zzz_last", "Last", False),
    ]


def test_an_unavailable_entity_is_still_offered_but_never_marked_in_use():
    """It may be temporarily down; hiding it would stop the user picking it at all."""
    hass = _Hass([_State("weather.broken", state="unavailable")], _Services())
    found = detect_weather_entities(hass)
    assert [(e.entity_id, e.auto) for e in found] == [("weather.broken", False)]


def test_no_weather_integration_offers_nothing():
    assert detect_weather_entities(_Hass([], _Services())) == []
    assert detect_weather_entities(None) == []


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def test_fetch_returns_daily_rows():
    """A successful fetch yields one row per forecast day, tagged with its source."""
    response = {"weather.forecast_home": {"forecast": [
        _forecast_entry("2026-08-01"), _forecast_entry("2026-08-02"),
    ]}}
    hass = _Hass(
        [_State("weather.forecast_home", wind_speed_unit="m/s")],
        _Services(response=response),
    )
    data = await WeatherTranslator().fetch(hass)
    assert isinstance(data, WeatherForecastData)
    assert len(data.days) == 2
    assert data.source_entity == "weather.forecast_home"
    assert data.days[0].wind_speed_kmh == pytest.approx(72.0)   # 20 m/s → km/h


async def test_fetch_returns_empty_when_the_service_raises():
    """A service failure degrades to empty data rather than breaking the cycle."""
    hass = _Hass(
        [_State("weather.forecast_home")],
        _Services(error=RuntimeError("boom")),
    )
    data = await WeatherTranslator().fetch(hass)
    assert data.days == ()
    assert data.source_entity == ""


async def test_fetch_returns_empty_without_a_weather_entity():
    """No weather entity means no call is attempted."""
    services = _Services()
    data = await WeatherTranslator().fetch(_Hass([], services))
    assert data.days == ()
    assert services.calls == []


async def test_fetch_asks_for_the_daily_forecast():
    """The daily forecast is what the model needs; hourly is too short."""
    services = _Services(response={"weather.forecast_home": {"forecast": []}})
    hass = _Hass([_State("weather.forecast_home")], services)
    await WeatherTranslator().fetch(hass)
    assert services.calls[0][:2] == ("weather", "get_forecasts")
    assert services.calls[0][2]["type"] == "daily"
