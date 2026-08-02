"""Generation stage: HA-edge readers + ObservedGenerationSeries assembly.

The PV power sensor is the only intra-day source — `PvPowerTranslator`
snapshots instantaneous PV power each cycle and the engine averages those
samples within each price-grid slot:

    slot_kwh = mean(power_W in [slot.start, slot.end)) × slot_duration_h / 1000

`GenerationTranslator` continues to snapshot the inverter's today-total
counter; the today-total samples are persisted by the coordinator and consumed
by the once-per-day bake-in operation (Phase 3) — not by this builder. Per-
cycle counter-based correction is intentionally absent: yesterday is finalised
once at the day rollover via the bake-in's proportional adjustment, today
stays as raw averaged values until the next rollover.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import tzinfo as TzInfo
from typing import Any

from ...contract.models import (
    BakedObservedHistory,
    GenerationReading,
    ObservedGenerationSeries,
    ObservedGenerationSlot,
    PvPowerHistory,
    PvPowerReading,
    SunSaleConfig,
)
from ...ha_state import read_float_state
from ..telemetry import (
    GenericCodec,
    HaTelemetryReader,
    TelemetrySignal,
    resolve_bindings,
)
from .bake_in import baked_slots_by_date
from .engine import ObservedSeriesEngine, Side

# Side identifier for the generation engine instance. Stable across the
# codebase — referenced by the bake-in store, the integration check, and the
# debug view as the canonical key for the single solar-generation track.
GENERATION_SIDE_ID = "generation"


def _generation_side() -> Side:
    """Return the Side spec for the single solar-generation track.

    Extracts non-negative PV power in **kW** from a ``PvPowerReading``. The
    engine multiplies the per-slot mean by the slot duration in hours to
    obtain kWh.

    Returns:
        Side with ``extract: PvPowerReading → kW``.
    """
    return Side(
        id=GENERATION_SIDE_ID,
        extract=lambda s: max(0.0, s.power_w) / 1000.0,
    )


def build_generation_engine(local_tz: TzInfo) -> ObservedSeriesEngine:
    """Return a single-side engine instance for solar generation.

    Used by both the per-cycle series builder and the once-per-day bake-in
    so they share the same side spec and timezone configuration.

    Args:
        local_tz: Local timezone for day-boundary handling.

    Returns:
        ``ObservedSeriesEngine`` registered with the generation side.
    """
    return ObservedSeriesEngine([_generation_side()], local_tz=local_tz)


def build_observed_generation_series(
    pv_power_history: PvPowerHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = UTC,
    baked_history: BakedObservedHistory | None = None,
) -> ObservedGenerationSeries:
    """Derive per-slot observed generation by averaging PV power samples.

    Slots span yesterday 00:00 local → now. Today slots are always raw
    power-averaged. Yesterday slots come from ``baked_history`` when a
    ``BakedDayRecord`` exists for ``GENERATION_SIDE_ID`` on yesterday's local
    date; otherwise they are raw averaged values (e.g. on the day after a
    fresh install before any bake has run).

    Args:
        pv_power_history: Rolling instantaneous PV power samples (W).
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations.
        baked_history: Persisted baked-observed history. When omitted or
            empty, yesterday's slots fall back to raw averaging.

    Returns:
        ObservedGenerationSeries covering yesterday 00:00 local → now,
        grid-aligned. Empty when no price grid or no samples.
    """
    if now is None:
        now = datetime.now(UTC)

    if not price_slots or not pv_power_history.samples:
        return ObservedGenerationSeries(slots=(), computed_at=now)

    engine = ObservedSeriesEngine([_generation_side()], local_tz=local_tz)
    today_start = _day_start(now, local_tz)
    yesterday_start = today_start - timedelta(days=1)
    samples_by_side = {GENERATION_SIDE_ID: pv_power_history.samples}

    today_slots = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=today_start,
        window_end=now,
    )[GENERATION_SIDE_ID]

    local_yesterday_str = (
        now.astimezone(local_tz).date() - timedelta(days=1)
    ).isoformat()
    baked_index = (
        baked_slots_by_date(baked_history, GENERATION_SIDE_ID)
        if baked_history is not None else {}
    )
    baked_record = baked_index.get(local_yesterday_str)
    if baked_record is not None:
        yesterday_slots = list(baked_record.baked_slots)
    else:
        yesterday_slots = engine.build_slots_for_window(
            samples_by_side=samples_by_side,
            price_slots=price_slots,
            window_start=yesterday_start,
            window_end=today_start,
        )[GENERATION_SIDE_ID]

    slots = tuple(
        ObservedGenerationSlot(
            start=s.start,
            end=s.end,
            generated_kwh=s.kwh,
            source="inverter",
        )
        for s in (yesterday_slots + today_slots)
    )

    yest_sum = round(sum(s.kwh for s in yesterday_slots), 4)
    today_sum = round(sum(s.kwh for s in today_slots), 4)

    return ObservedGenerationSeries(
        slots=slots,
        computed_at=now,
        total_yesterday_kwh=yest_sum,
        total_today_so_far_kwh=today_sum,
    )


def _day_start(t: datetime, local_tz: TzInfo) -> datetime:
    """Return local midnight for t's local day, expressed as a UTC-aware datetime.

    Args:
        t: Any tz-aware datetime.
        local_tz: Timezone defining "local midnight".

    Returns:
        UTC-aware datetime of local midnight on t's local date.
    """
    local_t = t.astimezone(local_tz)
    local_midnight = local_t.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC)


# ---------------------------------------------------------------------------
# Translators (HA-edge readers)
# ---------------------------------------------------------------------------

class PvPowerTranslator:
    """Reads the inverter instantaneous PV power sensor; produces PvPowerReading.

    Accepts sensors in W or kW; reads unit_of_measurement from entity attributes
    and normalises to watts internally.
    """

    output_type = PvPowerReading

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the inverter PV power sensor.

        Args:
            entity_id: Entity ID of the instantaneous PV power sensor (W or kW).
        """
        self._reader = HaTelemetryReader(
            resolve_bindings({}, pv_power=entity_id), GenericCodec()
        )

    def parse(self, hass: Any, now: datetime | None = None) -> PvPowerReading | None:
        """Read the PV power sensor and return a timestamped snapshot in watts.

        The ``PV_POWER`` codec transform (W or kW → non-negative watts) is the
        same one the recorder resampler applies, so live and historical PV
        samples cannot diverge. No freshness window — the PV translator never
        had one.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            PvPowerReading with power_w ≥ 0, or None when the entity is absent,
            unavailable, or not configured.
        """
        if now is None:
            now = datetime.now(UTC)
        power_w = self._reader.read(hass, TelemetrySignal.PV_POWER, now=now)
        if power_w is None:
            return None
        return PvPowerReading(power_w=power_w, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> PvPowerReading | None:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            PvPowerReading or None when the sensor is unavailable.
        """
        return self.parse(hass, now)


class GenerationTranslator:
    """Reads the inverter today-total generation sensor; produces GenerationReading.

    The sensor is a cumulative kWh counter that resets at local midnight.
    Persisted into ``GenerationHistory`` for use by the pre-rollover snapshot
    module (Phase 2) and the once-per-day bake-in operation (Phase 3).
    """

    output_type = GenerationReading

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the inverter solar-energy today sensor.

        Args:
            entity_id: Entity ID of the cumulative today-total kWh sensor.
        """
        self._entity_id = entity_id

    def parse(self, hass: Any, now: datetime | None = None) -> GenerationReading | None:
        """Read the today-total sensor and return a timestamped snapshot.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            GenerationReading with the current counter value, or None when the
            entity is absent, unavailable, or not configured.
        """
        if now is None:
            now = datetime.now(UTC)
        value = read_float_state(hass, self._entity_id)
        if value is None:
            return None
        return GenerationReading(today_total_kwh=value, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GenerationReading | None:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            GenerationReading or None when the sensor is unavailable.
        """
        return self.parse(hass, now)
