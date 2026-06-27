"""Pricing stage: price-feed HA-state readers + 72h PriceSeries assembly.

A *price-feed translator* reads a market-price sensor (or, for the synthetic
TOU source, no sensor) and produces source-agnostic ``PriceFeedData`` (today +
tomorrow, with tomorrow zero-filled until published). One translator exists per
supported market — ``NordpoolTranslator`` (Nordics), ``EntsoeTranslator`` (EU
day-ahead), ``OctopusAgileTranslator`` (UK), ``AmberTranslator`` (AU), and
``TouScheduleTranslator`` (synthetic TOU for US TOU tariffs) — all emitting the
identical ``PriceFeedData`` shape. ``build_price_translator`` selects one from
the configured price source. The ``build_price_series*`` functions then apply
tariff formulas and stitch in persisted yesterday entries to produce the full
72h PriceSeries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Protocol, Sequence

from ..pipeline import tariff as tariff_module
from ..contract.const import (
    PRICE_SOURCE_AMBER,
    PRICE_SOURCE_ENTSOE,
    PRICE_SOURCE_OCTOPUS,
    PRICE_SOURCE_TOU,
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

_LOGGER = logging.getLogger(__name__)


def build_price_series(
    prices: list[PriceEntry],
    config: TariffConfig,
    now: datetime | None = None,
    resolution: timedelta | None = None,
    local_tz: tzinfo | None = None,
    source: str = "nordpool",
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
            local time-of-day for TOU band selection. When None the flat
            distribution fees apply to every slot.
        source: Price-source identifier recorded in each slot's provenance tag.

    Returns:
        PriceSeries with buy/sell/spot populated for each entry.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    slots: list[PriceSlot] = []
    for p in prices:
        local_start = p.start.astimezone(local_tz) if local_tz is not None else None
        buy = tariff_module.buy_price(p.price_eur_kwh, config, local_start)
        sell = tariff_module.sell_price(
            p.price_eur_kwh, config, local_start, export=p.export_price_eur_kwh
        )
        slots.append(PriceSlot(
            start=p.start,
            end=p.end,
            buy_eur_kwh=buy,
            sell_eur_kwh=sell,
            spot_eur_kwh=p.price_eur_kwh,
            sources=(source, "tariff"),
            export_eur_kwh=p.export_price_eur_kwh,
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
) -> PriceSeries:
    """Assemble the 72h yesterday→today→tomorrow PriceSeries with tariff applied.

    Combines persisted yesterday entries with today+tomorrow from the price-feed
    translator. Resolution is taken from feed.resolution so the translator
    remains the single source of truth for slot granularity.

    Args:
        feed: Today + tomorrow entries from a price-feed translator.
        yesterday: Persisted yesterday entries from the coordinator store.
        config: User-configured tariff parameters.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: HA local timezone for TOU band selection; see
            :func:`build_price_series`.
        source: Price-source identifier for provenance.

    Returns:
        PriceSeries spanning yesterday 00:00 → tomorrow 23:59.
    """
    combined = list(yesterday.entries) + list(feed.entries)
    return build_price_series(
        combined, config, now=now, resolution=feed.resolution,
        local_tz=local_tz, source=source,
    )


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

    Args:
        entries: Existing price entries (must not be empty).
        resolution: Slot duration to use for stub entries.
        now: Unused; kept for signature compatibility.

    Returns:
        entries extended with zero-price PriceEntry stubs up to 48h coverage.
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
            now = datetime.now(timezone.utc)

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
                    dt = dt.replace(tzinfo=timezone.utc)
                start_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
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
                    hour_idx, 0, 0, tzinfo=timezone.utc,
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

    output_type: type

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
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)


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
            now = datetime.now(timezone.utc)
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


class OctopusAgileTranslator:
    """Reads Octopus Energy Agile rate sensors; produces PriceFeedData (UK).

    Targets the BottlecapDave Octopus Energy integration, whose current-rate
    sensor exposes ``all_rates`` / ``rates`` as lists of
    ``{"start": iso, "end": iso, "value_inc_vat": float}`` in GBP per kWh. When
    an export sensor is configured its rates populate the per-slot export price
    (consumed by ``sell_mode="feed"``).
    """

    output_type = PriceFeedData

    def __init__(self, entity_id: str, export_entity_id: str = "") -> None:
        """Initialise with the import-rate and optional export-rate sensor IDs."""
        self._entity_id = entity_id
        self._export_entity_id = export_entity_id

    def _read_rates(self, hass: Any, entity_id: str) -> list[tuple[datetime, float]]:
        """Read an Octopus rate sensor's ``all_rates``/``rates`` into points."""
        state = hass.states.get(entity_id)
        if state is None:
            return []
        raw = state.attributes.get("all_rates") or state.attributes.get("rates")
        return _parse_points(raw, ("start", "from", "valid_from"), ("value_inc_vat", "value"))

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Parse the Octopus import (+ optional export) rate sensors."""
        if now is None:
            now = datetime.now(timezone.utc)
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

    Targets the Amber integration, whose forecast sensor exposes ``forecasts``
    as a list of ``{"start_time": iso, "end_time": iso, "per_kwh": float}`` with
    ``per_kwh`` in **cents** per kWh (scaled to dollars/kWh here). When a feed-in
    forecast sensor is configured its values populate the per-slot export price
    (consumed by ``sell_mode="feed"``).
    """

    output_type = PriceFeedData
    _SCALE = 0.01  # Amber publishes cents/kWh

    def __init__(self, entity_id: str, export_entity_id: str = "") -> None:
        """Initialise with the general-price and optional feed-in sensor IDs."""
        self._entity_id = entity_id
        self._export_entity_id = export_entity_id

    def _read_forecasts(self, hass: Any, entity_id: str) -> list[tuple[datetime, float]]:
        """Read an Amber forecast sensor's ``forecasts`` into scaled points."""
        state = hass.states.get(entity_id)
        if state is None:
            return []
        raw = state.attributes.get("forecasts")
        return _parse_points(raw, ("start_time", "start", "nem_time"), ("per_kwh",), self._SCALE)

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Parse the Amber general-price (+ optional feed-in) forecast sensors."""
        if now is None:
            now = datetime.now(timezone.utc)
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
# Synthetic TOU source (no live sensor)
# ---------------------------------------------------------------------------

def tou_bands_from_config(raw_bands: Sequence[Any] | None) -> tuple[tuple[int, float], ...]:
    """Build a sorted (start_minute, price) schedule from stored TOU dicts.

    Each entry is a ``{"start": "HH:MM", "price": float}`` dict. Rows with a
    missing/invalid start are dropped; the result is sorted by start minute.

    Args:
        raw_bands: Stored TOU band list from the config entry, or None.

    Returns:
        Tuple of (start_minute, price) sorted by start_minute (empty when none).
    """
    bands: list[tuple[int, float]] = []
    for entry in raw_bands or ():
        if not isinstance(entry, dict):
            continue
        minute = tariff_module._parse_hhmm(entry.get("start"))
        if minute is None:
            continue
        try:
            price = float(entry.get("price", 0.0))
        except (TypeError, ValueError):
            continue
        bands.append((minute, price))
    return tuple(sorted(bands, key=lambda b: b[0]))


def _select_tou_price(bands: Sequence[tuple[int, float]], local_dt: datetime) -> float:
    """Pick the active TOU import price for a local time, wrapping past midnight.

    Args:
        bands: Sorted (start_minute, price) schedule.
        local_dt: Slot start projected into local time.

    Returns:
        The active band's price, or 0.0 when the schedule is empty.
    """
    if not bands:
        return 0.0
    minute = local_dt.hour * 60 + local_dt.minute
    chosen = bands[-1]  # wrap: before the first start the last band carries over
    for band in bands:
        if band[0] <= minute:
            chosen = band
        else:
            break
    return chosen[1]


class TouScheduleTranslator:
    """Synthetic price feed from a fixed TOU import-price schedule (US TOU).

    No live sensor: emits a 48h ``PriceFeedData`` (today + tomorrow, local) where
    each slot's import price is the active band's value. Pairs naturally with
    ``sell_mode="schedule"`` for a separate avoided-cost export schedule.
    """

    output_type = PriceFeedData

    def __init__(
        self,
        bands: Sequence[tuple[int, float]],
        resolution: timedelta = timedelta(hours=1),
        local_tz: tzinfo | None = None,
    ) -> None:
        """Initialise with the TOU schedule, slot resolution, and local timezone.

        Args:
            bands: Sorted (start_minute, price) import schedule.
            resolution: Slot duration to emit (e.g. 15min or 1h).
            local_tz: HA local timezone; the schedule is in local time. Defaults
                to UTC when None.
        """
        self._bands = tuple(bands)
        self._resolution = resolution
        self._local_tz = local_tz or timezone.utc

    def parse(self, hass: Any, now: datetime | None = None) -> PriceFeedData:
        """Generate a 48h synthetic feed from the TOU schedule.

        Args:
            hass: Unused (no live sensor); kept for signature parity.
            now: Reference time; defaults to UTC now. Determines today/tomorrow.

        Returns:
            PriceFeedData spanning local today 00:00 → tomorrow 23:59.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if not self._bands:
            return PriceFeedData(entries=[], resolution=self._resolution)

        local_now = now.astimezone(self._local_tz)
        cur = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        target_end = cur + timedelta(hours=48)
        entries: list[PriceEntry] = []
        while cur < target_end:
            start_utc = cur.astimezone(timezone.utc).replace(second=0, microsecond=0)
            price = _select_tou_price(self._bands, cur)
            entries.append(PriceEntry(
                start=start_utc, end=start_utc + self._resolution, price_eur_kwh=price,
            ))
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
    tou_bands: Sequence[tuple[int, float]] = (),
    local_tz: tzinfo | None = None,
) -> PriceFeedTranslator:
    """Select the price-feed translator for the configured price source.

    Args:
        source: Price-source identifier (see contract.const.PRICE_SOURCES).
        entity_id: Price sensor entity ID (import / primary feed).
        export_entity_id: Optional separate export-feed sensor ID.
        resolution: Slot resolution for the synthetic TOU source.
        tou_bands: Sorted (start_minute, price) schedule for the TOU source.
        local_tz: HA local timezone (used by the TOU source).

    Returns:
        The matching translator; falls back to NordpoolTranslator for unknown
        or default sources so existing configs keep working.
    """
    if source == PRICE_SOURCE_ENTSOE:
        return EntsoeTranslator(entity_id)
    if source == PRICE_SOURCE_OCTOPUS:
        return OctopusAgileTranslator(entity_id, export_entity_id)
    if source == PRICE_SOURCE_AMBER:
        return AmberTranslator(entity_id, export_entity_id)
    if source == PRICE_SOURCE_TOU:
        return TouScheduleTranslator(tou_bands, resolution=resolution, local_tz=local_tz)
    return NordpoolTranslator(entity_id)
