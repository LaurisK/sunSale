"""Pricing stage: price-feed HA-state readers + 72h PriceSeries assembly.

A *price-feed translator* reads a market-price sensor and produces
source-agnostic ``PriceFeedData`` (today + tomorrow, with tomorrow zero-filled
until published). One translator exists per supported market —
``NordpoolTranslator`` (Nordics), ``EntsoeTranslator`` (EU day-ahead),
``OctopusAgileTranslator`` (UK) and ``AmberTranslator`` (AU) — plus
``FixedPriceTranslator``, the zero-spot slot grid used when neither the buy
nor the sell price follows the market, all emitting the identical
``PriceFeedData`` shape. ``build_price_translator`` selects one from the
effective price source. The ``build_price_series*`` functions then apply the
tariff formulas and stitch in persisted yesterday entries to produce the full
72h PriceSeries.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, NamedTuple, Protocol

from ..contract.const import (
    PRICE_SOURCE_AMBER,
    PRICE_SOURCE_ENTSOE,
    PRICE_SOURCE_FIXED,
    PRICE_SOURCE_NORDPOOL,
    PRICE_SOURCE_OCTOPUS,
    PRICE_SOURCES,
)
from ..contract.models import (
    NordpoolData,
    PriceEntry,
    PriceFeedData,
    PriceSeries,
    PriceSlot,
    SunSaleConfig,
    TariffConfig,
    YesterdayPrices,
)
from ..pipeline import tariff as tariff_module
from ..pipeline.tariff import HolidayPredicate

_LOGGER = logging.getLogger(__name__)


def build_price_series(
    prices: list[PriceEntry],
    config: TariffConfig,
    now: datetime | None = None,
    resolution: timedelta | None = None,
    local_tz: tzinfo | None = None,
    source: str = "nordpool",
    is_holiday: HolidayPredicate | None = None,
) -> PriceSeries:
    """Apply tariff formulas to price-feed entries and return a PriceSeries.

    If resolution is provided it is recorded verbatim; otherwise it is
    derived from the first two slots (or defaults to 1h for single-slot input).

    Args:
        prices: Sorted price-feed entries.
        config: User-configured tariff parameters.
        now: Cycle timestamp for computed_at; defaults to UTC now.
        resolution: Slot resolution override; auto-detected from data when None.
        local_tz: HA local timezone used to project each slot start to a
            local time-of-day for grid-fee tariff selection. When None every
            slot gets tariff T1.
        source: Price-source identifier recorded in each slot's provenance tag.
        is_holiday: Public-holiday predicate for the holiday schedules; None
            treats no day as a holiday.

    Returns:
        PriceSeries with buy/sell/spot populated for each entry.
    """
    if now is None:
        now = datetime.now(UTC)

    slots: list[PriceSlot] = []
    for p in prices:
        local_start = p.start.astimezone(local_tz) if local_tz is not None else None
        buy = tariff_module.buy_price(p.price_eur_kwh, config, local_start, is_holiday)
        sell = tariff_module.sell_price(
            p.price_eur_kwh, config, local_start, export=p.export_price_eur_kwh, is_holiday=is_holiday,
        )
        slots.append(PriceSlot(
            start=p.start,
            end=p.end,
            buy_eur_kwh=buy,
            sell_eur_kwh=sell,
            spot_eur_kwh=p.price_eur_kwh,
            sources=(source, "tariff") if p.priced else (source, "tariff", "unpriced"),
            export_eur_kwh=p.export_price_eur_kwh,
            priced=p.priced,
        ))

    if resolution is None:
        resolution = (slots[1].start - slots[0].start) if len(slots) >= 2 else timedelta(hours=1)

    return PriceSeries(
        slots=tuple(slots),
        resolution=resolution,
        computed_at=now,
    )


def build_price_series_72h(
    feed: PriceFeedData,
    yesterday: YesterdayPrices,
    config: TariffConfig,
    now: datetime | None = None,
    local_tz: tzinfo | None = None,
    source: str = "nordpool",
    is_holiday: HolidayPredicate | None = None,
) -> PriceSeries:
    """Assemble the 72h yesterday→today→tomorrow PriceSeries with tariff applied.

    Combines persisted yesterday entries with today+tomorrow from the price-feed
    translator, then pads the result to the full local yesterday→tomorrow window
    (see :func:`_fill_grid`) so the grid every other series is resampled onto
    survives a feed outage. Resolution comes from the translator while it has
    data, and is re-derived from the entries otherwise
    (see :func:`_effective_resolution`).

    Args:
        feed: Today + tomorrow entries from a price-feed translator.
        yesterday: Persisted yesterday entries from the coordinator store.
        config: User-configured tariff parameters.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: HA local timezone for tariff selection; see
            :func:`build_price_series`.
        source: Price-source identifier for provenance.
        is_holiday: Public-holiday predicate; see :func:`build_price_series`.

    Returns:
        PriceSeries spanning yesterday 00:00 → tomorrow 23:59, with slots the
        feed had no price for marked ``priced=False``.
    """
    if now is None:
        now = datetime.now(UTC)
    combined = sorted(
        list(yesterday.entries) + list(feed.entries), key=lambda e: e.start
    )
    resolution = _effective_resolution(feed, combined)
    gridded = _fill_grid(combined, resolution, now, local_tz)
    return build_price_series(
        gridded, config, now=now, resolution=resolution,
        local_tz=local_tz, source=source, is_holiday=is_holiday,
    )


def _effective_resolution(
    feed: PriceFeedData, combined: Sequence[PriceEntry]
) -> timedelta:
    """Return the slot resolution the assembled series actually uses.

    ``feed.resolution`` is authoritative while the feed has data — the
    translator detects it from the sensor. With an empty feed (sensor missing
    or unavailable) that value is the translator's 1h default, which would
    misdescribe a 15-min series stitched from persisted yesterday entries and
    silently quadruple every ``kW × slot_hours`` energy downstream, so the
    spacing is re-derived from the entries that do exist.

    Args:
        feed: Translator output for today + tomorrow.
        combined: Yesterday + feed entries, sorted by start.

    Returns:
        The slot duration to record on the series.
    """
    if feed.entries or len(combined) < 2:
        return feed.resolution
    return combined[1].start - combined[0].start


def _fill_grid(
    entries: Sequence[PriceEntry],
    resolution: timedelta,
    now: datetime,
    local_tz: tzinfo | None,
) -> list[PriceEntry]:
    """Pad the entry list with unpriced placeholders across the full 72h window.

    The PriceSeries slot grid is what the generation and observed series are
    resampled onto, so a feed outage would otherwise shrink every one of them
    to whatever days the feed still covers — on a dead sensor, yesterday alone.
    Placeholders keep the grid spanning local yesterday 00:00 → tomorrow 24:00;
    they carry ``priced=False`` so no consumer trades or bills on their filler
    zero.

    Args:
        entries: Real price entries, sorted by start; may be empty.
        resolution: Slot duration to step the placeholder grid by.
        now: Cycle timestamp, used to locate the local window.
        local_tz: HA local timezone; UTC when None.

    Returns:
        A sorted list of entries covering the window, real where known.
    """
    if resolution <= timedelta(0):
        return list(entries)

    tz = local_tz or UTC
    local_midnight = now.astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    window_start = (local_midnight - timedelta(days=1)).astimezone(UTC)
    window_end = (local_midnight + timedelta(days=2)).astimezone(UTC)

    def placeholder(start: datetime) -> PriceEntry:
        """Build one zero-filler entry marked as carrying no known price."""
        return PriceEntry(
            start=start, end=start + resolution, price_eur_kwh=0.0, priced=False,
        )

    out: list[PriceEntry] = []
    cursor = window_start
    for entry in entries:
        while cursor + resolution <= entry.start:
            out.append(placeholder(cursor))
            cursor += resolution
        out.append(entry)
        cursor = max(cursor, entry.end)
    while cursor + resolution <= window_end:
        out.append(placeholder(cursor))
        cursor += resolution
    return out


# ---------------------------------------------------------------------------
# Nordpool translator (HA-edge reader)
# ---------------------------------------------------------------------------

def _zero_fill_tomorrow(
    entries: list[PriceEntry], resolution: timedelta, now: datetime
) -> list[PriceEntry]:
    """Extend the entry list with zero-price stubs so the series spans 48h from its start.

    Nordpool reports in local time; deriving "tomorrow" from a UTC date can leave
    a gap when the local day starts before UTC midnight. Filling forward from the
    last entry's end to first_start + 48h is timezone- and resolution-agnostic.

    The stubs are marked ``priced=False``: until the day-ahead auction
    publishes, tomorrow's price is genuinely unknown, and a filler zero is not
    a forecast of it. Leaving them priced let the optimiser plan two thirds of
    its horizon against a fabricated 0.00 €/kWh spot.

    Args:
        entries: Existing price entries (must not be empty).
        resolution: Slot duration to use for stub entries.
        now: Unused; kept for signature compatibility.

    Returns:
        entries extended with unpriced PriceEntry stubs up to 48h coverage.
    """
    if not entries:
        return entries
    target_end = entries[0].start + timedelta(hours=48)
    last_end = max(e.end for e in entries)
    fill: list[PriceEntry] = []
    cur = last_end
    while cur < target_end:
        fill.append(PriceEntry(
            start=cur,
            end=cur + resolution,
            price_eur_kwh=0.0,
            priced=False,
        ))
        cur += resolution
    return entries + fill


class NordpoolTranslator:
    """Reads Nordpool sensor; produces NordpoolData for today + tomorrow.

    Resolution is auto-detected from the sensor data (15min or 1h).
    Tomorrow entries are zero-filled when not yet published.
    Coordinator prepends yesterday entries from persistent store.
    """

    output_type = NordpoolData

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the Nordpool sensor.

        Args:
            entity_id: HA entity ID (e.g. "sensor.nordpool_kwh_lt_eur_3_10_025").
        """
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> NordpoolData:
        """Parse the Nordpool HA sensor state into NordpoolData (today + tomorrow).

        Synchronous; callable directly from tests.

        Args:
            hass: Home Assistant instance.
            now: Reference time for zero-fill logic; defaults to UTC now.

        Returns:
            NordpoolData with today + tomorrow entries zero-filled to 48h.
            Returns an empty NordpoolData on missing or unparseable state.
        """
        if now is None:
            now = datetime.now(UTC)

        state = hass.states.get(self._entity_id)
        if state is None:
            _LOGGER.warning("Nordpool entity '%s' not found", self._entity_id)
            return NordpoolData(entries=[], resolution=timedelta(hours=1))

        raw_entries: list[dict] = []
        for attr_key in ("raw_today", "raw_tomorrow"):
            raw = state.attributes.get(attr_key)
            if isinstance(raw, list):
                raw_entries.extend(raw)

        if raw_entries:
            return self._parse_raw_entries(raw_entries, now)

        return self._parse_legacy(state, now)

    def _parse_raw_entries(self, raw_entries: list[dict], now: datetime) -> NordpoolData:
        """Parse the modern raw_today/raw_tomorrow dict-list format.

        Args:
            raw_entries: Combined list of {"start": …, "value": …} dicts.
            now: Reference time for zero-fill.

        Returns:
            NordpoolData with auto-detected resolution and 48h zero-fill.
        """
        parsed: list[tuple[datetime, float]] = []
        for entry in raw_entries:
            try:
                sv = entry["start"]
                dt = sv if isinstance(sv, datetime) else datetime.fromisoformat(str(sv))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                start_utc = dt.astimezone(UTC).replace(second=0, microsecond=0)
                parsed.append((start_utc, float(entry["value"])))
            except (KeyError, ValueError, TypeError):
                continue

        seen: set[datetime] = set()
        unique: list[tuple[datetime, float]] = []
        for item in sorted(parsed):
            if item[0] not in seen:
                seen.add(item[0])
                unique.append(item)

        if not unique:
            return NordpoolData(entries=[], resolution=timedelta(hours=1))

        resolution = (unique[1][0] - unique[0][0]) if len(unique) >= 2 else timedelta(hours=1)
        entries = [PriceEntry(start=s, end=s + resolution, price_eur_kwh=p) for s, p in unique]
        entries = _zero_fill_tomorrow(entries, resolution, now)
        return NordpoolData(entries=entries, resolution=resolution)

    def _parse_legacy(self, state: Any, now: datetime) -> NordpoolData:
        """Parse the legacy Nordpool sensor format (flat list of up to 24 hourly prices).

        Args:
            state: HA state object with today/tomorrow attributes.
            now: Reference time for deriving base dates and zero-fill.

        Returns:
            NordpoolData at 1h resolution with 48h zero-fill.
        """
        resolution = timedelta(hours=1)
        entries: list[PriceEntry] = []
        for offset, attr_key in enumerate(("today", "tomorrow")):
            raw = state.attributes.get(attr_key)
            if not isinstance(raw, list):
                continue
            base_date = (now + timedelta(days=offset)).date()
            for hour_idx, price in enumerate(raw):
                if price is None or hour_idx >= 24:
                    continue
                start = datetime(
                    base_date.year, base_date.month, base_date.day,
                    hour_idx, 0, 0, tzinfo=UTC,
                )
                entries.append(PriceEntry(start=start, end=start + resolution, price_eur_kwh=float(price)))

        if not entries:
            return NordpoolData(entries=[], resolution=resolution)
        entries = _zero_fill_tomorrow(entries, resolution, now)
        return NordpoolData(entries=entries, resolution=resolution)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> NordpoolData:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            PriceFeedData for today + tomorrow.
        """
        return self.parse(hass, now)


# ---------------------------------------------------------------------------
# Price-feed translator protocol + shared assembly helpers
# ---------------------------------------------------------------------------

class PriceFeedTranslator(Protocol):
    """Common surface of every price-feed translator.

    Each implementation reads its market's price sensor (or, for the synthetic
    TOU source, no sensor) and produces source-agnostic ``PriceFeedData``.
    """

    output_type: type[PriceFeedData]

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Read HA state synchronously and return PriceFeedData."""
        ...

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PriceFeedData:
        """DAG translator entry-point."""
        ...


def _to_utc_minute(value: Any) -> datetime | None:
    """Parse an ISO string / datetime to a UTC datetime truncated to the minute.

    Args:
        value: An ISO-8601 string or datetime; naive values are assumed UTC.

    Returns:
        A timezone-aware UTC datetime with seconds/micros zeroed, or None when
        unparseable.
    """
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


def _build_feed(
    import_points: list[tuple[datetime, float]],
    now: datetime,
    export_by_start: dict[datetime, float] | None = None,
    default_resolution: timedelta = timedelta(hours=1),
) -> PriceFeedData:
    """Assemble PriceFeedData from parsed (start, price) points.

    Dedups by start timestamp, auto-detects resolution from the first gap,
    aligns any per-start export prices, and zero-fills forward to 48h coverage.

    Args:
        import_points: List of (UTC start, import price) tuples (any order).
        now: Reference time forwarded to the zero-fill.
        export_by_start: Optional map of UTC start → export price, aligned by
            exact start match (unmatched starts get no export price).
        default_resolution: Resolution to use when fewer than two points exist.

    Returns:
        PriceFeedData with entries sorted by start and zero-filled to 48h.
    """
    seen: set[datetime] = set()
    unique: list[tuple[datetime, float]] = []
    for start, price in sorted(import_points):
        if start not in seen:
            seen.add(start)
            unique.append((start, price))

    if not unique:
        return PriceFeedData(entries=[], resolution=default_resolution)

    resolution = (unique[1][0] - unique[0][0]) if len(unique) >= 2 else default_resolution
    entries = [
        PriceEntry(
            start=start,
            end=start + resolution,
            price_eur_kwh=price,
            export_price_eur_kwh=(export_by_start or {}).get(start),
        )
        for start, price in unique
    ]
    entries = _zero_fill_tomorrow(entries, resolution, now)
    return PriceFeedData(entries=entries, resolution=resolution)


def _parse_points(
    raw: Any, time_keys: Sequence[str], value_keys: Sequence[str], scale: float = 1.0
) -> list[tuple[datetime, float]]:
    """Parse a list of price dicts into (UTC start, scaled price) tuples.

    Args:
        raw: A list of dicts; non-list input yields an empty result.
        time_keys: Candidate keys holding the slot start (first present wins).
        value_keys: Candidate keys holding the price (first present wins).
        scale: Multiplier applied to each raw price (e.g. 0.01 for cents→unit).

    Returns:
        List of (start, price) tuples; malformed rows are skipped.
    """
    points: list[tuple[datetime, float]] = []
    if not isinstance(raw, list):
        return points
    for row in raw:
        if not isinstance(row, dict):
            continue
        start = next((_to_utc_minute(row[k]) for k in time_keys if k in row), None)
        if start is None:
            continue
        raw_val = next((row[k] for k in value_keys if k in row), None)
        if raw_val is None:
            continue
        try:
            points.append((start, float(raw_val) * scale))
        except (TypeError, ValueError):
            continue
    return points


class EntsoeTranslator:
    """Reads an ENTSO-e price sensor; produces PriceFeedData for the EU market.

    Targets the ENTSO-e custom integration, whose sensor exposes
    ``prices_today`` / ``prices_tomorrow`` (and a combined ``prices``) as lists
    of ``{"time": iso, "price": float}`` in the configured currency per kWh.
    """

    output_type = PriceFeedData

    def __init__(self, entity_id: str) -> None:
        """Initialise with the ENTSO-e price sensor entity ID."""
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Parse the ENTSO-e sensor state into PriceFeedData (today + tomorrow)."""
        if now is None:
            now = datetime.now(UTC)
        state = hass.states.get(self._entity_id)
        if state is None:
            _LOGGER.warning("ENTSO-e entity '%s' not found", self._entity_id)
            return PriceFeedData(entries=[], resolution=timedelta(hours=1))

        points: list[tuple[datetime, float]] = []
        for attr_key in ("prices_today", "prices_tomorrow"):
            points += _parse_points(state.attributes.get(attr_key), ("time",), ("price",))
        if not points:
            points = _parse_points(state.attributes.get("prices"), ("time",), ("price",))
        return _build_feed(points, now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PriceFeedData:
        """DAG translator entry-point; delegates to parse()."""
        return self.parse(hass, now)


def octopus_rate_entities(entity_id: str) -> list[str]:
    """Return the entities an Octopus price pick is read from, the pick first.

    Current Octopus Energy versions publish each day's rates on event entities
    (``event.…_current_day_rates`` / ``event.…_next_day_rates``); older ones
    carried them on the ``sensor.…_current_rate`` sensor. A current-day event
    entity brings its next-day sibling, and a current-rate sensor its two event
    entities, so either pick covers today and tomorrow.

    Args:
        entity_id: The configured Octopus import or export entity.

    Returns:
        The entity ids to read rates from.
    """
    if entity_id.endswith("_current_day_rates"):
        return [entity_id, entity_id.removesuffix("_current_day_rates") + "_next_day_rates"]
    if entity_id.startswith("sensor.") and entity_id.endswith("_current_rate"):
        base = "event." + entity_id.removeprefix("sensor.").removesuffix("_current_rate")
        return [entity_id, f"{base}_current_day_rates", f"{base}_next_day_rates"]
    return [entity_id]


class OctopusAgileTranslator:
    """Reads Octopus Energy rates; produces PriceFeedData (UK).

    Targets the BottlecapDave Octopus Energy integration. Current versions put
    each day's rates on event entities
    (``event.octopus_energy_electricity_<serial>_<mpan>[_export]_current_day_rates``
    and ``…_next_day_rates``) as a ``rates`` attribute of ``{"start": datetime,
    "end": datetime, "value_inc_vat": float}`` in GBP per kWh (the integration
    converts the API's pence); older versions carried ``rates`` / ``all_rates``
    on the current-rate sensor. Both are read (:func:`octopus_rate_entities`).
    When an export entity is configured its rates populate the per-slot export
    price.
    """

    output_type = PriceFeedData

    def __init__(self, entity_id: str, export_entity_id: str = "") -> None:
        """Initialise with the import-rate and optional export-rate sensor IDs."""
        self._entity_id = entity_id
        self._export_entity_id = export_entity_id

    def _read_rates(self, hass: Any, entity_id: str) -> list[tuple[datetime, float]]:
        """Read the ``all_rates`` / ``rates`` of an Octopus pick and its day-rate siblings into points."""
        points: list[tuple[datetime, float]] = []
        for rate_entity in octopus_rate_entities(entity_id):
            state = hass.states.get(rate_entity)
            if state is None:
                continue
            raw = state.attributes.get("all_rates") or state.attributes.get("rates")
            points += _parse_points(raw, ("start", "from", "valid_from"), ("value_inc_vat", "value"))
        return points

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Parse the Octopus import (+ optional export) rate sensors."""
        if now is None:
            now = datetime.now(UTC)
        points = self._read_rates(hass, self._entity_id)
        if not points:
            _LOGGER.warning("Octopus entity '%s' not found or empty", self._entity_id)
        export_by_start: dict[datetime, float] = {}
        if self._export_entity_id:
            export_by_start = dict(self._read_rates(hass, self._export_entity_id))
        return _build_feed(points, now, export_by_start=export_by_start or None)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PriceFeedData:
        """DAG translator entry-point; delegates to parse()."""
        return self.parse(hass, now)


class AmberTranslator:
    """Reads Amber Electric forecast sensors; produces PriceFeedData (AU).

    Targets Home Assistant's Amber Electric integration, whose forecast sensors
    expose ``forecasts`` as a list of ``{"start_time": iso, "end_time": iso,
    "per_kwh": float}`` already in **dollars** per kWh — the integration divides
    the API's cents by 100 — with the feed-in channel's sign flipped so a
    positive value is what exporting earns. When a feed-in forecast sensor is
    configured its values populate the per-slot export price.
    """

    output_type = PriceFeedData

    def __init__(self, entity_id: str, export_entity_id: str = "") -> None:
        """Initialise with the general-price and optional feed-in sensor IDs."""
        self._entity_id = entity_id
        self._export_entity_id = export_entity_id

    def _read_forecasts(self, hass: Any, entity_id: str) -> list[tuple[datetime, float]]:
        """Read an Amber forecast sensor's ``forecasts`` into points."""
        state = hass.states.get(entity_id)
        if state is None:
            return []
        raw = state.attributes.get("forecasts")
        return _parse_points(raw, ("start_time", "start", "nem_time"), ("per_kwh",))

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Parse the Amber general-price (+ optional feed-in) forecast sensors."""
        if now is None:
            now = datetime.now(UTC)
        points = self._read_forecasts(hass, self._entity_id)
        if not points:
            _LOGGER.warning("Amber entity '%s' not found or empty", self._entity_id)
        export_by_start: dict[datetime, float] = {}
        if self._export_entity_id:
            export_by_start = dict(self._read_forecasts(hass, self._export_entity_id))
        return _build_feed(points, now, export_by_start=export_by_start or None)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PriceFeedData:
        """DAG translator entry-point; delegates to parse()."""
        return self.parse(hass, now)


# ---------------------------------------------------------------------------
# Fixed prices (no live sensor)
# ---------------------------------------------------------------------------

class FixedPriceTranslator:
    """Zero-spot slot grid for an install whose buy and sell prices are both fixed.

    No sensor is read: the tariff formulas ignore the spot price, but the
    pipeline still needs today's and tomorrow's slots to price, so this emits
    a 48h ``PriceFeedData`` (local today 00:00 → tomorrow 23:59) of 0.0 spots.
    """

    output_type = PriceFeedData

    def __init__(self, resolution: timedelta = timedelta(hours=1), local_tz: tzinfo | None = None) -> None:
        """Initialise with the slot resolution and local timezone.

        Args:
            resolution: Slot duration to emit (e.g. 15min or 1h).
            local_tz: HA local timezone the days are counted in; UTC when None.
        """
        self._resolution = resolution
        self._local_tz = local_tz or UTC

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Generate the 48h zero-spot slot grid.

        Args:
            hass: Unused (no live sensor); kept for signature parity.
            now: Reference time; defaults to UTC now. Determines today/tomorrow.

        Returns:
            PriceFeedData spanning local today 00:00 → tomorrow 23:59.
        """
        if now is None:
            now = datetime.now(UTC)
        cur = now.astimezone(self._local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        target_end = cur + timedelta(hours=48)
        entries: list[PriceEntry] = []
        while cur < target_end:
            start_utc = cur.astimezone(UTC)
            entries.append(PriceEntry(start=start_utc, end=start_utc + self._resolution, price_eur_kwh=0.0))
            cur += self._resolution
        return PriceFeedData(entries=entries, resolution=self._resolution)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PriceFeedData:
        """DAG translator entry-point; delegates to parse()."""
        return self.parse(hass, now)


def build_price_translator(
    source: str,
    *,
    entity_id: str,
    export_entity_id: str = "",
    resolution: timedelta = timedelta(hours=1),
    local_tz: tzinfo | None = None,
) -> PriceFeedTranslator:
    """Select the price-feed translator for the effective price source.

    Args:
        source: Price-source identifier (see contract.const.PRICE_SOURCES), or
            ``PRICE_SOURCE_FIXED`` when neither price follows the market.
        entity_id: Price sensor entity ID (import / primary feed).
        export_entity_id: Optional separate export-feed sensor ID.
        resolution: Slot resolution of the fixed-price slot grid.
        local_tz: HA local timezone (used by the fixed-price slot grid).

    Returns:
        The matching translator; falls back to NordpoolTranslator for unknown
        or default sources so existing configs keep working.
    """
    if source == PRICE_SOURCE_FIXED:
        return FixedPriceTranslator(resolution=resolution, local_tz=local_tz)
    if source == PRICE_SOURCE_ENTSOE:
        return EntsoeTranslator(entity_id)
    if source == PRICE_SOURCE_OCTOPUS:
        return OctopusAgileTranslator(entity_id, export_entity_id)
    if source == PRICE_SOURCE_AMBER:
        return AmberTranslator(entity_id, export_entity_id)
    return NordpoolTranslator(entity_id)


# ---------------------------------------------------------------------------
# Price-sensor discovery (setup)
# ---------------------------------------------------------------------------

def _first_item_has(raw: Any, keys: tuple[str, ...]) -> bool:
    """Return True when raw is a non-empty list whose first item is a dict holding any of keys."""
    return isinstance(raw, list) and bool(raw) and isinstance(raw[0], dict) and any(k in raw[0] for k in keys)


def price_source_of(attributes: dict[str, Any]) -> str | None:
    """Return the price source whose sensor these state attributes belong to.

    Each test mirrors what that source's translator reads, so a sensor this
    recognises is one its translator can parse.

    Args:
        attributes: A sensor state's attributes.

    Returns:
        The ``PRICE_SOURCES`` id, or None when no translator reads this sensor.
    """
    if isinstance(attributes.get("raw_today"), list) or (
        isinstance(attributes.get("today"), list) and "tomorrow_valid" in attributes
    ):
        return PRICE_SOURCE_NORDPOOL
    if isinstance(attributes.get("prices_today"), list) or _first_item_has(attributes.get("prices"), ("time",)):
        return PRICE_SOURCE_ENTSOE
    rates = attributes.get("all_rates") or attributes.get("rates")
    if _first_item_has(rates, ("value_inc_vat",)):
        return PRICE_SOURCE_OCTOPUS
    if _first_item_has(attributes.get("forecasts"), ("per_kwh",)):
        return PRICE_SOURCE_AMBER
    return None


class DetectedPriceSensor(NamedTuple):
    """A sensor on this system that a price translator can read."""

    source: str
    entity_id: str
    name: str
    # A separate export-price feed (Octopus export rates, Amber feed-in) rather
    # than the import / market price sensor.
    export: bool


def is_export_feed(source: str, entity_id: str, attributes: dict[str, Any]) -> bool:
    """Return True when a detected price sensor is a separate export-price feed.

    Only Octopus and Amber publish one. Octopus flags its export meter's rate
    sensors with ``is_export``, Amber tags each sensor's ``channel_type``
    (``feedIn``); the entity id is the fallback for older versions.

    Args:
        source: The source the sensor was recognised as.
        entity_id: The sensor's entity id.
        attributes: Its state attributes.

    Returns:
        True for an export-price feed, False for an import / market price sensor.
    """
    if source not in (PRICE_SOURCE_OCTOPUS, PRICE_SOURCE_AMBER):
        return False
    if attributes.get("is_export") is True or attributes.get("channel_type") == "feedIn":
        return True
    return "export" in entity_id or "feed_in" in entity_id


def _is_side_feed(source: str, entity_id: str) -> bool:
    """Return True for a readable rate entity that is not one to pick.

    Octopus also publishes gas rates and the previous / next day's electricity
    rates in the same shape; the translator reads the next day itself from the
    picked current-day entity (:func:`octopus_rate_entities`).
    """
    if source != PRICE_SOURCE_OCTOPUS:
        return False
    return "_gas_" in entity_id or entity_id.endswith(("_previous_day_rates", "_next_day_rates"))


def detect_price_sensors(hass: Any) -> list[DetectedPriceSensor]:
    """Return every sensor on this system that a price translator can read.

    Args:
        hass: Home Assistant instance, or None (nothing is detected then).

    Returns:
        The detected sensors, ordered by source as in ``PRICE_SOURCES``, then
        import sensors before export feeds, then by entity id.
    """
    if hass is None:
        return []
    found = []
    # Octopus publishes its rates on event entities, the other sources on sensors.
    for state in hass.states.async_all(("sensor", "event")):
        source = price_source_of(state.attributes)
        if source is not None and not _is_side_feed(source, state.entity_id):
            found.append(DetectedPriceSensor(
                source,
                state.entity_id,
                state.attributes.get("friendly_name") or state.entity_id,
                is_export_feed(source, state.entity_id, state.attributes),
            ))
    rank = {source: i for i, source in enumerate(PRICE_SOURCES)}
    return sorted(found, key=lambda sensor: (rank[sensor.source], sensor.export, sensor.entity_id))
