"""Forecast stage: solar HA-state reader + GenerationSeries assembly.

The `SolarTranslator` reads forecast entities (Open Meteo watts, with a
Forecast.Solar / Solcast fallback) and produces SolarData. The
`build_generation_series` function then resamples it onto the price grid to
produce a continuous 72h GenerationSeries (yesterday 00:00 → tomorrow 23:59).
Every price slot gets exactly one generation slot, zero-filled where solar
coverage is absent. Yesterday is stitched in upstream from the persistent
store, invisible to consumers here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..contract.models import (
    GenerationSeries,
    GenerationSlot,
    SolarData,
    SolarEntry,
    SunSaleConfig,
)


def build_generation_series(
    solar: SolarData,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: Any = None,
) -> GenerationSeries:
    """Resample SolarData onto the price grid and return a complete GenerationSeries.

    Args:
        solar: Unified solar forecast from SolarTranslator.
        price_slots: Price-grid slots defining the target time resolution.
        now: Cycle timestamp for today_remaining calculation; defaults to UTC now.
        local_tz: HA-configured local timezone used for date-boundary bucketing.
            When provided, "today/tomorrow/etc." labels align to local midnight
            rather than UTC midnight, avoiding a mismatch window after local midnight
            for UTC-offset installations.

    Returns:
        GenerationSeries with one GenerationSlot per price slot and daily totals.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    ext = _compute_extended_day_totals(solar.entries, now, local_tz)

    if not solar.entries or not price_slots:
        return GenerationSeries(
            slots=(),
            total_d2_kwh=ext[2],
            total_d3_kwh=ext[3],
            total_d4_kwh=ext[4],
            total_d5_kwh=ext[5],
            total_d6_kwh=ext[6],
        )

    resampled = _resample_to_grid(solar.entries, price_slots)
    totals = _compute_totals(resampled, now, local_tz)

    return GenerationSeries(
        slots=resampled,
        total_yesterday_kwh=totals["yesterday"],
        total_today_kwh=totals["today"],
        total_tomorrow_kwh=totals["tomorrow"],
        today_remaining_kwh=totals["today_remaining"],
        total_d2_kwh=ext[2],
        total_d3_kwh=ext[3],
        total_d4_kwh=ext[4],
        total_d5_kwh=ext[5],
        total_d6_kwh=ext[6],
    )


def _resample_to_grid(
    entries: list[SolarEntry],
    target_slots: tuple,
) -> tuple[GenerationSlot, ...]:
    """Redistribute entry kWh onto target slots by overlap-weighted area.

    Works for both downsampling (15-min → 1h) and upsampling (1h → 15-min).
    Every target slot is emitted with zero kWh when no entries overlap it,
    ensuring the output covers the full price grid continuously.

    Args:
        entries: Raw SolarEntry list from the forecast source.
        target_slots: Price-grid slots defining start/end for each output slot.

    Returns:
        Tuple of GenerationSlots aligned to target_slots; empty on empty entries.
    """
    # Pre-extract entry spans once; entries may not be sorted strictly, but
    # we iterate over all of them per target slot anyway.
    spans: list[tuple[datetime, datetime, float, float]] = []
    for e in entries:
        dur = (e.end - e.start).total_seconds()
        if dur <= 0:
            continue
        spans.append((e.start, e.end, e.expected_kwh, dur))

    if not spans:
        return ()

    out: list[GenerationSlot] = []
    for t in target_slots:
        total = 0.0
        for e_start, e_end, e_kwh, e_dur in spans:
            ov_start = e_start if e_start > t.start else t.start
            ov_end = e_end if e_end < t.end else t.end
            ov_secs = (ov_end - ov_start).total_seconds()
            if ov_secs <= 0:
                continue
            total += e_kwh * (ov_secs / e_dur)
        out.append(GenerationSlot(
            start=t.start,
            end=t.end,
            expected_kwh=round(total, 6),
        ))
    return tuple(out)


def _compute_extended_day_totals(
    entries: list[SolarEntry], now: datetime, local_tz: Any = None
) -> dict[int, float]:
    """Sum raw solar entries into daily kWh totals for days d2..d6 (outside the price grid).

    Args:
        entries: All SolarEntry objects (any date).
        now: Reference time used to determine today's date.
        local_tz: HA-configured local timezone; when provided, dates are compared
            in local time so "today" aligns with local midnight.

    Returns:
        Dict {2: kwh, 3: kwh, 4: kwh, 5: kwh, 6: kwh} for days 2–6 ahead of today.
    """
    today = now.astimezone(local_tz).date() if local_tz else now.date()

    def _entry_date(e: SolarEntry):
        """Return e's start date, in local time when local_tz is supplied."""
        return e.start.astimezone(local_tz).date() if local_tz else e.start.date()

    return {
        n: round(sum(e.expected_kwh for e in entries if _entry_date(e) == today + timedelta(days=n)), 4)
        for n in range(2, 7)
    }


def _compute_totals(
    slots: tuple[GenerationSlot, ...], now: datetime, local_tz: Any = None
) -> dict[str, float]:
    """Bucket resampled generation slots into yesterday/today/tomorrow totals.

    Args:
        slots: Price-grid-aligned GenerationSlots.
        now: Reference UTC timestamp; used as-is for today_remaining comparison.
        local_tz: HA-configured local timezone; when provided, date boundaries use
            local midnight so labels align with entity day labels.

    Returns:
        Dict with keys "yesterday", "today", "tomorrow", "today_remaining" in kWh.
    """
    today = now.astimezone(local_tz).date() if local_tz else now.date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    yest_sum = 0.0
    today_sum = 0.0
    tomo_sum = 0.0
    today_remaining = 0.0
    for s in slots:
        d = s.start.astimezone(local_tz).date() if local_tz else s.start.date()
        if d == yesterday:
            yest_sum += s.expected_kwh
        elif d == today:
            today_sum += s.expected_kwh
            if s.start >= now:
                today_remaining += s.expected_kwh
        elif d == tomorrow:
            tomo_sum += s.expected_kwh

    return {
        "yesterday": round(yest_sum, 4),
        "today": round(today_sum, 4),
        "tomorrow": round(tomo_sum, 4),
        "today_remaining": round(today_remaining, 4),
    }


# ---------------------------------------------------------------------------
# Solar translator (HA-edge reader)
# ---------------------------------------------------------------------------

def _tomorrow_entity(entity_id: str) -> str:
    """Derive the tomorrow forecast entity ID by substituting 'today' → 'tomorrow'.

    Args:
        entity_id: Entity ID containing "_today_" or "_today" substring.

    Returns:
        Modified entity ID, or empty string if no substitution pattern is found.
    """
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""


def _day_entity(entity_id: str, n: int) -> str:
    """Derive the day-n (d2..d6) forecast entity ID by substituting 'today' → 'dn'.

    Args:
        entity_id: Entity ID containing "_today_" or "_today" substring.
        n: Day offset (2–6).

    Returns:
        Modified entity ID, or empty string if no substitution pattern is found.
    """
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", f"d{n}"), 1)
    return ""


def _watts_to_solar_entries(watts: dict[datetime, float]) -> list[SolarEntry]:
    """Convert a {slot_utc: watts} dict to a SolarEntry list, auto-detecting slot duration.

    Args:
        watts: Dict mapping UTC slot timestamps to power in watts.

    Returns:
        Sorted SolarEntry list with expected_kwh = W * slot_hours / 1000.
    """
    sorted_ts = sorted(watts.keys())
    if len(sorted_ts) >= 2:
        slot_dur = sorted_ts[1] - sorted_ts[0]
    else:
        slot_dur = timedelta(hours=1)
    slot_h = slot_dur.total_seconds() / 3600.0
    return [
        SolarEntry(
            start=ts,
            end=ts + slot_dur,
            expected_kwh=round(w * slot_h / 1000.0, 6),
            source="open_meteo",
        )
        for ts in sorted_ts
        for w in (watts[ts],)
    ]


def _make_solar_data(entries: list[SolarEntry], source: str, now: datetime) -> SolarData:
    """Wrap a SolarEntry list in SolarData with today totals computed.

    Args:
        entries: All solar entries (any date).
        source: Primary source label (e.g. "open_meteo").
        now: Reference time for computing today's totals.

    Returns:
        SolarData with total_today_kwh and today_remaining_kwh populated.
    """
    today = now.date()
    total_today = sum(e.expected_kwh for e in entries if e.start.date() == today)
    remaining = sum(e.expected_kwh for e in entries if e.start.date() == today and e.start >= now)
    return SolarData(
        entries=entries,
        total_today_kwh=round(total_today, 4),
        today_remaining_kwh=round(remaining, 4),
        primary_source=source,
    )


class SolarTranslator:
    """Reads solar forecast from HA entities; produces SolarData.

    Tries Open Meteo (watts attribute) first across all entity IDs (today + tomorrow).
    Falls back to Forecast.Solar / Solcast (forecast attribute).
    Any number of panels (one base "today" entity each) are combined by summing
    at each timestamp — device-based selection resolves these upstream in
    ``inbound/forecast_resolver.py``; manual config supplies them directly.
    Coordinator prepends yesterday entries from persistent store.
    """

    output_type = SolarData

    def __init__(self, base_entities: list[str]) -> None:
        """Initialise with the resolved base "today" forecast entity IDs.

        Args:
            base_entities: One base forecast entity per panel/device (e.g. an
                Open Meteo ``*_today`` sensor). Empty strings are ignored; an
                empty list yields empty SolarData.
        """
        self._base_entities = [e for e in base_entities if e]

    def parse(self, hass: Any, now: datetime | None = None) -> SolarData:
        """Parse all configured solar forecast entities into SolarData.

        Tries Open Meteo watts first; falls back to Forecast.Solar/Solcast forecast.
        Multiple panels are combined by summing at each timestamp.

        Synchronous; callable directly from tests.

        Args:
            hass: Home Assistant instance.
            now: Reference time for today totals; defaults to UTC now.

        Returns:
            SolarData with entries from yesterday → d6. Returns empty SolarData
            when no recognised entity or attribute is found.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        combined_watts: dict[datetime, float] = {}
        for base_eid in self._base_entities:
            eids = [base_eid, _tomorrow_entity(base_eid)] + [_day_entity(base_eid, n) for n in range(2, 7)]
            for eid in eids:
                if not eid:
                    continue
                state = hass.states.get(eid)
                if state is None:
                    continue
                raw_watts = state.attributes.get("watts")
                if not isinstance(raw_watts, dict):
                    continue
                for ts_str, w in raw_watts.items():
                    try:
                        dt = datetime.fromisoformat(str(ts_str))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        slot_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                        combined_watts[slot_utc] = combined_watts.get(slot_utc, 0.0) + float(w)
                    except (ValueError, TypeError):
                        continue

        if combined_watts:
            entries = _watts_to_solar_entries(combined_watts)
            return _make_solar_data(entries, "open_meteo", now)

        # --- Forecast.Solar / Solcast fallback: collect from all entities ---
        combined_kwh: dict[datetime, float] = {}
        for base_eid in self._base_entities:
            state = hass.states.get(base_eid)
            if state is None:
                continue
            for slot in state.attributes.get("forecast", []):
                try:
                    dt = datetime.fromisoformat(str(slot["time"]))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    slot_utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                    kwh = float(slot.get("pv_estimate", slot.get("energy", 0.0)))
                    combined_kwh[slot_utc] = combined_kwh.get(slot_utc, 0.0) + kwh
                except (KeyError, ValueError, TypeError):
                    continue

        if combined_kwh:
            entries = [
                SolarEntry(start=s, end=s + timedelta(hours=1), expected_kwh=kwh, source="forecast_solar")
                for s, kwh in sorted(combined_kwh.items())
            ]
            return _make_solar_data(entries, "forecast_solar", now)

        return SolarData(entries=[], total_today_kwh=0.0, today_remaining_kwh=0.0, primary_source="none")

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> SolarData:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            SolarData for all configured forecast entities.
        """
        return self.parse(hass, now)
