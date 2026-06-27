"""sunSale DataUpdateCoordinator — thin orchestrator for the DAG pipeline.

Responsibilities:
  1. Build translators, DAG nodes, and engine from config.
  2. Manage CapacityEstimator state across cycles (pre-DAG update + persistence).
  3. Run translators (parallel) → deposit EstimatedCapacity → run engine.
  4. Drive the inverter control module's dispatch tick from the DAG's Schedule.
  5. Map typed DAG outputs to the string-keyed coordinator.data dict for sensors.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover — Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo    # type: ignore[no-redef]

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..pipeline.battery import CapacityEstimator
from ..contract.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_BMS_BATTERY_POWER,
    CONF_BMS_BATTERY_SOC,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_ENTITY_INVERTER_CLOCK,
    CONF_INVERTER_EXPORT_LIMIT_KW,
    CONF_CURRENCY,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_NORDPOOL_ENTITY,
    CONF_NORDPOOL_RESOLUTION,
    CONF_PRICE_SOURCE,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_TOU_BANDS,
    DEFAULT_CURRENCY,
    DEFAULT_PRICE_SOURCE,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY_2,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_FIXED_SELL_PRICE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_MODE,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CONF_TARIFF_WEEKDAY_BANDS,
    CONF_TARIFF_WEEKEND_BANDS,
    DEFAULT_SELL_MODE,
    CAPACITY_OBS_COUNTER_RESET_EPS_KWH,
    CAPACITY_OBS_EMIT_SOC_DELTA,
    CAPACITY_OBS_MAX_WINDOW_S,
    CAPACITY_OBS_PURITY_FRACTION,
    COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_EXPORT_LIMIT_W,
    DEFAULT_INVERTER_EXPORT_LIMIT_KW,
    DEFAULT_INVERTER_MAX_POWER_KW,
    GRID_POWER_HISTORY_RETENTION_DAYS,
    DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID,
    DEFAULT_SCHEDULE_ALLOW_FEED_IN,
    DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING,
    DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW,
    DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH,
    DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA,
    DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT,
    DEFAULT_SCHEDULE_USE_STANDBY,
    DOMAIN,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
    SCHEDULE_MODE_CHANGE_PENALTY_MAX,
    SCHEDULE_MODE_CHANGE_PENALTY_MIN,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
    PRICE_HISTORY_RETENTION_DAYS,
    STORAGE_KEY_CAPACITY,
    STORAGE_KEY_FORECAST_QUALITY,
    STORAGE_KEY_BAKED_OBSERVED,
    STORAGE_KEY_COUNTER_SNAPSHOT,
    STORAGE_KEY_DERIVED_POWER,
    STORAGE_KEY_CONSUMPTION_DAILY,
    STORAGE_KEY_MODE_HISTORY,
    STORAGE_KEY_MONTHLY_BILL,
    STORAGE_KEY_PRICE_HISTORY,
    STORAGE_KEY_YESTERDAY,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from ..pipeline.dag_engine import DagEngine, run_translators
from ..ha_state import normalize_power_to_kw, power_unit_scale
from ..inbound.telemetry import (
    SignalRole,
    TelemetryCodec,
    TelemetrySignal,
    signed_polarity,
)
from ..outbound.inverter import (
    InverterController,
    InverterPlatform,
)
from ..outbound.entity_control import InverterContext
from ..outbound.driver_factory import make_inverter_driver
from ..inbound.platform_profiles import profile_for, unbacked_roles
from ..outbound.inverter_control_module import InverterControlModule
from ..contract.models import (
    BaseLoadProfile,
    BatteryConfig,
    BatteryReading,
    BatteryRuntimeEstimate,
    BatteryState,
    BatteryStatus,
    CalculationResult,
    CapacityObservation,
    DailyPeak,
    DayClass,
    BakedDayRecord,
    BakedObservedHistory,
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
    CounterSnapshotHistory,
    CounterSnapshotRecord,
    DegradationCost,
    EstimatedCapacity,
    ForecastAccuracyResult,
    ForecastQualityStore,
    GenerationReading,
    GenerationSeries,
    GridExportPowerHistory,
    GridExportPowerReading,
    GridExportTodayReading,
    GridImportPowerHistory,
    GridImportPowerReading,
    GridImportTodayReading,
    InverterModeChange,
    InverterModeHistory,
    InverterModeReading,
    InverterTimeReading,
    MonthlyBillResult,
    MonthlyBillState,
    PvPowerHistory,
    PvPowerReading,
    HouseholdConsumptionReading,
    AcPortPowerReading,
    BackupPowerReading,
    DerivedPowerHistory,
    NordpoolData,
    ObservedConsumptionSeries,
    ObservedGenerationSeries,
    ObservedGridSeries,
    ObservedLossesSeries,
    PriceEntry,
    PriceHistory,
    PriceSeries,
    ProfitabilityScore,
    SchedulePolicy,
    SlotKwh,
    SolarData,
    SolarEntry,
    Schedule,
    StorageMode,
    SunSaleConfig,
    SunTimes,
    TariffConfig,
    YesterdayPrices,
)
from ..pipeline.nodes import (
    BaseLoadProfileNode,
    BatteryRuntimeNode,
    BatteryStateNode,
    BatteryStatusNode,
    DegradationNode,
    ForecastAccuracyNode,
    GenerationNode,
    LockoutNode,
    MonthlyBillNode,
    ObservedConsumptionNode,
    ObservedGenerationNode,
    ObservedGridNode,
    ObservedLossesNode,
    ScheduleNode,
    PricingNode,
    ProfitabilityNode,
)
from ..pipeline import forecast_accuracy as forecast_accuracy_module
from ..pipeline import profitability as profitability_module
from ..pipeline import tariff as tariff_module
from .persistent_store import (
    PersistentStore,
    entry_storage_key,
)
from .history_stores import (
    ALL_HISTORY_SPECS,
    DERIVED_POWER_SPEC,
    GRID_EXPORT_POWER_SPEC,
    GRID_IMPORT_POWER_SPEC,
    SAMPLE_HISTORY_SPECS,
    append_and_inject,
)
from ..inbound.battery import BatteryTranslator
from ..inbound.battery_source import (
    BatterySource,
    ChainedBatterySource,
    InverterBatterySource,
    JkBmsBatterySource,
)
from ..inbound.inverter_entity_resolver import resolve_inverter_entities
from ..inbound.forecast import SolarTranslator
from ..inbound.forecast_resolver import resolve_forecast_entities
from ..inbound.observer.generation import (
    GENERATION_SIDE_ID,
    GenerationTranslator,
    PvPowerTranslator,
    build_generation_engine,
)
from ..inbound.consumption_daily import (
    backfill_from_derived_history,
    try_finalise_yesterday_consumption,
)
from ..inbound.household_consumption import HouseholdConsumptionTranslator
from ..inbound.observer.grid import (
    GRID_EXPORT_SIDE_ID,
    GRID_IMPORT_SIDE_ID,
    GridExportPowerObserver,
    GridExportTotalTranslator,
    GridImportPowerObserver,
    GridImportTotalTranslator,
    build_grid_engine,
)
from ..inbound.observer.derived import (
    AcPortPowerTranslator,
    BackupPowerTranslator,
    build_derived_power_sample,
)
from ..inbound.inverter_mode import InverterModeTranslator
from ..inbound.inverter_time import (
    InverterTimeHistory,
    InverterTimeTranslator,
    current_skew_seconds,
    empty_history as empty_inverter_time_history,
    update_history as update_inverter_time_history,
)
from ..inbound.observer.bake_in import try_bake_yesterday
from ..inbound.observer.recorder_resample import (
    RecorderResampler,
    ResampleSources,
    merge_by_timestamp,
)
from ..inbound.pre_rollover_snapshot import maybe_capture_snapshots
from ..inbound.pricing import build_price_translator, tou_bands_from_config

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Yesterday/today two-bucket state
# ---------------------------------------------------------------------------

@dataclass
class _YesterdayBuckets:
    """In-memory state for the two-bucket yesterday/today price + solar store."""

    yesterday_date: str | None = None
    yesterday_nordpool: list[PriceEntry] = field(default_factory=list)
    yesterday_solar: list[SolarEntry] = field(default_factory=list)
    today_date: str | None = None
    today_nordpool: list[PriceEntry] = field(default_factory=list)
    today_solar: list[SolarEntry] = field(default_factory=list)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Return ``value`` clamped into ``[lo, hi]`` and coerced to ``float``.

    Args:
        value: User-set knob value (already coerced from the Number entity).
        lo: Inclusive lower bound.
        hi: Inclusive upper bound.

    Returns:
        ``value`` clamped to the closed interval; NaN inputs collapse to ``lo``.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    if v != v:    # NaN check — NaN != NaN by IEEE-754
        return lo
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _parse_nordpool_entries(payload: dict) -> list[PriceEntry]:
    """Deserialise a list of price entries from a stored payload dict."""
    return [
        PriceEntry(
            start=datetime.fromisoformat(e["start"]),
            end=datetime.fromisoformat(e["end"]),
            price_eur_kwh=e["price"],
        )
        for e in payload.get("nordpool", [])
    ]


def _parse_solar_entries(payload: dict) -> list[SolarEntry]:
    """Deserialise a list of solar entries from a stored payload dict."""
    return [
        SolarEntry(
            start=datetime.fromisoformat(e["start"]),
            end=datetime.fromisoformat(e["end"]),
            expected_kwh=e["kwh"],
            source=e["source"],
        )
        for e in payload.get("solar", [])
    ]


def _serialize_yesterday(buckets: _YesterdayBuckets) -> dict:
    """Serialise yesterday buckets to the two-bucket storage layout."""
    def _ser_nordpool(xs: list[PriceEntry]) -> list[dict]:
        """Serialise a list of Nordpool price entries to dicts."""
        return [{"start": e.start.isoformat(), "end": e.end.isoformat(), "price": e.price_eur_kwh} for e in xs]

    def _ser_solar(xs: list[SolarEntry]) -> list[dict]:
        """Serialise a list of solar forecast entries to dicts."""
        return [{"start": e.start.isoformat(), "end": e.end.isoformat(), "kwh": e.expected_kwh, "source": e.source} for e in xs]

    return {
        "yesterday": {
            "date":     buckets.yesterday_date,
            "nordpool": _ser_nordpool(buckets.yesterday_nordpool),
            "solar":    _ser_solar(buckets.yesterday_solar),
        },
        "today": {
            "date":     buckets.today_date,
            "nordpool": _ser_nordpool(buckets.today_nordpool),
            "solar":    _ser_solar(buckets.today_solar),
        },
    }


def _deserialize_yesterday(d: dict) -> _YesterdayBuckets:
    """Deserialise yesterday buckets, handling the legacy single-bucket layout."""
    if "yesterday" in d or "today" in d:
        y = d.get("yesterday") or {}
        t = d.get("today") or {}
        return _YesterdayBuckets(
            yesterday_date=y.get("date"),
            yesterday_nordpool=_parse_nordpool_entries(y),
            yesterday_solar=_parse_solar_entries(y),
            today_date=t.get("date"),
            today_nordpool=_parse_nordpool_entries(t),
            today_solar=_parse_solar_entries(t),
        )
    # Legacy single-bucket layout {"date","nordpool","solar"} — treat as yesterday.
    return _YesterdayBuckets(
        yesterday_date=d.get("date"),
        yesterday_nordpool=_parse_nordpool_entries(d),
        yesterday_solar=_parse_solar_entries(d),
    )


def _rotate_yesterday_buckets(
    buckets: _YesterdayBuckets,
    today_str: str,
    today_nordpool: list[PriceEntry],
    today_solar: list[SolarEntry],
) -> _YesterdayBuckets:
    """Apply day-rollover rotation and replace the today slice.

    On the first save of a new local day the previous today bucket becomes
    yesterday; within the same day only the today slice is overwritten.

    Args:
        buckets: Current bucket state.
        today_str: ISO date string for the current local day.
        today_nordpool: Today's price entries to store.
        today_solar: Today's solar entries to store.

    Returns:
        Updated _YesterdayBuckets.
    """
    if buckets.today_date is not None and buckets.today_date != today_str:
        return _YesterdayBuckets(
            yesterday_date=buckets.today_date,
            yesterday_nordpool=buckets.today_nordpool,
            yesterday_solar=buckets.today_solar,
            today_date=today_str,
            today_nordpool=today_nordpool,
            today_solar=today_solar,
        )
    return _YesterdayBuckets(
        yesterday_date=buckets.yesterday_date,
        yesterday_nordpool=buckets.yesterday_nordpool,
        yesterday_solar=buckets.yesterday_solar,
        today_date=today_str,
        today_nordpool=today_nordpool,
        today_solar=today_solar,
    )


# ---------------------------------------------------------------------------
# Per-store serialisation helpers
# ---------------------------------------------------------------------------
# The rolling sample-history stores (generation, PV power, the two grid-power
# directions, the two grid today-totals, and the derived sample) are described
# declaratively in ``history_stores.py``; only the irregular stores below carry
# bespoke serialisers.

def _serialize_consumption_daily(buckets: ConsumptionDailyBuckets) -> dict:
    """Serialise the rolling per-day hour-bucket consumption history."""
    return {
        "records": [
            {
                "date":  r.local_date.isoformat(),
                "kwh":   list(r.hour_kwh),
                "cov":   list(r.hour_completeness),
                "at":    r.finalised_at.isoformat(),
            }
            for r in buckets.records
        ]
    }


def _deserialize_consumption_daily(d: dict) -> ConsumptionDailyBuckets:
    """Deserialise the rolling per-day hour-bucket consumption history.

    Malformed records are skipped silently so a single bad row cannot block
    startup. Records that don't carry a full 24-tuple for either ``kwh`` or
    ``cov`` are dropped — the builder requires fixed shape.
    """
    records: list[ConsumptionDayRecord] = []
    for r in d.get("records", []):
        try:
            kwh = tuple(float(x) for x in r["kwh"])
            cov = tuple(float(x) for x in r["cov"])
            if len(kwh) != 24 or len(cov) != 24:
                continue
            records.append(
                ConsumptionDayRecord(
                    local_date=date.fromisoformat(r["date"]),
                    hour_kwh=kwh,
                    hour_completeness=cov,
                    finalised_at=datetime.fromisoformat(r["at"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return ConsumptionDailyBuckets(records=tuple(records))


def _serialize_price_history(peaks: list[DailyPeak]) -> dict:
    """Serialise a list of daily peaks."""
    return {
        "peaks": [{"day": p.day.isoformat(), "peak": p.peak_eur_kwh, "class": p.day_class.value} for p in peaks]
    }


def _deserialize_price_history(d: dict) -> list[DailyPeak]:
    """Deserialise a list of daily peaks, silently skipping malformed entries."""
    result: list[DailyPeak] = []
    for p in d.get("peaks", []):
        try:
            result.append(DailyPeak(
                day=date.fromisoformat(p["day"]),
                peak_eur_kwh=p["peak"],
                day_class=DayClass(p["class"]),
            ))
        except (KeyError, ValueError):
            continue
    return result


def _serialize_counter_snapshot(history: CounterSnapshotHistory) -> dict:
    """Serialise the rolling pre-rollover counter snapshot history."""
    return {
        "records": [
            {
                "side": r.side_id,
                "ts":   r.captured_at.isoformat(),
                "kwh":  r.today_total_kwh,
            }
            for r in history.records
        ]
    }


def _deserialize_counter_snapshot(d: dict) -> CounterSnapshotHistory:
    """Deserialise the rolling pre-rollover counter snapshot history."""
    records: list[CounterSnapshotRecord] = []
    for r in d.get("records", []):
        try:
            records.append(
                CounterSnapshotRecord(
                    side_id=r["side"],
                    captured_at=datetime.fromisoformat(r["ts"]),
                    today_total_kwh=float(r["kwh"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return CounterSnapshotHistory(records=tuple(records))


def _serialize_baked_observed(history: BakedObservedHistory) -> dict:
    """Serialise the rolling baked-observed history (one record per (date, side))."""
    return {
        "records": [
            {
                "date":   r.date_str,
                "side":   r.side_id,
                "ctotal": r.counter_total_used,
                "src":    r.source_kind,
                "slots":  [
                    {"s": s.start.isoformat(), "e": s.end.isoformat(), "kwh": s.kwh}
                    for s in r.baked_slots
                ],
                "sum":    r.baked_sum,
                "at":     r.baked_at.isoformat(),
            }
            for r in history.records
        ]
    }


def _deserialize_baked_observed(d: dict) -> BakedObservedHistory:
    """Deserialise the rolling baked-observed history.

    Malformed entries are skipped silently so a single bad row cannot block
    startup. Slot lists with non-parseable timestamps are dropped wholesale.
    """
    records: list[BakedDayRecord] = []
    for r in d.get("records", []):
        try:
            slots = tuple(
                SlotKwh(
                    start=datetime.fromisoformat(s["s"]),
                    end=datetime.fromisoformat(s["e"]),
                    kwh=float(s["kwh"]),
                )
                for s in r["slots"]
            )
            records.append(
                BakedDayRecord(
                    date_str=r["date"],
                    side_id=r["side"],
                    counter_total_used=float(r["ctotal"]),
                    source_kind=r["src"],
                    baked_slots=slots,
                    baked_sum=float(r["sum"]),
                    baked_at=datetime.fromisoformat(r["at"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return BakedObservedHistory(records=tuple(records))


def _serialize_mode_history(history: InverterModeHistory) -> dict:
    """Serialise the rolling inverter-mode-change history."""
    return {
        "samples": [
            {"ts": s.timestamp.isoformat(), "mode": s.mode.value, "raw_state": s.raw_state}
            for s in history.samples
        ]
    }


def _deserialize_mode_history(d: dict) -> InverterModeHistory:
    """Deserialise the rolling inverter-mode-change history.

    Unknown mode strings (e.g. from older versions) are coerced to UNKNOWN so
    the integration starts cleanly after a release that retired a mode value.
    """
    samples: list[InverterModeChange] = []
    for s in d.get("samples", []):
        try:
            mode = StorageMode(s["mode"])
        except ValueError:
            mode = StorageMode.UNKNOWN
        samples.append(
            InverterModeChange(
                timestamp=datetime.fromisoformat(s["ts"]),
                mode=mode,
                # Accept the legacy "reg" key so histories persisted before the
                # raw_state rename still load.
                raw_state=int(s["raw_state"] if "raw_state" in s else s["reg"]),
            )
        )
    return InverterModeHistory(samples=tuple(samples))


def _serialize_monthly_bill(state: MonthlyBillState) -> dict:
    """Serialise the monthly bill ledger as a ``date_str → cost`` map."""
    return {"finalized_days": dict(state.finalized_days)}


def _deserialize_monthly_bill(d: dict) -> MonthlyBillState:
    """Deserialise the monthly bill ledger.

    Legacy entries (pre-ledger, carrying ``carry_eur`` / ``yday_str``) have no
    ``finalized_days`` key and deserialise to an empty ledger — the carry then
    rebuilds from the next priceable day forward (older days' prices are gone,
    so they are unrecoverable regardless).
    """
    raw = d.get("finalized_days") or {}
    return MonthlyBillState(
        finalized_days=tuple(
            (str(ds), float(cost)) for ds, cost in raw.items()
        ),
    )


async def _backfill_directional_power_from_recorder(
    hass: HomeAssistant,
    entity_id: str,
    reading_cls: type,
    existing: list,
    start: datetime,
    end: datetime,
) -> list:
    """Return existing samples merged with recorder state changes for ``entity_id``.

    Used on coordinator setup so the monthly bill's yday→now slots have data
    even on the first run after the integration is installed/upgraded.
    Recorder state values are normalised to kW via the entity's
    ``unit_of_measurement`` attribute, matching the live read path. Negative
    values are clamped to 0 — directional magnitudes are non-negative by
    contract.

    Args:
        hass: Home Assistant instance.
        entity_id: Power sensor entity ID; empty disables backfill.
        reading_cls: Reading dataclass to construct (e.g.
            ``GridImportPowerReading``). Must accept ``power_kw`` and
            ``timestamp`` keyword args.
        existing: Samples already loaded from the persistent store.
        start: Earliest UTC timestamp to backfill.
        end: Latest UTC timestamp to backfill.

    Returns:
        Combined, time-sorted list of ``reading_cls`` samples deduplicated by
        timestamp. Returns ``existing`` unchanged if the recorder is
        unavailable or the entity_id is empty.
    """
    if not entity_id:
        return existing

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import (
            state_changes_during_period,
        )
    except ImportError:
        return existing

    try:
        instance = get_instance(hass)
        states_dict = await instance.async_add_executor_job(
            state_changes_during_period,
            hass,
            start,
            end,
            entity_id,
        )
    except Exception:    # pragma: no cover — recorder may be unavailable
        _LOGGER.warning(
            "Directional power recorder backfill failed for %s", entity_id, exc_info=True,
        )
        return existing

    states = states_dict.get(entity_id, []) if states_dict else []
    backfilled: list = []
    for s in states:
        raw = getattr(s, "state", None)
        if raw in (None, "unavailable", "unknown", ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        unit = str((getattr(s, "attributes", {}) or {}).get("unit_of_measurement") or "").strip()
        kw = max(0.0, normalize_power_to_kw(value, unit))
        ts = getattr(s, "last_updated", None) or getattr(s, "last_changed", None)
        if ts is None:
            continue
        backfilled.append(reading_cls(power_kw=kw, timestamp=ts))

    if not backfilled:
        return existing

    seen: set[datetime] = set()
    merged: list = []
    for sample in (*existing, *backfilled):
        if sample.timestamp in seen:
            continue
        seen.add(sample.timestamp)
        merged.append(sample)
    merged.sort(key=lambda x: x.timestamp)
    return merged


class SunSaleCoordinator(DataUpdateCoordinator):
    """Thin orchestrator: translators → capacity update → DAG → event routing."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialise coordinator with lazy setup state; call async_setup() before use.

        Args:
            hass: Home Assistant instance.
            config_entry: HA config entry containing user configuration.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._entry = config_entry
        self._config: dict = {}
        self._sun_sale_config: SunSaleConfig | None = None
        self._capacity_store: PersistentStore[CapacityEstimator] | None = None
        self._capacity_estimator: CapacityEstimator | None = None
        self._engine: DagEngine | None = None
        self._translators: list = []
        self._control_module: InverterControlModule | None = None
        # Capacity-estimator anchor: the reading a still-open SoC swing is
        # measured against. Held fixed across cycles (it advances only when an
        # observation is emitted or the window is abandoned), so it is *not* the
        # previous cycle's reading. Managed entirely inside
        # ``_build_capacity_observation``.
        self._cap_anchor_reading: BatteryReading | None = None
        self._cap_anchor_at: datetime | None = None
        self._yesterday_store: PersistentStore[_YesterdayBuckets] | None = None
        # Rolling sample-history stores keyed by storage key — populated in
        # async_setup from ``history_stores.ALL_HISTORY_SPECS``. See
        # orchestration/history_stores.py.
        self._history_stores: dict[str, PersistentStore] = {}
        self._consumption_daily_store: PersistentStore[ConsumptionDailyBuckets] | None = None
        self._price_history_store: PersistentStore[list[DailyPeak]] | None = None
        self._forecast_quality_store: PersistentStore[ForecastQualityStore] | None = None
        self._grid_import_power_entity_id: str = ""
        self._grid_export_power_entity_id: str = ""
        self._monthly_bill_store: PersistentStore[MonthlyBillState] | None = None
        self._mode_history_store: PersistentStore[InverterModeHistory] | None = None
        self._counter_snapshot_store: PersistentStore[CounterSnapshotHistory] | None = None
        self._baked_observed_store: PersistentStore[BakedObservedHistory] | None = None
        self._inverter_time_history: InverterTimeHistory = empty_inverter_time_history()
        self.automation_enabled: bool = False
        self.use_standby: bool = DEFAULT_SCHEDULE_USE_STANDBY
        self.allow_grid_charging: bool = DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING
        self.allow_feed_in: bool = DEFAULT_SCHEDULE_ALLOW_FEED_IN
        self.allow_discharge_to_grid: bool = DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID
        self.mode_change_penalty_eur_per_kwh: float = (
            DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH
        )
        self.profitability_tilt_alpha: float = DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA
        self.terminal_value_discount: float = DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT
        self.max_discharge_to_grid_kw: float | None = DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW
        # Deployment export-power cap (W) — set from config in async_setup; the
        # default keeps the panel's power-flow axis sane before setup completes.
        self._export_limit_w: int = DEFAULT_EXPORT_LIMIT_W
        # Manual override for the dispatched StorageMode. When set, the control
        # module forwards this to the inverter regardless of the scheduler's
        # current-slot choice AND regardless of ``automation_enabled`` —
        # operator intent always reaches the inverter. ``None`` (default)
        # keeps sunSale's scheduled choice (the "sunsale" option in the UI).
        self.mode_override: StorageMode | None = None
        self.last_dispatched_action: str | None = None
        self.last_dispatched_at: datetime | None = None
        # Phase 0 visibility: per-tick dispatch outcome surfaced on the
        # ObservedInverterModeSensor. ``last_dispatched_action`` reflects only
        # successful writes; these fields reflect the latest tick regardless
        # of outcome (no_target, no_spec, automation_disabled, ok, reconcile,
        # holding).
        self.last_dispatch_outcome: str | None = None
        self.last_dispatch_target: str | None = None
        self.last_dispatch_tick_at: datetime | None = None
        self.automation_enabled_at_dispatch: bool | None = None
        # Phase 2 visibility: commanded-mode + verify-loop state, mirrored
        # from the InverterControlModule after each tick. See
        # ``inverter_control_module.py`` for the verify-state vocabulary.
        self.last_commanded_mode: str | None = None
        self.last_commanded_at: datetime | None = None
        self.verify_state: str | None = None
        self.last_verify_at: datetime | None = None
        self.last_verify_observed_reg: int | None = None
        # Per-register desired-vs-observed comparison for the last-commanded
        # mode, mirrored from the control module. Drives the panel's
        # green/amber/red register readout.
        self.register_status: list[dict] = []
        # Always-populated canonical register comparison (observed value of
        # every register, with the commanded target or None) plus the live
        # decoded observed mode — drive the panel's intended-vs-observed
        # 3-column readout, which must show register values even when no mode
        # is commanded or the observed bitmask maps to no named mode.
        self.register_panel: list[dict] = []
        self.observed_mode: str | None = None
        # Unsubscribe for the live mode-observation listener (set in
        # ``async_setup``): pushes a fresh observed-mode/register readout to the
        # panel whenever a decoded inverter register changes, between ticks.
        self._unsub_mode_registers: Callable[[], None] | None = None

    @property
    def battery_config(self) -> BatteryConfig | None:
        """Return the configured BatteryConfig, or None before async_setup completes."""
        return self._sun_sale_config.battery if self._sun_sale_config else None

    async def force_verify_inverter_mode(self) -> None:
        """Run an inverter-mode verify cycle immediately.

        Backs the ``sun_sale.force_verify_inverter_mode`` service. Delegates
        to ``InverterControlModule.force_verify_now``; when the control
        module isn't ready yet (during early ``async_setup``), the call is
        a no-op so the service handler doesn't have to special-case startup.
        """
        if self._control_module is None:
            return
        await self._control_module.force_verify_now()

    async def dispatch_mode_override(self) -> None:
        """Push the current ``mode_override`` to the inverter immediately.

        Lightweight alternative to ``async_request_refresh`` for the
        mode-override select: a button press dispatches through the control
        module's ``dispatch_override`` entry point, skipping the full DAG
        refresh (14 translators, 16 nodes, the store saves) that
        ``async_request_refresh`` would incur just to reach the dispatcher.
        The override (or, when released to ``sunsale``, the current slot from
        the last computed Schedule) reaches the inverter at once; the next
        regular tick reconciles the rest of the pipeline.

        Mirrors the resulting dispatch + verify state and notifies listeners so
        the panel's badge and per-register colours update instantly. A no-op
        before the control module is built (early ``async_setup``).
        """
        if self._control_module is None:
            return
        now = datetime.now(timezone.utc)
        schedule = self.data.get("schedule") if isinstance(self.data, dict) else None
        await self._control_module.dispatch_override(
            now=now,
            schedule=schedule,
            mode_override=self.mode_override,
            automation_enabled=self.automation_enabled,
        )
        self._mirror_control_module_state()
        self.async_update_listeners()

    def _mirror_control_module_state(self) -> None:
        """Copy the control module's dispatch + verify state onto the coordinator.

        Pulls every module-derived diagnostic field (dispatch outcome, the
        commanded-mode truth, the verify-loop state, and the per-register
        comparison) into the coordinator's public attributes so the
        ``ObservedInverterModeSensor`` and panel read a single, current
        snapshot. Tick-local fields (``last_dispatched_action`` / ``_at``) are
        owned by ``_async_update_data`` and not touched here.
        """
        module = self._control_module
        if module is None:
            return
        self.last_dispatch_outcome = module.last_dispatch_outcome
        module_target = module.last_dispatch_target
        self.last_dispatch_target = (
            module_target.value if module_target is not None else None
        )
        self.last_dispatch_tick_at = module.last_dispatch_at
        self.automation_enabled_at_dispatch = (
            module.automation_enabled_at_last_dispatch
        )
        commanded = module.last_commanded_mode
        self.last_commanded_mode = (
            commanded.value if commanded is not None else None
        )
        self.last_commanded_at = module.last_commanded_at
        self.verify_state = module.verify_state
        self.last_verify_at = module.last_verify_at
        self.last_verify_observed_reg = module.last_verify_observed_reg
        self.register_status = module.register_status
        self.register_panel = module.register_panel
        self.observed_mode = module.observed_mode.value

    def _on_control_state_change(self) -> None:
        """Refresh entity state when the verify loop mutates outside a tick.

        Wired into ``InverterControlModule`` as ``on_state_change``. The verify
        loop runs on ``async_call_later`` between coordinator cycles, so without
        this push the engagement badge and per-register colours would lag up to
        one 5-minute cycle. Mirrors the fresh module state, then notifies the
        coordinator's listeners so ``ObservedInverterModeSensor`` re-renders.
        """
        self._mirror_control_module_state()
        self.async_update_listeners()

    def _subscribe_mode_register_changes(self) -> None:
        """Subscribe to the inverter registers that decode the observed mode.

        On any change, mirror the control module's live-decoded observation
        onto the coordinator and notify listeners, so the panel's mode readout
        tracks the inverter in real time. The watched entities are exactly the
        ones ``observed_mode`` / ``register_panel`` decode from — on Solis: the
        register-43110 readback, the battery currents, the RC selector +
        setpoint, and the export cap; unmapped entities are skipped. Read-only:
        it never issues a write.
        The unsubscribe is stored for teardown in ``async_shutdown``.
        """
        ids = self._inverter_entity_ids
        watched_keys = (
            "storage_control_readback",
            "battery_max_charge_current",
            "battery_max_discharge_current",
            "rc_setpoint",
            "rc_grid_adjustment_select",
            "backflow_power",
        )
        entity_ids = [ids[k] for k in watched_keys if ids.get(k)]
        if not entity_ids:
            return
        self._unsub_mode_registers = async_track_state_change_event(
            self.hass, entity_ids, self._on_mode_register_change,
        )

    @callback
    def _on_mode_register_change(self, event: Event) -> None:
        """Push a fresh mode observation when a decoded register changes.

        Read-only HA state-change handler for ``_subscribe_mode_register_changes``:
        delegates to ``_on_control_state_change`` (mirror + notify). Guarded
        against an as-yet-unbuilt control module so an early registry event is a
        harmless no-op.

        Args:
            event: The HA state-change event (unused; the handler re-reads live
                state via the control module rather than the event payload).
        """
        if self._control_module is None:
            return
        self._on_control_state_change()

    async def async_shutdown(self) -> None:
        """Tear down the coordinator and its control module on entry unload.

        Extends ``DataUpdateCoordinator.async_shutdown`` (which cancels the
        scheduled refresh) by also shutting down the control module, so its
        pending verify-tick cannot fire after unload and issue ghost Modbus
        writes against the still-valid solis_modbus entities.
        """
        if self._unsub_mode_registers is not None:
            self._unsub_mode_registers()
            self._unsub_mode_registers = None
        if self._control_module is not None:
            self._control_module.shutdown()
        await super().async_shutdown()

    @property
    def tariff_config(self) -> TariffConfig | None:
        """Return the configured TariffConfig, or None before async_setup completes."""
        return self._sun_sale_config.tariff if self._sun_sale_config else None

    async def async_setup(self) -> None:
        """Build config objects, translators, DAG nodes, engine, and event router."""
        data = {**self._entry.data, **self._entry.options}
        self._config = data

        tariff_config = TariffConfig(
            distribution_fee=data[CONF_TARIFF_DISTRIBUTION_FEE],
            tax_rate=data[CONF_TARIFF_TAX_RATE] / 100.0,
            markup=data[CONF_TARIFF_MARKUP],
            sell_distribution_fee=data[CONF_TARIFF_SELL_DISTRIBUTION_FEE],
            sell_tax_rate=data[CONF_TARIFF_SELL_TAX_RATE] / 100.0,
            sell_markup=data[CONF_TARIFF_SELL_MARKUP],
            weekday_bands=tariff_module.bands_from_config(data.get(CONF_TARIFF_WEEKDAY_BANDS)),
            weekend_bands=tariff_module.bands_from_config(data.get(CONF_TARIFF_WEEKEND_BANDS)),
            sell_mode=data.get(CONF_TARIFF_SELL_MODE, DEFAULT_SELL_MODE),
            fixed_sell_price=data.get(CONF_TARIFF_FIXED_SELL_PRICE, 0.0),
        )

        battery_config = BatteryConfig(
            nominal_capacity_kwh=data[CONF_BATTERY_NOMINAL_CAPACITY],
            purchase_price_eur=data[CONF_BATTERY_PURCHASE_PRICE],
            rated_cycle_life=data[CONF_BATTERY_RATED_CYCLE_LIFE],
            max_charge_power_kw=data[CONF_BATTERY_MAX_CHARGE_POWER],
            max_discharge_power_kw=data[CONF_BATTERY_MAX_DISCHARGE_POWER],
            min_soc=data[CONF_BATTERY_MIN_SOC] / 100.0,
            max_soc=data[CONF_BATTERY_MAX_SOC] / 100.0,
            round_trip_efficiency=data[CONF_BATTERY_ROUND_TRIP_EFFICIENCY] / 100.0,
            nominal_voltage_v=data.get(CONF_BATTERY_NOMINAL_VOLTAGE, DEFAULT_BATTERY_NOMINAL_VOLTAGE),
        )

        # Entity-ID resolution (platform branch, Solis auto-detect, observer
        # entity merge, yesterday-total mapping) lives in
        # inbound/inverter_entity_resolver.py. It mutates ``data`` in place to
        # add any auto-detected yesterday-total entities.
        resolved = resolve_inverter_entities(self.hass, data)
        inverter = InverterController(
            self.hass, resolved.platform, resolved.inverter_entity_ids, battery_config,
        )
        self._inverter_entity_ids = dict(resolved.inverter_entity_ids)
        self._inverter_platform = resolved.platform
        # Single platform telemetry codec, owned by the controller and reused
        # here for the live-flow panel spec and below for the recorder
        # resampler — one selection, one instance, no scattered sign literals.
        self._telemetry_codec: TelemetryCodec = inverter.codec
        self._grid_import_power_entity_id = resolved.grid_import_power
        self._grid_export_power_entity_id = resolved.grid_export_power
        self._pv_power_entity_id = resolved.pv_power
        self._solar_energy_today_entity_id = resolved.solar_energy_today
        self._ac_port_power_entity_id = resolved.ac_port_power
        self._backup_power_entity_id = resolved.backup_power
        self._signed_grid_power_entity_id = resolved.signed_grid_power
        # Battery telemetry source. A dedicated BMS (JK-BMS) is preferred for
        # SoC / pack power when its SoC entity is mapped, falling back to the
        # inverter per field; otherwise the inverter is the sole source.
        battery_source: BatterySource = InverterBatterySource(inverter)
        bms_soc_entity = data.get(CONF_BMS_BATTERY_SOC, "")
        if bms_soc_entity:
            battery_source = ChainedBatterySource(
                primary=JkBmsBatterySource(
                    self.hass,
                    soc_entity=bms_soc_entity,
                    power_entity=data.get(CONF_BMS_BATTERY_POWER, ""),
                ),
                fallback=battery_source,
            )
        # Source map for the per-cycle recorder resampling that lifts the
        # observer streams from per-tick to native resolution. See
        # inbound/observer/recorder_resample.py.
        self._resampler = RecorderResampler(ResampleSources(
            pv_power=resolved.pv_power,
            ac_port_power=resolved.ac_port_power,
            backup_power=resolved.backup_power,
            grid_import_power=resolved.grid_import_power,
            grid_export_power=resolved.grid_export_power,
            signed_grid_power=resolved.signed_grid_power,
            entity_ids=dict(resolved.inverter_entity_ids),
            codec=inverter.codec,
        ))

        local_tz = self._resolve_local_tz()
        self._sun_sale_config = SunSaleConfig(
            tariff=tariff_config, battery=battery_config,
            local_tz=local_tz,
            price_source=data.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE),
            currency=(data.get(CONF_CURRENCY) or DEFAULT_CURRENCY).strip().upper(),
        )

        # Per-deployment inverter power ratings (config flow, kW → W). Fall back
        # to the historical 10 kW defaults for configs created before these
        # fields existed.
        inverter_max_power_w = int(
            data.get(CONF_INVERTER_MAX_POWER_KW, DEFAULT_INVERTER_MAX_POWER_KW) * 1000
        )
        self._export_limit_w = int(
            data.get(CONF_INVERTER_EXPORT_LIMIT_KW, DEFAULT_INVERTER_EXPORT_LIMIT_KW) * 1000
        )

        # The DP's Discharge-to-grid cap defaults to the rated inverter power so
        # the planned export rate matches what the dispatcher commands. A user
        # override restored by MaxDischargeToGridKwNumber (added after this
        # setup) takes precedence; raising the entity to its max restores the
        # "use hardware max" (None) behaviour.
        self.max_discharge_to_grid_kw = inverter_max_power_w / 1000.0

        # Platform control driver — the seam where a new inverter platform plugs
        # in. Owns spec composition, the panel control surface, observed-mode
        # decoding, and the per-cycle mode observation, so the control module's
        # verify/reconcile loop and the mode translator carry no register knowledge.
        # Neutral HA environment the entity-driven (non-Solis) drivers build
        # from, so they never reach into the Solis-flavoured controller. Grid
        # power stays an inverter-side telemetry read (the controller's getter).
        inverter_context = InverterContext(
            hass=self.hass,
            entity_ids=resolved.inverter_entity_ids,
            get_grid_power=inverter.get_grid_power,
        )
        driver = make_inverter_driver(
            inverter,
            inverter_context,
            resolved.platform,
            battery_config,
            export_limit_w=self._export_limit_w,
            inverter_max_power_w=inverter_max_power_w,
        )
        self._validate_driver_role_contract(resolved.platform, driver)

        # Resolve once and cache so the debug view / integration check can see
        # the actual base entities feeding SolarTranslator (device-based
        # selection resolves these from config-entry IDs at runtime).
        self._forecast_base_entities = self._resolve_forecast_base_entities(data)

        tou_resolution = (
            timedelta(minutes=15)
            if data.get(CONF_NORDPOOL_RESOLUTION) == "15min"
            else timedelta(hours=1)
        )
        self._translators = [
            build_price_translator(
                data.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE),
                entity_id=data.get(CONF_NORDPOOL_ENTITY, ""),
                export_entity_id=data.get(CONF_PRICE_EXPORT_ENTITY, ""),
                resolution=tou_resolution,
                tou_bands=tou_bands_from_config(data.get(CONF_PRICE_TOU_BANDS)),
                local_tz=local_tz,
            ),
            SolarTranslator(
                base_entities=self._forecast_base_entities,
            ),
            BatteryTranslator(
                inverter=inverter,
                # Legacy: kept so existing configs that mapped a single
                # household-load sensor still feed BatteryReading.household_load_kw
                # for the dashboard. New configs leave this empty → 0.2 kW stub.
                household_load_entity=data.get("inverter_entity_household_load", ""),
                battery_source=battery_source,
            ),
            GridImportPowerObserver(
                entity_id=self._grid_import_power_entity_id,
                signed_entity_id=resolved.signed_grid_power,
            ),
            GridExportPowerObserver(
                entity_id=self._grid_export_power_entity_id,
                signed_entity_id=resolved.signed_grid_power,
            ),
            GridImportTotalTranslator(entity_id=resolved.grid_import_total),
            GridExportTotalTranslator(entity_id=resolved.grid_export_total),
            GenerationTranslator(entity_id=resolved.solar_energy_today),
            PvPowerTranslator(entity_id=resolved.pv_power),
            HouseholdConsumptionTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY, ""),
            ),
            AcPortPowerTranslator(entity_id=self._ac_port_power_entity_id),
            BackupPowerTranslator(entity_id=self._backup_power_entity_id),
            InverterModeTranslator(driver=driver),
            InverterTimeTranslator(
                entity_id=data.get(CONF_INVERTER_ENTITY_INVERTER_CLOCK, ""),
                local_tz=local_tz,
            ),
        ]

        nodes = [
            PricingNode(),
            BatteryStateNode(),
            BatteryStatusNode(),
            BaseLoadProfileNode(),
            GenerationNode(),
            ObservedGenerationNode(),
            ObservedGridNode(),
            ObservedConsumptionNode(),
            ObservedLossesNode(),
            DegradationNode(),
            MonthlyBillNode(),
            BatteryRuntimeNode(),
            ForecastAccuracyNode(),
            ProfitabilityNode(),
            LockoutNode(),
            ScheduleNode(),
        ]

        self._engine = DagEngine(nodes)
        self._control_module = InverterControlModule(
            driver=driver,
            local_tz=local_tz,
            hass=self.hass,
            on_state_change=self._on_control_state_change,
        )
        # Live mode observation: push the panel's intended-vs-observed readout
        # on every change of the registers that decode the inverter mode,
        # instead of only once per 5-min cycle. Read-only — it recomputes
        # ``register_panel`` / ``observed_mode`` (which read live HA state) and
        # notifies listeners; it never writes. Complements the verify loop
        # (which pushes only during its ~60 s post-command window) so a
        # steady-state change — the inverter altered at its own screen, or an
        # RC function expiring — surfaces within one solis_modbus poll rather
        # than lagging up to a full cycle.
        self._subscribe_mode_register_changes()

        # round_trip_efficiency lets the estimator normalise charge (AC-in,
        # over-reads) and discharge (AC-out, under-reads) samples to a single
        # stored-capacity figure. Passing the live nominal lets the deserialize
        # callback purge observations when it changed (batteries added/removed) —
        # and also drops pre-fix poison (older schema_version). Both reset the
        # estimate to the current nominal to re-learn quickly. See
        # CapacityEstimator.from_dict.
        # Persistent stores are namespaced per config entry so two entries keep
        # independent state. ``ekey`` builds the per-entry key from each base key.
        entry_id = self._entry.entry_id

        def ekey(base_key: str) -> str:
            """Return the per-entry storage key for ``base_key``."""
            return entry_storage_key(entry_id, base_key)

        _rt_eff = battery_config.round_trip_efficiency
        _nominal = battery_config.nominal_capacity_kwh
        self._capacity_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_CAPACITY),
            serialize=lambda e: e.to_dict(),
            deserialize=lambda d: CapacityEstimator.from_dict(d, _rt_eff, _nominal),
        )
        self._capacity_estimator = (
            await self._capacity_store.load()
            or CapacityEstimator(_nominal, round_trip_efficiency=_rt_eff)
        )

        # Rolling sample-history stores — one PersistentStore per spec, all
        # sharing the spec-driven serialise/deserialise. The dict stays keyed by
        # the base ``spec.storage_key``; only the HA store key is per-entry. See
        # orchestration/history_stores.py.
        for spec in ALL_HISTORY_SPECS:
            store = PersistentStore(
                self.hass, STORAGE_VERSION, ekey(spec.storage_key),
                serialize=spec.serialize,
                deserialize=spec.deserialize,
            )
            await store.load()
            self._history_stores[spec.storage_key] = store

        # Backfill the directional grid-power stores from the recorder so the
        # monthly bill's yday→now slots have data on the first run after the
        # integration is installed/upgraded.
        backfill_now = datetime.now(timezone.utc)
        backfill_start = backfill_now - timedelta(days=GRID_POWER_HISTORY_RETENTION_DAYS)
        for spec, entity_id in (
            (GRID_IMPORT_POWER_SPEC, self._grid_import_power_entity_id),
            (GRID_EXPORT_POWER_SPEC, self._grid_export_power_entity_id),
        ):
            store = self._history_stores[spec.storage_key]
            existing_samples = list(store.value or [])
            merged_samples = await _backfill_directional_power_from_recorder(
                self.hass, entity_id, spec.reading_type,
                existing_samples, backfill_start, backfill_now,
            )
            if len(merged_samples) != len(existing_samples):
                await store.save(merged_samples)

        self._consumption_daily_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_CONSUMPTION_DAILY),
            serialize=_serialize_consumption_daily,
            deserialize=_deserialize_consumption_daily,
        )
        await self._consumption_daily_store.load()

        self._price_history_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_PRICE_HISTORY),
            serialize=_serialize_price_history,
            deserialize=_deserialize_price_history,
        )
        await self._price_history_store.load()

        self._forecast_quality_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_FORECAST_QUALITY),
            serialize=forecast_accuracy_module.store_to_dict,
            deserialize=forecast_accuracy_module.store_from_dict,
        )
        await self._forecast_quality_store.load()

        self._monthly_bill_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_MONTHLY_BILL),
            serialize=_serialize_monthly_bill,
            deserialize=_deserialize_monthly_bill,
        )
        await self._monthly_bill_store.load()

        self._yesterday_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_YESTERDAY),
            serialize=_serialize_yesterday,
            deserialize=_deserialize_yesterday,
        )
        await self._yesterday_store.load()

        self._mode_history_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_MODE_HISTORY),
            serialize=_serialize_mode_history,
            deserialize=_deserialize_mode_history,
        )
        await self._mode_history_store.load()

        self._counter_snapshot_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_COUNTER_SNAPSHOT),
            serialize=_serialize_counter_snapshot,
            deserialize=_deserialize_counter_snapshot,
        )
        await self._counter_snapshot_store.load()

        self._baked_observed_store = PersistentStore(
            self.hass, STORAGE_VERSION, ekey(STORAGE_KEY_BAKED_OBSERVED),
            serialize=_serialize_baked_observed,
            deserialize=_deserialize_baked_observed,
        )
        await self._baked_observed_store.load()

        # Backfill the consumption-daily store from any complete local days
        # already present in the derived-power history. With the standard
        # 2-day derived retention this seeds 1–2 records (typically
        # yesterday + day-before-yesterday) so the per-hour P15 builder has
        # something to chew on before the regular per-cycle finalise hook
        # accumulates fresh days. Idempotent — skipped for dates already
        # recorded.
        derived_store = self._history_stores.get(STORAGE_KEY_DERIVED_POWER)
        if (
            self._consumption_daily_store is not None
            and derived_store is not None
        ):
            local_tz = self._sun_sale_config.local_tz
            existing = (
                self._consumption_daily_store.value
                or ConsumptionDailyBuckets(records=())
            )
            derived_history = DerivedPowerHistory(
                samples=tuple(derived_store.value or []),
            )
            after = backfill_from_derived_history(
                derived_history=derived_history,
                existing=existing,
                local_tz=local_tz,
                now=datetime.now(timezone.utc),
            )
            if after is not existing:
                await self._consumption_daily_store.save(after)

    @contextmanager
    def _guarded(self, label: str):
        """Swallow and log any exception from a best-effort bookkeeping block.

        Isolates a per-cycle accounting / persistence side-effect so a single
        failure (a corrupt stored record, a try_bake_yesterday edge case) logs
        and the cycle continues — keeping sensor data fresh and, critically,
        never blocking the inverter dispatch. The ``with`` body may ``await``;
        exceptions raised inside it propagate here and are contained.

        Args:
            label: Human-readable name of the guarded step, used in the log.

        Yields:
            None; control returns to the ``with`` body.
        """
        try:
            yield
        except Exception:    # best-effort isolation by design — never re-raised
            _LOGGER.warning(
                "sunSale bookkeeping step '%s' failed; continuing", label,
                exc_info=True,
            )

    async def _async_update_data(self) -> dict:
        """One DAG cycle: translate → capacity update → DAG → event routing."""
        now = datetime.now(timezone.utc)

        try:
            primary = await run_translators(
                self._translators, self.hass, self._sun_sale_config, self._config, now
            )

            # Day boundaries must be LOCAL so the chart's yesterday/today/tomorrow
            # axis aligns with the persisted yesterday store. Computing from
            # now.date() (UTC) leaves a UTC-offset-sized window after local
            # midnight where the store has "today's UTC date" but the lookup
            # asks for "yesterday LOCAL" — they never match, yesterday silently
            # disappears from the chart.
            local_now     = now.astimezone(self._sun_sale_config.local_tz)
            today_str     = local_now.date().isoformat()
            yesterday_str = (local_now.date() - timedelta(days=1)).isoformat()

            nordpool_data: NordpoolData | None = primary.get(NordpoolData)
            solar_data: SolarData | None = primary.get(SolarData)

            buckets = (self._yesterday_store.value if self._yesterday_store else None) or _YesterdayBuckets()

            # Pricing: pass yesterday in via a primary input; inbound.pricing
            # owns the 72h yesterday→today→tomorrow assembly. Stored data older
            # than yesterday is treated as empty.
            yesterday_pricing_entries = (
                tuple(buckets.yesterday_nordpool)
                if buckets.yesterday_date == yesterday_str
                else ()
            )
            primary[YesterdayPrices] = YesterdayPrices(entries=yesterday_pricing_entries)

            if buckets.yesterday_date == yesterday_str and solar_data is not None:
                solar_data.entries = buckets.yesterday_solar + solar_data.entries

            # Persist today's slice; rotate yesterday at LOCAL date rollover.
            # Best-effort: the pricing/solar primaries above are already
            # assembled, so a rotation or save failure must not abort the cycle.
            with self._guarded("yesterday-bucket rotation"):
                if nordpool_data is not None and solar_data is not None and self._yesterday_store is not None:
                    _local = self._sun_sale_config.local_tz
                    today_nordpool = [e for e in nordpool_data.entries
                                       if e.start.astimezone(_local).date().isoformat() == today_str]
                    today_solar = [e for e in solar_data.entries
                                    if e.start.astimezone(_local).date().isoformat() == today_str]
                    new_buckets = _rotate_yesterday_buckets(buckets, today_str, today_nordpool, today_solar)
                    await self._yesterday_store.save(new_buckets)

            # Rolling sample histories — append each cycle's reading to its
            # store (trimming to the spec's retention) and inject the window
            # as the spec's *History primary. See history_stores.py.
            for spec in SAMPLE_HISTORY_SPECS:
                store = self._history_stores.get(spec.storage_key)
                # Seed the primary with the un-appended window first so that if
                # the append (a disk write) fails, the DAG still receives this
                # key and the cycle — including the inverter dispatch — proceeds.
                primary[spec.history_type] = spec.build_history(store)
                with self._guarded(f"history append {spec.storage_key}"):
                    await append_and_inject(
                        spec, store, primary, primary.get(spec.reading_type), now,
                    )

            # Derived-power cross-stream sample: composes AC port + backup +
            # battery (carries battery_power + grid_net_signed) + PV into one
            # synchronised tuple. Persisted as a rolling history so the
            # consumption + losses observers can power-average per slot. The
            # composer returns None when any source is unavailable this cycle —
            # partial samples would bias the per-slot mean asymmetrically.
            derived_store = self._history_stores.get(STORAGE_KEY_DERIVED_POWER)
            primary[DERIVED_POWER_SPEC.history_type] = DERIVED_POWER_SPEC.build_history(
                derived_store,
            )
            with self._guarded("derived-power append"):
                derived_sample = build_derived_power_sample(
                    now=now,
                    ac_port=primary.get(AcPortPowerReading),
                    backup=primary.get(BackupPowerReading),
                    battery=primary.get(BatteryReading),
                    pv=primary.get(PvPowerReading),
                )
                await append_and_inject(
                    DERIVED_POWER_SPEC, derived_store, primary, derived_sample, now,
                )

            # Lift the observer streams from per-tick (~5 min) to the inverter's
            # native update rate: the resampler fetches only the new recorder
            # rows since the last cycle, keeps a trimmed in-memory buffer per
            # source sensor, and rebuilds the full-resolution samples — merged
            # into each stream's primary before the DAG averages them per slot.
            # Best-effort — on recorder failure the coarse per-tick primaries
            # built above are used as-is.
            with self._guarded("recorder resample"):
                _local_midnight = local_now.replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
                _resample_start = (
                    _local_midnight.astimezone(timezone.utc) - timedelta(days=1)
                )
                resampled = await self._resampler.update(
                    self.hass, _resample_start, now,
                )
                for _key, _history_type in (
                    ("pv", PvPowerHistory),
                    ("grid_import", GridImportPowerHistory),
                    ("grid_export", GridExportPowerHistory),
                    ("derived", DerivedPowerHistory),
                ):
                    _extra = resampled.get(_key)
                    if not _extra:
                        continue
                    _current = primary.get(_history_type)
                    _existing = _current.samples if _current is not None else ()
                    primary[_history_type] = _history_type(
                        samples=merge_by_timestamp(_existing, _extra),
                    )

            # Inverter clock skew tracker — drives the snapshot window shift
            # below so the capture aligns with INVERTER-local midnight when
            # the two clocks have drifted apart. Returns ``None`` (no shift)
            # until enough samples have accumulated for confidence.
            current_inverter_time: InverterTimeReading | None = primary.get(
                InverterTimeReading,
            )
            clock_skew = None
            with self._guarded("inverter-time tracking"):
                self._inverter_time_history = update_inverter_time_history(
                    self._inverter_time_history, current_inverter_time,
                )
                clock_skew = current_skew_seconds(self._inverter_time_history)

            # Pre-rollover snapshot: capture today_total per side within the
            # late-evening window so the next-day bake-in has an authoritative
            # value when no dedicated yesterday-total sensor is mapped.
            with self._guarded("pre-rollover snapshot capture"):
                if self._counter_snapshot_store is not None:
                    current_snapshots = (
                        self._counter_snapshot_store.value
                        or CounterSnapshotHistory(records=())
                    )
                    updated_snapshots = maybe_capture_snapshots(
                        snapshot_history=current_snapshots,
                        sources=[
                            (GENERATION_SIDE_ID, primary.get(GenerationReading)),
                            (GRID_IMPORT_SIDE_ID, primary.get(GridImportTodayReading)),
                            (GRID_EXPORT_SIDE_ID, primary.get(GridExportTodayReading)),
                        ],
                        now=now,
                        local_tz=self._sun_sale_config.local_tz,
                        retention_days=COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS,
                        clock_skew_seconds=clock_skew,
                    )
                    if updated_snapshots is not current_snapshots:
                        await self._counter_snapshot_store.save(updated_snapshots)
            # Default to an empty history when the store exists but holds no
            # value yet (fresh install / first cycle). The DAG node consumes
            # these via ``ctx.require`` so an explicit empty fallback is
            # required — ``ctx.get`` returns the primary value whenever the key
            # is present, so a stored ``None`` would raise
            # ``MissingDependencyError``.
            current_snapshots = (
                self._counter_snapshot_store.value
                if self._counter_snapshot_store else None
            ) or CounterSnapshotHistory(records=())
            primary[CounterSnapshotHistory] = current_snapshots

            current_baked = (
                self._baked_observed_store.value
                if self._baked_observed_store else None
            ) or BakedObservedHistory(records=())
            primary[BakedObservedHistory] = current_baked

            primary[MonthlyBillState] = (
                self._monthly_bill_store.value if self._monthly_bill_store else None
            )

            # Consumption-daily rollup: finalise yesterday once per local date.
            # The hook is idempotent — when yesterday's record already exists
            # only trimming runs, so calling every cycle is fine. The primary
            # is populated from the (possibly just-updated) store value so the
            # BaseLoadProfile node sees the latest rolling window.
            with self._guarded("consumption-daily finalise"):
                if (
                    self._consumption_daily_store is not None
                    and derived_store is not None
                    and self._sun_sale_config is not None
                ):
                    existing_buckets = (
                        self._consumption_daily_store.value
                        or ConsumptionDailyBuckets(records=())
                    )
                    derived_for_finalise = primary.get(DerivedPowerHistory)
                    if derived_for_finalise is None:
                        derived_for_finalise = DerivedPowerHistory(
                            samples=tuple(derived_store.value or []),
                        )
                    updated_buckets = try_finalise_yesterday_consumption(
                        derived_history=derived_for_finalise,
                        existing=existing_buckets,
                        local_tz=self._sun_sale_config.local_tz,
                        now=now,
                    )
                    if updated_buckets is not existing_buckets:
                        await self._consumption_daily_store.save(updated_buckets)
            primary[ConsumptionDailyBuckets] = (
                self._consumption_daily_store.value
                if self._consumption_daily_store is not None
                else None
            ) or ConsumptionDailyBuckets(records=())

            primary[PriceHistory] = PriceHistory(
                peaks=tuple((self._price_history_store.value or []) if self._price_history_store else []),
            )
            primary[ForecastQualityStore] = (
                self._forecast_quality_store.value if self._forecast_quality_store else None
            ) or ForecastQualityStore()
            primary[SunTimes] = self._read_sun_times(now)

            current_reading: BatteryReading | None = primary.get(BatteryReading)
            with self._guarded("capacity observation"):
                if current_reading is not None:
                    obs = self._build_capacity_observation(current_reading, now)
                    if obs is not None:
                        self._capacity_estimator.add_observation(obs)
                        if self._capacity_store is not None:
                            await self._capacity_store.save(self._capacity_estimator)

            primary[EstimatedCapacity] = EstimatedCapacity(
                value_kwh=self._capacity_estimator.estimated_capacity_kwh
            )

            primary[SchedulePolicy] = SchedulePolicy(
                use_standby=self.use_standby,
                allow_grid_charging=self.allow_grid_charging,
                allow_feed_in=self.allow_feed_in,
                allow_discharge_to_grid=self.allow_discharge_to_grid,
                mode_change_penalty_eur_per_kwh=_clamp(
                    self.mode_change_penalty_eur_per_kwh,
                    SCHEDULE_MODE_CHANGE_PENALTY_MIN,
                    SCHEDULE_MODE_CHANGE_PENALTY_MAX,
                ),
                profitability_tilt_alpha=_clamp(
                    self.profitability_tilt_alpha,
                    SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
                    SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
                ),
                terminal_value_discount=_clamp(
                    self.terminal_value_discount,
                    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
                    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
                ),
                max_discharge_to_grid_kw=(
                    _clamp(
                        self.max_discharge_to_grid_kw,
                        SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
                        SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
                    )
                    if self.max_discharge_to_grid_kw is not None
                    else None
                ),
            )

            secondary = await self._engine.run(primary, self._sun_sale_config, now)

            # --- Inverter actuation, ahead of the persistence bookkeeping.
            #     A dispatch failure is logged but never blanks the sensor set,
            #     and the accounting below can never block the inverter write
            #     (it ran already). The verify loop recovers a failed write. ---
            try:
                await self._dispatch_inverter_mode(primary, secondary, now)
            except Exception:    # keep sensors available; verify loop recovers
                _LOGGER.error("sunSale inverter dispatch failed", exc_info=True)

            # --- Persistence bookkeeping: best-effort, never fatal to the cycle
            #     nor to the dispatch above. ---
            await self._persist_secondary_outputs(primary, secondary, now)

            return self._build_sensor_dict(primary, secondary)

        except Exception as exc:
            raise UpdateFailed(f"Error updating sunSale data: {exc}") from exc

    async def _dispatch_inverter_mode(
        self, primary: dict, secondary: dict, now: datetime,
    ) -> None:
        """Run the control-module tick and surface its dispatch/verify state.

        Pulled out of ``_async_update_data`` and run *before* the persistence
        bookkeeping so an accounting failure can never block the inverter
        write. The caller guards this call, so a dispatch failure is logged
        without blanking the sensor set. Mutates ``primary[InverterModeHistory]``
        in place with the updated history.

        Args:
            primary: The cycle's primary-input dict; supplies the current
                ``InverterModeReading`` and receives the updated mode history.
            secondary: DAG outputs; supplies the current ``Schedule``.
            now: Current cycle timestamp.
        """
        reading: InverterModeReading | None = primary.get(InverterModeReading)
        if (
            self._control_module is None
            or reading is None
            or self._mode_history_store is None
        ):
            return

        schedule: Schedule | None = secondary.get(Schedule)
        history_before = (
            self._mode_history_store.value or InverterModeHistory(samples=())
        )
        updated_history = await self._control_module.tick(
            now=now,
            schedule=schedule,
            reading=reading,
            history=history_before,
            automation_enabled=self.automation_enabled,
            mode_override=self.mode_override,
        )
        if updated_history.samples != history_before.samples:
            await self._mode_history_store.save(updated_history)
        target = self._control_module.current_target(
            now, schedule, mode_override=self.mode_override,
        )
        # Surface module-level dispatch + verify state for the diagnostic
        # sensor — set every cycle regardless of write success so a stale
        # value never masks the current state.
        self._mirror_control_module_state()
        if self.automation_enabled and target is not None:
            self.last_dispatched_action = target.value
            self.last_dispatched_at = now
        primary[InverterModeHistory] = updated_history

    async def _persist_secondary_outputs(
        self, primary: dict, secondary: dict, now: datetime,
    ) -> None:
        """Persist DAG-derived bookkeeping: bake-ins and forecast/bill/price history.

        Best-effort and order-independent. Each block is guarded on its own so
        a single failure (a corrupt record, a ``try_bake_yesterday`` edge case)
        logs and is swallowed rather than aborting the cycle. Runs *after* the
        inverter dispatch, so a failure here can never block the inverter write.

        Args:
            primary: The cycle's primary-input dict (observed power histories,
                snapshot/baked stores).
            secondary: DAG outputs (``PriceSeries``, accuracy/bill results).
            now: Current cycle timestamp.
        """
        # Bake-in: idempotent per (date, side). Runs once the post-DAG
        # PriceSeries is available; updates take effect downstream on the
        # next coordinator cycle. No-op when no source can be resolved
        # before the hard cutoff.
        with self._guarded("observed bake-in"):
            pricing_for_bake: PriceSeries | None = secondary.get(PriceSeries)
            if (
                pricing_for_bake is not None
                and self._baked_observed_store is not None
            ):
                local_tz = self._sun_sale_config.local_tz
                baked_before = (
                    self._baked_observed_store.value
                    or BakedObservedHistory(records=())
                )
                snap_history = (
                    self._counter_snapshot_store.value
                    if self._counter_snapshot_store else None
                ) or CounterSnapshotHistory(records=())
                pv_samples = primary.get(PvPowerHistory)
                grid_import_samples = primary.get(GridImportPowerHistory)
                grid_export_samples = primary.get(GridExportPowerHistory)

                baked_after = baked_before
                if pv_samples is not None:
                    baked_after = try_bake_yesterday(
                        engine=build_generation_engine(local_tz),
                        samples_by_side={GENERATION_SIDE_ID: pv_samples.samples},
                        price_slots=pricing_for_bake.slots,
                        baked_history=baked_after,
                        snapshot_history=snap_history,
                        hass=self.hass,
                        raw_config=self._config,
                        now=now,
                        local_tz=local_tz,
                    )
                if grid_import_samples is not None or grid_export_samples is not None:
                    baked_after = try_bake_yesterday(
                        engine=build_grid_engine(local_tz),
                        samples_by_side={
                            GRID_IMPORT_SIDE_ID: (
                                grid_import_samples.samples if grid_import_samples else ()
                            ),
                            GRID_EXPORT_SIDE_ID: (
                                grid_export_samples.samples if grid_export_samples else ()
                            ),
                        },
                        price_slots=pricing_for_bake.slots,
                        baked_history=baked_after,
                        snapshot_history=snap_history,
                        hass=self.hass,
                        raw_config=self._config,
                        now=now,
                        local_tz=local_tz,
                    )

                if baked_after is not baked_before:
                    await self._baked_observed_store.save(baked_after)

        with self._guarded("forecast-quality save"):
            acc_result: ForecastAccuracyResult | None = secondary.get(ForecastAccuracyResult)
            if acc_result is not None and self._forecast_quality_store is not None:
                await self._forecast_quality_store.save(acc_result.quality)

        with self._guarded("monthly-bill save"):
            bill_result: MonthlyBillResult | None = secondary.get(MonthlyBillResult)
            if bill_result is not None and self._monthly_bill_store is not None:
                await self._monthly_bill_store.save(bill_result.updated_state)

        with self._guarded("price-history save"):
            pricing_result: PriceSeries | None = secondary.get(PriceSeries)
            if pricing_result is not None and self._price_history_store is not None:
                # Key by local date, not now.date() (UTC). The profitability
                # scorer resolves "today" in local time, so the history it
                # ranks against must be keyed the same way — otherwise the
                # first UTC-offset hours of each local day land in the wrong
                # bucket and skew day-class classification.
                local_tz = self._sun_sale_config.local_tz
                today_local = now.astimezone(local_tz).date()
                today_peak_val = profitability_module.today_peak_from_price_series(
                    pricing_result, today_local, local_tz
                )
                if today_peak_val is not None:
                    new_peak = DailyPeak(
                        day=today_local,
                        peak_eur_kwh=today_peak_val,
                        day_class=profitability_module.classify_day(today_local),
                    )
                    peaks = [p for p in (self._price_history_store.value or []) if p.day != today_local]
                    peaks.append(new_peak)
                    peaks.sort(key=lambda p: p.day)
                    cutoff_day = today_local - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
                    peaks = [p for p in peaks if p.day >= cutoff_day]
                    await self._price_history_store.save(peaks)

    @staticmethod
    def _validate_driver_role_contract(
        platform: InverterPlatform, driver: object,
    ) -> None:
        """Warn when an entity-driven driver references roles its profile can't supply.

        Closes the implicit string-coupling between a platform's write-side driver
        (which names roles in its plans) and its read-side
        :class:`..inbound.platform_profiles.ControlProfile` (which resolves them):
        any role the driver references but the profile does not declare can never
        be populated, so that write would silently degrade to a logged skip. The
        same invariant is asserted for every platform by the profile tests; this
        runtime check catches a field deployment whose profile was edited out of
        step. It only warns (the actuator already degrades safely) — a misconfig
        should not brick setup.

        Args:
            platform: The configured inverter platform.
            driver: The control driver built for it (only entity-driven drivers
                expose ``referenced_roles``; others are skipped).
        """
        profile = profile_for(platform)
        referenced = getattr(driver, "referenced_roles", None)
        if profile is None or referenced is None:
            return
        missing = unbacked_roles(profile, referenced())
        if missing:
            _LOGGER.warning(
                "%s driver references roles %s not declared in its profile — those "
                "writes will be skipped; fix platform_profiles.py",
                platform.value, sorted(missing),
            )

    def _resolve_forecast_base_entities(self, data: dict) -> list[str]:
        """Resolve the base forecast entities SolarTranslator should read.

        Prefers device-based selection: when ``CONF_SOLAR_FORECAST_DEVICE_IDS``
        is configured, each chosen forecast-integration config entry is resolved
        to its base "today" sensor. Falls back to the two legacy entity keys
        (``CONF_SOLAR_FORECAST_ENTITY`` / ``_2``) when no device IDs are set or
        none resolve, so existing configs keep working unchanged.

        Args:
            data: The merged config-entry data dict.

        Returns:
            List of base forecast entity IDs (possibly empty).
        """
        device_ids = data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []
        if device_ids:
            resolved = resolve_forecast_entities(self.hass, list(device_ids))
            if resolved:
                return resolved
        return [
            e
            for e in (
                data.get(CONF_SOLAR_FORECAST_ENTITY, ""),
                data.get(CONF_SOLAR_FORECAST_ENTITY_2, ""),
            )
            if e
        ]

    def _resolve_local_tz(self):
        """Return the HA-configured local timezone, falling back to UTC.

        Reads hass.config.time_zone (e.g. "Europe/Riga"). Falls back to UTC
        when unset, unknown, or unparseable; baseload bucketing depends on this.

        Returns:
            tzinfo for the HA installation's local timezone.
        """
        tz_name = getattr(self.hass.config, "time_zone", None)
        if not tz_name:
            return timezone.utc
        try:
            return ZoneInfo(tz_name)
        except Exception:    # ZoneInfoNotFoundError + anything weird from HA mocks
            return timezone.utc

    def _build_live_flow_sources(self) -> dict[str, Any] | None:
        """Build the per-leg source specs the panel reads live for the flow diagram.

        The live energy-flow diagram is instantaneous power, but the
        server-computed ``live_flows`` only refreshes once per ~5-min coordinator
        cycle (and is starved further by the derived-sample freshness gate). To
        track the inverter in real time the panel instead reads the raw source
        sensors directly on every HA state push — but it must not re-implement
        the platform-specific sign / unit / role-selection logic that
        ``InverterController`` and the derived-power composer apply server-side.

        This method resolves, per flow leg, the single entity the server would
        read this cycle plus the ``{scale, sign, clamp_min}`` transform that maps
        its raw state to a sunSale-convention kW value:

          * ``scale`` folds the entity's ``unit_of_measurement`` → kW.
          * ``sign`` carries the convention flip (e.g. Solis battery/grid net
            sensors, or the grid-port fallback) — mirrors
            ``InverterController.get_battery_power`` / ``get_grid_power``.
          * ``clamp_min`` is ``0.0`` for the source-only legs (solar, backup),
            matching the ``max(0, …)`` clamps in
            ``build_derived_power_sample``.

        The panel then evaluates ``value * scale * sign`` (clamped) and the same
        ``home`` / ``losses`` formulas as ``_build_live_flows``.

        Returns:
            Mapping of leg name (``solar`` / ``battery`` / ``grid`` / ``ac_port``
            / ``backup``) → spec dict (or ``None`` for an unmapped leg), or
            ``None`` when no leg resolves — the panel then falls back to the
            cycle-computed ``live_flows``.
        """
        ids = self._inverter_entity_ids

        def power_spec(
            entity_id: str | None,
            sign: int = 1,
            clamp_min: float | None = None,
        ) -> dict[str, Any] | None:
            """Resolve a unit-aware spec for one entity, or None when unmapped."""
            if not entity_id:
                return None
            state = self.hass.states.get(entity_id)
            unit = ""
            if state is not None:
                unit = str((state.attributes or {}).get("unit_of_measurement") or "")
            spec: dict[str, Any] = {
                "entity_id": entity_id,
                "scale": power_unit_scale(unit),
                "sign": sign,
            }
            if clamp_min is not None:
                spec["clamp_min"] = clamp_min
            return spec

        # Solar — the PV translator's own unit rule: the attribute defaults to
        # "W" when absent, and only an exact "W" is treated as watts (anything
        # else is kW). Replicated here rather than via power_unit_scale.
        solar: dict[str, Any] | None = None
        if self._pv_power_entity_id:
            pv_state = self.hass.states.get(self._pv_power_entity_id)
            pv_unit = "W"
            if pv_state is not None:
                pv_unit = str(
                    (pv_state.attributes or {}).get("unit_of_measurement") or "W"
                )
            solar = {
                "entity_id": self._pv_power_entity_id,
                "scale": 0.001 if pv_unit.upper() == "W" else 1.0,
                "sign": 1,
                "clamp_min": 0.0,
            }

        # Battery — signed role preferred; on Solis its net sensor is
        # discharge-positive, so flip to sunSale's positive=charging. The
        # magnitude fallback carries no direction and is read as-is. The flip
        # comes from the codec so it stays in lockstep with the live getters.
        if ids.get("battery_power_signed"):
            battery = power_spec(
                ids["battery_power_signed"],
                sign=signed_polarity(
                    self._telemetry_codec,
                    TelemetrySignal.BATTERY_POWER,
                    SignalRole.SIGNED_NET,
                ),
            )
        else:
            battery = power_spec(ids.get("battery_power"), sign=1)

        # Grid — the net sensor already matches positive=import; the ac-port
        # fallback is positive=inverter→grid and needs a flip on Solis.
        if ids.get("grid_power"):
            grid = power_spec(ids["grid_power"], sign=1)
        else:
            grid = power_spec(
                ids.get("grid_power_fallback"),
                sign=signed_polarity(
                    self._telemetry_codec,
                    TelemetrySignal.GRID_NET_POWER,
                    SignalRole.FALLBACK,
                ),
            )

        ac_port = power_spec(self._ac_port_power_entity_id, sign=1)
        backup = power_spec(self._backup_power_entity_id, sign=1, clamp_min=0.0)

        legs = {
            "solar": solar,
            "battery": battery,
            "grid": grid,
            "ac_port": ac_port,
            "backup": backup,
        }
        if not any(legs.values()):
            return None
        return legs

    def _build_sensor_dict(self, primary: dict, secondary: dict) -> dict:
        """Map typed DAG outputs to the string-keyed dict consumed by sensor entities.

        Args:
            primary: Translation-layer outputs keyed by type.
            secondary: DAG node outputs keyed by type.

        Returns:
            Dict with string keys matching what each sensor entity reads from
            coordinator.data.
        """
        reading: BatteryReading | None = primary.get(BatteryReading)
        imp_power: GridImportPowerReading | None = primary.get(GridImportPowerReading)
        exp_power: GridExportPowerReading | None = primary.get(GridExportPowerReading)
        # Signed grid power = import − export. Used by the dashboard sensor;
        # downstream pipeline reads each direction's history directly.
        grid_power_kw_signed = (
            (imp_power.power_kw if imp_power else 0.0)
            - (exp_power.power_kw if exp_power else 0.0)
        ) if (imp_power is not None or exp_power is not None) else 0.0
        nordpool: NordpoolData | None = primary.get(NordpoolData)
        deg: DegradationCost | None = secondary.get(DegradationCost)
        consumption: HouseholdConsumptionReading | None = primary.get(
            HouseholdConsumptionReading,
        )

        _acc: ForecastAccuracyResult | None = secondary.get(ForecastAccuracyResult)
        return {
            "pricing": secondary.get(PriceSeries),
            "forecast": secondary.get(GenerationSeries),
            "observed_generation": secondary.get(ObservedGenerationSeries),
            "observed_grid": secondary.get(ObservedGridSeries),
            "observed_consumption": secondary.get(ObservedConsumptionSeries),
            "observed_losses": secondary.get(ObservedLossesSeries),
            "forecast_error": _acc.error_series if _acc else None,
            "calculation": secondary.get(CalculationResult),
            "schedule": secondary.get(Schedule),
            "battery_state": secondary.get(BatteryState),
            "battery_status": secondary.get(BatteryStatus),
            "degradation_cost": deg.value_kwh if deg else 0.0,
            "estimated_capacity": self._capacity_estimator.estimated_capacity_kwh,
            "capacity_observations": self._capacity_estimator.debug_observations(),
            "prices": nordpool.entries if nordpool else [],
            "grid_power_kw": grid_power_kw_signed,
            "live_flow_sources": self._build_live_flow_sources(),
            # Deployment export-power cap (kW) — the panel scales the power-flow
            # chart's kW axis to this + headroom. Configured value, not the live
            # backflow setting (which drops to 0 in non-export modes).
            "export_limit_kw": self._export_limit_w / 1000.0,
            "battery_power_kw": reading.power_kw if reading else 0.0,
            "household_load_kw": reading.household_load_kw if reading else 0.0,
            "base_load_profile": secondary.get(BaseLoadProfile),
            "battery_runtime": secondary.get(BatteryRuntimeEstimate),
            "profitability_score": secondary.get(ProfitabilityScore),
            "consumption_today_kwh": (
                consumption.today_total_kwh if consumption else None
            ),
            "forecast_quality": _acc.quality if _acc else None,
            "sun_times": primary.get(SunTimes),
            "monthly_bill": secondary.get(MonthlyBillResult),
            "grid_import_power_history": primary.get(GridImportPowerHistory),
            "grid_export_power_history": primary.get(GridExportPowerHistory),
            "pv_power_history": primary.get(PvPowerHistory),
            "derived_power_history": primary.get(DerivedPowerHistory),
            "inverter_mode_history": primary.get(InverterModeHistory),
            "inverter_mode_reading": primary.get(InverterModeReading),
            "baked_observed_history": primary.get(BakedObservedHistory),
            "counter_snapshot_history": primary.get(CounterSnapshotHistory),
            "consumption_daily_buckets": primary.get(ConsumptionDailyBuckets),
            "today_generation_live_kwh": (
                primary.get(GenerationReading).today_total_kwh
                if primary.get(GenerationReading) is not None else None
            ),
            "today_imported_live_kwh": (
                primary.get(GridImportTodayReading).today_total_kwh
                if primary.get(GridImportTodayReading) is not None else None
            ),
            "today_exported_live_kwh": (
                primary.get(GridExportTodayReading).today_total_kwh
                if primary.get(GridExportTodayReading) is not None else None
            ),
        }

    def _read_sun_times(self, now: datetime) -> SunTimes:
        """Read today's sunrise and sunset from the sun.sun HA entity.

        HA only exposes *next* rising/setting. If the next event is today
        (sun hasn't risen yet) it is this cycle's sunrise; if it's tomorrow,
        today's sunrise was approximately one sidereal day earlier.

        Args:
            now: Current cycle UTC timestamp for computing today's local date.

        Returns:
            SunTimes with today_sunrise and today_sunset in UTC, or None fields
            when the sun.sun entity is unavailable.
        """
        local_today = now.astimezone(self._sun_sale_config.local_tz).date()

        def _parse(attr: str) -> datetime | None:
            """Read a sun.sun datetime attribute as UTC-aware, or None when missing."""
            state = self.hass.states.get("sun.sun")
            if state is None:
                return None
            raw = state.attributes.get(attr)
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(raw)
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None

        def _today_event(next_event: datetime | None) -> datetime | None:
            """Map a sun.sun "next_*" event to today's instance of that event."""
            if next_event is None:
                return None
            local_date = next_event.astimezone(self._sun_sale_config.local_tz).date()
            if local_date == local_today:
                return next_event
            # next_event is tomorrow → today's event was ~1 day ago
            return next_event - timedelta(days=1)

        next_rising  = _parse("next_rising")
        next_setting = _parse("next_setting")
        return SunTimes(
            today_sunrise=_today_event(next_rising),
            today_sunset=_today_event(next_setting),
        )

    def _build_capacity_observation(
        self,
        current: BatteryReading,
        now: datetime,
    ) -> CapacityObservation | None:
        """Emit a capacity observation once SoC has swung far enough since the anchor.

        Real SoC moves only a few percent per 5-min cycle, so a single-cycle
        delta is too small and too quantised to estimate capacity from. Instead an
        *anchor* reading is held fixed across cycles and cycle energy is taken from
        the inverter's own daily charge/discharge counters (clean and
        authoritative — unlike a two-endpoint power average). When the net SoC
        swing since the anchor reaches ``CAPACITY_OBS_EMIT_SOC_DELTA`` in one
        direction and the window was "clean" (the off-direction counter barely
        moved), an observation is emitted with the matching counter's energy delta
        and the anchor advances to ``current``.

        The anchor is (re)set to ``current`` — returning None — when it is unset
        (first valid reading), when either energy counter is missing (no clean
        energy source, so the estimator simply does not learn), when a counter ran
        backwards (daily midnight reset), or when the window outlived
        ``CAPACITY_OBS_MAX_WINDOW_S``.

        Args:
            current: Most recent battery reading (needs both energy counters).
            now: Cycle timestamp; the observation timestamp and window endpoint.

        Returns:
            A CapacityObservation when a clean swing completed this cycle, else None.
        """
        anchor = self._cap_anchor_reading
        if (
            anchor is None
            or self._cap_anchor_at is None
            or current.charge_energy_today_kwh is None
            or current.discharge_energy_today_kwh is None
            or anchor.charge_energy_today_kwh is None
            or anchor.discharge_energy_today_kwh is None
            or current.charge_energy_today_kwh
            < anchor.charge_energy_today_kwh - CAPACITY_OBS_COUNTER_RESET_EPS_KWH
            or current.discharge_energy_today_kwh
            < anchor.discharge_energy_today_kwh - CAPACITY_OBS_COUNTER_RESET_EPS_KWH
            or (now - self._cap_anchor_at).total_seconds() > CAPACITY_OBS_MAX_WINDOW_S
        ):
            self._cap_anchor_reading = current
            self._cap_anchor_at = now
            return None

        soc_delta = current.soc - anchor.soc
        if abs(soc_delta) < CAPACITY_OBS_EMIT_SOC_DELTA:
            return None  # keep accumulating against the fixed anchor

        charge_delta = (
            current.charge_energy_today_kwh - anchor.charge_energy_today_kwh
        )
        discharge_delta = (
            current.discharge_energy_today_kwh - anchor.discharge_energy_today_kwh
        )
        direction = "charge" if soc_delta > 0 else "discharge"
        on_energy = charge_delta if direction == "charge" else discharge_delta
        off_energy = discharge_delta if direction == "charge" else charge_delta

        # Advance past the completed swing whether or not it yields a usable
        # sample, so the next window starts fresh from here.
        self._cap_anchor_reading = current
        self._cap_anchor_at = now

        # Discard ambiguous windows: no on-direction energy, or the battery
        # oscillated both ways (off-direction energy is not negligible).
        if on_energy <= 0 or off_energy > CAPACITY_OBS_PURITY_FRACTION * on_energy:
            return None

        return CapacityObservation(
            timestamp=now,
            soc_start=anchor.soc,
            soc_end=current.soc,
            energy_kwh=on_energy,
            direction=direction,
        )
