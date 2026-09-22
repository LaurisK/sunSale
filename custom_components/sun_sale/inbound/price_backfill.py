"""Fetch settled day-ahead prices and past weather, so a new install starts warm.

The online shape engine (``pipeline/online_shape.py``) learns from settled days.
Left to itself a fresh install learns from the first one it sees, which means a
week before the weekend buckets have anything in them and a season before the
silhouettes stop drifting. Every day it is waiting for is public and free, so
this module fetches them once and replays them through the same learning loop
the engine runs live — a backfilled install and one that has been running for
two years end up in the same state.

Sources, both keyless:

* **Elering** ``nps/price`` for the Baltic zones and Finland, which is the same
  market data the install's own price sensor publishes.
* **Energy-Charts** ``price`` for the continental European bidding zones.
* **Open-Meteo previous-runs** for the weather **as it was forecast a day
  ahead**, which is the kind of input the engine is served. The realised-weather
  archive is the fallback when that fails.

This is the one place the integration talks to the internet, and it is
deliberately all-or-nothing and silent: a failure, a timeout, or a market this
module cannot map leaves the engine to cold-start, which is what it did before.
Nothing here is retried and nothing is scheduled — it runs once, at setup.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo

from ..contract.models import (
    OnlineShapeState,
    PriceCurveHistory,
    PriceDayRecord,
)
from ..pipeline import online_shape
from ..pipeline.profitability import classify_day

_LOGGER = logging.getLogger(__name__)

_ELERING = "https://dashboard.elering.ee/api/nps/price"
_ENERGY_CHARTS = "https://api.energy-charts.info/price"
_OPEN_METEO_VINTAGE = "https://previous-runs-api.open-meteo.com/v1/forecast"
_OPEN_METEO_ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"

#: Elering's zone codes, which cover the Baltics and Finland.
_ELERING_ZONES = {"LT": "lt", "LV": "lv", "EE": "ee", "FI": "fi"}

#: Energy-Charts bidding zones that a country code identifies unambiguously.
#: Sweden, Norway, Denmark and Italy are split into several zones, so a country
#: is not enough to choose one and they are left to cold-start.
_ENERGY_CHARTS_ZONES = {
    "DE": "DE-LU", "PL": "PL", "NL": "NL", "BE": "BE", "FR": "FR",
    "ES": "ES", "PT": "PT", "AT": "AT", "CH": "CH", "CZ": "CZ",
    "SK": "SK", "HU": "HU", "SI": "SI", "HR": "HR", "RO": "RO",
    "BG": "BG", "GR": "GR", "IE": "IE",
}

#: How much history to fetch. Two years covers both a full seasonal cycle and
#: the day-of-year neighbourhood the silhouettes settle into.
DEFAULT_DAYS = 730

#: Total seconds the whole backfill may take before it is abandoned. Setup
#: waits for this, so it is sized to be tolerable on a slow connection rather
#: than generous.
TIMEOUT_S = 90.0

#: Days per price request. Keeps each response small enough that one failure
#: is cheap and no single parse holds the loop for long.
_PRICE_CHUNK_DAYS = 180


def zone_for(country: str | None) -> tuple[str, str] | None:
    """Return the price source and zone code for a country.

    Args:
        country: ISO-3166 alpha-2 code, typically Home Assistant's own.

    Returns:
        ``("elering", "lt")`` or ``("energy_charts", "PL")``, or None when the
        country has several bidding zones or no covered source.
    """
    code = (country or "").upper()
    if code in _ELERING_ZONES:
        return "elering", _ELERING_ZONES[code]
    if code in _ENERGY_CHARTS_ZONES:
        return "energy_charts", _ENERGY_CHARTS_ZONES[code]
    return None


async def _get_json(session, url: str, timeout: float = 30.0) -> dict | list | None:
    """Fetch one JSON document, returning None on any failure.

    Args:
        session: An aiohttp client session.
        url: Fully-formed request URL.
        timeout: Seconds to wait.

    Returns:
        The decoded body, or None.
    """
    try:
        async with session.get(url, timeout=timeout) as response:
            if response.status != 200:
                _LOGGER.debug("Backfill %s returned HTTP %s", url, response.status)
                return None
            return await response.json(content_type=None)
    except (TimeoutError, asyncio.TimeoutError, OSError, ValueError) as err:
        _LOGGER.debug("Backfill %s failed: %s", url, err)
        return None


def _chunks(start: date, end: date, days: int):
    """Yield (from, to) date windows covering the range inclusively."""
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


async def fetch_prices(
    session,
    source: str,
    zone: str,
    start: date,
    end: date,
    local_tz: tzinfo,
) -> dict[date, list[float]]:
    """Fetch settled hourly prices and group them into local days.

    Args:
        session: An aiohttp client session.
        source: ``elering`` or ``energy_charts``.
        zone: Zone code for that source.
        start: First local date wanted.
        end: Last local date wanted.
        local_tz: Local timezone the days are cut on.

    Returns:
        Local date → 24 hourly prices in EUR/kWh, only for complete days.
    """
    hourly: dict[date, dict[int, list[float]]] = {}
    for window_start, window_end in _chunks(start, end, _PRICE_CHUNK_DAYS):
        if source == "elering":
            url = (f"{_ELERING}?start={window_start.isoformat()}T00:00:00.000Z"
                   f"&end={window_end.isoformat()}T23:59:59.999Z")
            body = await _get_json(session, url)
            rows = (body or {}).get("data", {}).get(zone, []) if isinstance(body, dict) else []
            points = [(int(r["timestamp"]), float(r["price"])) for r in rows
                      if r.get("price") is not None]
        else:
            url = (f"{_ENERGY_CHARTS}?bzn={zone}"
                   f"&start={window_start.isoformat()}&end={window_end.isoformat()}")
            body = await _get_json(session, url)
            stamps = (body or {}).get("unix_seconds", []) if isinstance(body, dict) else []
            prices = (body or {}).get("price", []) if isinstance(body, dict) else []
            points = [(int(t), float(p)) for t, p in zip(stamps, prices) if p is not None]
        for stamp, price in points:
            when = datetime.fromtimestamp(stamp, timezone.utc).astimezone(local_tz)
            # Both feeds quote EUR/MWh; the integration works in EUR/kWh.
            hourly.setdefault(when.date(), {}).setdefault(when.hour, []).append(price / 1000.0)
        # Give the event loop air between chunks; each parse is thousands of rows.
        await asyncio.sleep(0)

    out: dict[date, list[float]] = {}
    for day, hours in hourly.items():
        if len(hours) != online_shape.HOURS:
            continue
        out[day] = [sum(hours[h]) / len(hours[h]) for h in range(online_shape.HOURS)]
    return out


async def fetch_weather(
    session,
    latitude: float,
    longitude: float,
    start: date,
    end: date,
) -> dict[date, tuple[float | None, float | None, float | None]]:
    """Fetch daily wind, temperature and cloud cover for the backfill window.

    The day-ahead vintage is asked for first: the engine is served forecasts,
    so it should be trained on forecasts. The realised archive stands in when
    that endpoint has no data for the period.

    Args:
        session: An aiohttp client session.
        latitude: Site latitude.
        longitude: Site longitude.
        start: First local date wanted.
        end: Last local date wanted.

    Returns:
        Local date → (wind km/h, temperature °C, cloud cover %).
    """
    for host, suffix in ((_OPEN_METEO_VINTAGE, "_previous_day1"), (_OPEN_METEO_ARCHIVE, "")):
        fields = [f"wind_speed_10m{suffix}", f"temperature_2m{suffix}",
                  f"cloud_cover{suffix}"]
        url = (f"{host}?latitude={latitude}&longitude={longitude}"
               f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
               f"&hourly={','.join(fields)}&timezone=auto")
        body = await _get_json(session, url, timeout=45.0)
        block = (body or {}).get("hourly", {}) if isinstance(body, dict) else {}
        stamps = block.get("time", [])
        if not stamps:
            continue
        sums: dict[date, list[list[float]]] = {}
        for index, stamp in enumerate(stamps):
            try:
                day = datetime.fromisoformat(stamp).date()
            except ValueError:
                continue
            bucket = sums.setdefault(day, [[], [], []])
            for slot, field in enumerate(fields):
                values = block.get(field) or []
                if index < len(values) and values[index] is not None:
                    bucket[slot].append(float(values[index]))
        if sums:
            return {
                day: tuple(
                    (sum(values) / len(values)) if values else None
                    for values in buckets
                )
                for day, buckets in sums.items()
            }
    return {}


def build_history(
    curves: dict[date, list[float]],
    weather: dict[date, tuple[float | None, float | None, float | None]],
    local_tz: tzinfo,
    latitude: float,
    longitude: float,
    negative_threshold_eur_kwh: float,
    is_holiday: Callable[[date], bool] | None = None,
    neighbour_share: Callable[[date], float] | None = None,
) -> PriceCurveHistory:
    """Turn fetched days into records and learn the engine's state from them.

    Args:
        curves: Local date → 24 hourly prices.
        weather: Local date → (wind km/h, temperature °C, cloud %).
        local_tz: Local timezone.
        latitude: Site latitude.
        longitude: Site longitude.
        negative_threshold_eur_kwh: Export break-even spot price.
        is_holiday: Local public-holiday predicate.
        neighbour_share: Share of neighbouring zones on holiday.

    Returns:
        A history whose records carry curves and whose state has seen them all.
    """
    records: list[PriceDayRecord] = []
    state: OnlineShapeState = online_shape.initial_state()
    for day in sorted(curves):
        curve = curves[day]
        wind, temperature, cloud = weather.get(day, (None, None, None))
        ordered = sorted(curve)
        holiday = bool(is_holiday(day)) if is_holiday is not None else False
        records.append(PriceDayRecord(
            day=day,
            day_class=classify_day(day, is_holiday),
            peak_1h_eur_kwh=ordered[-1],
            peak_4h_eur_kwh=sum(ordered[-4:]) / 4.0,
            trough_1h_eur_kwh=ordered[0],
            trough_4h_eur_kwh=sum(ordered[:4]) / 4.0,
            negative_hours=float(sum(1 for v in ordered
                                     if v <= negative_threshold_eur_kwh)),
            mean_eur_kwh=sum(curve) / len(curve),
            wind_speed_kmh=wind,
            temperature_c=temperature,
            cloud_coverage_pct=cloud,
            curve=tuple(curve),
        ))
        state = online_shape.absorb(
            state,
            online_shape.DayInputs(
                day=day,
                bucket=online_shape.bucket_of(day, holiday),
                wind_index=online_shape.wind_power_index(wind),
                solar_profile=online_shape.solar_profile(
                    day, latitude, longitude, local_tz, cloud,
                ),
                temperature_c=temperature,
                is_holiday=holiday and day.weekday() < 5,
                neighbour_share=(neighbour_share(day) if neighbour_share else 0.0),
            ),
            curve,
        )
    return PriceCurveHistory(records=tuple(records), shape_state=state)


async def backfill(
    session,
    *,
    country: str | None,
    latitude: float,
    longitude: float,
    local_tz: tzinfo,
    today: date,
    negative_threshold_eur_kwh: float,
    days: int = DEFAULT_DAYS,
    is_holiday: Callable[[date], bool] | None = None,
    neighbour_share: Callable[[date], float] | None = None,
) -> PriceCurveHistory | None:
    """Fetch history and return a warm store, or None when it cannot be had.

    Args:
        session: An aiohttp client session.
        country: ISO-3166 alpha-2 code used to pick the market.
        latitude: Site latitude.
        longitude: Site longitude.
        local_tz: Local timezone.
        today: Local date; the window ends yesterday.
        negative_threshold_eur_kwh: Export break-even spot price.
        days: How much history to ask for.
        is_holiday: Local public-holiday predicate.
        neighbour_share: Share of neighbouring zones on holiday.

    Returns:
        A populated history, or None when the market is not covered, the
        network fails, or too few complete days came back to be worth storing.
    """
    market = zone_for(country)
    if market is None:
        _LOGGER.debug("Price backfill skipped: no mapped market for country %r", country)
        return None
    source, zone = market
    end = today - timedelta(days=1)
    start = end - timedelta(days=days)
    curves = await fetch_prices(session, source, zone, start, end, local_tz)
    if len(curves) < 30:
        _LOGGER.debug("Price backfill skipped: only %d complete days", len(curves))
        return None
    weather = await fetch_weather(session, latitude, longitude, start, end)
    history = build_history(
        curves, weather, local_tz, latitude, longitude,
        negative_threshold_eur_kwh, is_holiday, neighbour_share,
    )
    _LOGGER.info(
        "Price backfill: %d settled days from %s/%s, weather for %d of them",
        len(history.records), source, zone, len(weather),
    )
    return history
