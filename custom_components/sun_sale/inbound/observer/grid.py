"""Grid stage: per-direction observers + ObservedGridSeries builder.

Four translators feed the pipeline:

* ``GridImportPowerObserver`` snapshots the inverter's instantaneous grid
  *import* power magnitude (kW, ≥ 0) and persists it as
  ``GridImportPowerHistory``.
* ``GridExportPowerObserver`` does the same for *export* (kW, ≥ 0).
* ``GridImportTotalTranslator`` / ``GridExportTotalTranslator`` snapshot the
  daily-resetting cumulative import / export kWh counters. Persisted for the
  pre-rollover snapshot module and the once-per-day bake-in.

``build_observed_grid_series`` averages each direction's samples within each
price-grid slot independently. Because each stream is already non-negative
and direction-pure, no sign split is needed — the ``grid_import`` engine side
extracts directly from ``GridImportPowerHistory``, the ``grid_export`` side
from ``GridExportPowerHistory``.

Window: day-before-yesterday 00:00 local → now. The extra day beyond
"yesterday" is required by ``MonthlyBillNode`` so its day-rollover bake-in
window stays inside the series. Yesterday and the older day are sourced
from ``BakedObservedHistory`` when present; otherwise from raw averaging.
Today is always raw — the next-day bake-in finalises it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TzInfo
from typing import Any

from ...contract.models import (
    BakedObservedHistory,
    GridExportPowerHistory,
    GridExportPowerReading,
    GridExportTodayReading,
    GridImportPowerHistory,
    GridImportPowerReading,
    GridImportTodayReading,
    ObservedGridSeries,
    ObservedGridSlot,
    SlotKwh,
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


# Side identifiers for the grid engine. Stable across the codebase —
# referenced by the bake-in store, the integration check, and the debug view
# as the canonical keys for the import / export tracks.
GRID_IMPORT_SIDE_ID = "grid_import"
GRID_EXPORT_SIDE_ID = "grid_export"


# Maximum age (seconds) for a grid power sample to be considered fresh.
# The solis_modbus meter chain can silently stop polling (sensor state stays
# numeric but ``last_updated`` freezes); when the freshness window is
# exceeded we treat the entity as unavailable.
_GRID_POWER_MAX_AGE_S = 180


def _grid_sides() -> list[Side]:
    """Return the two Side specs for grid import and export tracks.

    Each side's extractor is the identity on its own non-negative sample
    stream — the engine consumes ``GridImportPowerHistory.samples`` for the
    import side and ``GridExportPowerHistory.samples`` for the export side.

    Returns:
        ``[Side("grid_import"), Side("grid_export")]``.
    """
    return [
        Side(id=GRID_IMPORT_SIDE_ID, extract=lambda s: max(0.0, s.power_kw)),
        Side(id=GRID_EXPORT_SIDE_ID, extract=lambda s: max(0.0, s.power_kw)),
    ]


def build_grid_engine(local_tz: TzInfo) -> ObservedSeriesEngine:
    """Return a two-side engine instance for grid import + export.

    Used by both the per-cycle series builder and the once-per-day bake-in
    so they share the same side specs and timezone configuration. Each side
    pulls from its own ``GridImport/ExportPowerHistory`` via the
    ``samples_by_side`` argument.

    Args:
        local_tz: Local timezone for day-boundary handling.

    Returns:
        ``ObservedSeriesEngine`` registered with the import and export sides.
    """
    return ObservedSeriesEngine(_grid_sides(), local_tz=local_tz)


# ---------------------------------------------------------------------------
# Directional power observers
# ---------------------------------------------------------------------------

class _DirectionalPowerObserver:
    """Shared parsing for a non-negative directional grid-power reading.

    Two input modes are supported, picked once at construction:

    1. **Directional entity** — a sensor exposing the magnitude of grid flow
       in a single direction (``entity_id``). Used when the inverter or a
       user-provided template helper exposes per-direction sensors directly.
    2. **Signed fallback** — a single signed net-flow sensor
       (``signed_entity_id``, positive = import) projected onto this side. Used
       by Solis auto-detect when the inverter only exposes ``grid_power_net``.

    Both the clamp and the directional split are the ``GRID_IMPORT`` /
    ``GRID_EXPORT`` codec transforms (the same ones the recorder resampler
    uses), reached through a :class:`..telemetry.HaTelemetryReader`. Selection
    is a single config pick (no runtime fallback from a mapped-but-stale
    directional sensor to the signed one), so :func:`..telemetry.resolve_bindings`
    yields a one-element chain.

    A freshness check guards against stale states — the meter chain can
    silently freeze (numeric value persists but ``last_updated`` does not
    advance), at which point the reading is treated as unavailable rather than
    continuing to bias the per-slot mean. Subclasses set ``_signal`` (the
    direction) and ``reading_cls`` / ``_directional_role`` accordingly.
    """

    reading_cls: type
    output_type: type
    _signal: TelemetrySignal
    _directional_key: str

    def __init__(
        self,
        entity_id: str,
        max_age_s: float = _GRID_POWER_MAX_AGE_S,
        signed_entity_id: str = "",
    ) -> None:
        """Initialise with the HA entity ID(s) + freshness window.

        Args:
            entity_id: Entity ID of the directional power sensor. Empty
                string activates the ``signed_entity_id`` fallback when set.
            max_age_s: Maximum age (seconds) of ``state.last_updated`` for
                the reading to be considered fresh; older states return
                ``None``. ``float('inf')`` disables the freshness check
                (used by tests that inject states without timestamps).
            signed_entity_id: Optional signed net-flow sensor (sunSale
                convention: positive = import, negative = export). Used only
                when ``entity_id`` is empty.
        """
        self._max_age_s = max_age_s
        self._reader = HaTelemetryReader(
            resolve_bindings(
                {},
                signed_grid_power=signed_entity_id,
                **{self._directional_key: entity_id},
            ),
            GenericCodec(),
        )

    def _parse(self, hass: Any, now: datetime):
        """Read the configured entity and return a non-negative directional reading.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; also the reference for the freshness check.

        Returns:
            ``reading_cls`` instance, or ``None`` when unmapped, unparseable,
            or stale.
        """
        power_kw = self._reader.read(
            hass, self._signal, now=now, max_age_s=self._max_age_s
        )
        if power_kw is None:
            return None
        return self.reading_cls(power_kw=power_kw, timestamp=now)


class GridImportPowerObserver(_DirectionalPowerObserver):
    """Reads the directional grid-import power sensor; produces GridImportPowerReading."""

    reading_cls = GridImportPowerReading
    output_type = GridImportPowerReading
    _signal = TelemetrySignal.GRID_IMPORT
    _directional_key = "grid_import_power"

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime,
    ) -> GridImportPowerReading | None:
        """DAG translator entry-point; delegates to ``_parse``."""
        return self._parse(hass, now)


class GridExportPowerObserver(_DirectionalPowerObserver):
    """Reads the directional grid-export power sensor; produces GridExportPowerReading."""

    reading_cls = GridExportPowerReading
    output_type = GridExportPowerReading
    _signal = TelemetrySignal.GRID_EXPORT
    _directional_key = "grid_export_power"

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime,
    ) -> GridExportPowerReading | None:
        """DAG translator entry-point; delegates to ``_parse``."""
        return self._parse(hass, now)


# ---------------------------------------------------------------------------
# Daily today-total counter translators (kWh, reset at local midnight)
# ---------------------------------------------------------------------------

class _DailyTotalKwhTranslator:
    """Shared parsing for daily-resetting cumulative kWh sensors.

    Reads a single HA sensor whose value is "kWh since local midnight" and
    returns the latest snapshot. Subclasses bind it to a concrete reading type.
    """

    reading_cls: type
    output_type: type

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the counter sensor.

        Args:
            entity_id: Entity ID of the cumulative today-total kWh sensor.
        """
        self._entity_id = entity_id

    def _parse(self, hass: Any, now: datetime | None):
        """Read the sensor and return a typed snapshot, or None when unavailable.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            ``reading_cls`` instance, or None on missing / non-numeric state.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        value = read_float_state(hass, self._entity_id)
        if value is None:
            return None
        return self.reading_cls(today_total_kwh=value, timestamp=now)


class GridImportTotalTranslator(_DailyTotalKwhTranslator):
    """Reads the today-total imported-kWh counter; produces GridImportTodayReading."""

    reading_cls = GridImportTodayReading
    output_type = GridImportTodayReading

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GridImportTodayReading | None:
        """DAG translator entry-point; delegates to _parse()."""
        return self._parse(hass, now)


class GridExportTotalTranslator(_DailyTotalKwhTranslator):
    """Reads the today-total exported-kWh counter; produces GridExportTodayReading."""

    reading_cls = GridExportTodayReading
    output_type = GridExportTodayReading

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> GridExportTodayReading | None:
        """DAG translator entry-point; delegates to _parse()."""
        return self._parse(hass, now)


# ---------------------------------------------------------------------------
# ObservedGridSeries assembly
# ---------------------------------------------------------------------------

def build_observed_grid_series(
    import_power_history: GridImportPowerHistory,
    export_power_history: GridExportPowerHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = timezone.utc,
    baked_history: BakedObservedHistory | None = None,
) -> ObservedGridSeries:
    """Derive per-slot gross import / export kWh from per-direction samples.

    The engine consumes the two history streams independently — no sign-
    split needed because each stream is already direction-pure and
    non-negative.

    Window: day-before-yesterday 00:00 local → now. Today's slots are
    always raw averages. Yesterday and the older day come from
    ``baked_history`` when ``BakedDayRecord`` entries exist for the matching
    ``side_id`` on the matching local date; otherwise they fall back to raw
    averaging.

    Args:
        import_power_history: Rolling non-negative import-power samples (kW).
        export_power_history: Rolling non-negative export-power samples (kW).
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations.
        baked_history: Persisted baked-observed history. When omitted or
            empty, past-day slots fall back to raw averaging.

    Returns:
        ObservedGridSeries grid-aligned over the two-day-back window. Empty
        when no price grid is supplied or both histories are empty.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not price_slots or (
        not import_power_history.samples and not export_power_history.samples
    ):
        return ObservedGridSeries(slots=(), computed_at=now)

    engine = build_grid_engine(local_tz=local_tz)
    today_start = _day_start(now, local_tz)
    yesterday_start = today_start - timedelta(days=1)
    two_days_ago_start = today_start - timedelta(days=2)

    samples_by_side = {
        GRID_IMPORT_SIDE_ID: import_power_history.samples,
        GRID_EXPORT_SIDE_ID: export_power_history.samples,
    }

    local_today = now.astimezone(local_tz).date()
    local_yesterday = local_today - timedelta(days=1)
    local_two_days_ago = local_today - timedelta(days=2)

    imp_baked = (
        baked_slots_by_date(baked_history, GRID_IMPORT_SIDE_ID)
        if baked_history is not None else {}
    )
    exp_baked = (
        baked_slots_by_date(baked_history, GRID_EXPORT_SIDE_ID)
        if baked_history is not None else {}
    )

    imp_two_days_ago, exp_two_days_ago = _resolve_past_day(
        engine, samples_by_side, price_slots,
        two_days_ago_start, yesterday_start,
        local_two_days_ago, imp_baked, exp_baked,
    )
    imp_yesterday, exp_yesterday = _resolve_past_day(
        engine, samples_by_side, price_slots,
        yesterday_start, today_start,
        local_yesterday, imp_baked, exp_baked,
    )

    today_per_side = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=today_start,
        window_end=now,
    )
    imp_today = today_per_side[GRID_IMPORT_SIDE_ID]
    exp_today = today_per_side[GRID_EXPORT_SIDE_ID]

    imp_all = imp_two_days_ago + imp_yesterday + imp_today
    exp_all = exp_two_days_ago + exp_yesterday + exp_today

    slots = tuple(
        ObservedGridSlot(
            start=i.start,
            end=i.end,
            imported_kwh=i.kwh,
            exported_kwh=e.kwh,
            source="inverter",
        )
        for i, e in zip(imp_all, exp_all)
    )

    yest_imp = round(sum(s.kwh for s in imp_yesterday), 4)
    yest_exp = round(sum(s.kwh for s in exp_yesterday), 4)
    today_imp = round(sum(s.kwh for s in imp_today), 4)
    today_exp = round(sum(s.kwh for s in exp_today), 4)

    return ObservedGridSeries(
        slots=slots,
        computed_at=now,
        total_yesterday_imported_kwh=yest_imp,
        total_yesterday_exported_kwh=yest_exp,
        total_today_imported_kwh=today_imp,
        total_today_exported_kwh=today_exp,
    )


def _resolve_past_day(
    engine: ObservedSeriesEngine,
    samples_by_side: dict[str, tuple],
    price_slots: tuple,
    window_start: datetime,
    window_end: datetime,
    local_date,
    imp_baked: dict,
    exp_baked: dict,
) -> tuple[list[SlotKwh], list[SlotKwh]]:
    """Return ``(import_slots, export_slots)`` for a past local day.

    For each side independently: if a baked record exists for ``local_date``,
    use its ``baked_slots``; otherwise compute raw averaged slots from the
    engine. Sides are independent — a day where only one side has a baked
    record uses the baked slots for that side and raw averages for the other.

    Args:
        engine: Two-side grid engine.
        samples_by_side: ``side_id → samples`` mapping covering both directions.
        price_slots: Price grid.
        window_start: Inclusive UTC start of the past day.
        window_end: Exclusive UTC end of the past day.
        local_date: The local date the window covers.
        imp_baked: ``date_str → BakedDayRecord`` index for import side.
        exp_baked: ``date_str → BakedDayRecord`` index for export side.

    Returns:
        ``(import_slots, export_slots)`` covering the past day.
    """
    date_str = local_date.isoformat()
    imp_rec = imp_baked.get(date_str)
    exp_rec = exp_baked.get(date_str)
    if imp_rec is not None and exp_rec is not None:
        return list(imp_rec.baked_slots), list(exp_rec.baked_slots)

    raw_per_side = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=window_start,
        window_end=window_end,
    )
    imp_slots = (
        list(imp_rec.baked_slots) if imp_rec is not None
        else raw_per_side[GRID_IMPORT_SIDE_ID]
    )
    exp_slots = (
        list(exp_rec.baked_slots) if exp_rec is not None
        else raw_per_side[GRID_EXPORT_SIDE_ID]
    )
    return imp_slots, exp_slots


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
    return local_midnight.astimezone(timezone.utc)
