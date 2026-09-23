"""sunSale DataUpdateCoordinator — thin orchestrator for the DAG pipeline.

Responsibilities:
  1. Build translators, DAG nodes, and engine from config.
  2. Manage CapacityEstimator state across cycles (pre-DAG update + persistence).
  3. Run translators (parallel) → deposit EstimatedCapacity → run engine.
  4. Drive the inverter control module's dispatch tick from the DAG's Schedule.
  5. Map typed DAG outputs to the string-keyed coordinator.data dict for sensors.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover — Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..contract.const import (
    CAPACITY_OBS_COUNTER_RESET_EPS_KWH,
    CAPACITY_OBS_EMIT_SOC_DELTA,
    CAPACITY_OBS_MAX_WINDOW_S,
    CAPACITY_OBS_PURITY_FRACTION,
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
    CONF_CURRENCY,
    CONF_FORECAST_RESERVE_ENABLED,
    CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY,
    CONF_INVERTER_EXPORT_LIMIT_KW,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_NORDPOOL_ENTITY,
    CONF_NORDPOOL_RESOLUTION,
    CONF_PRICE_EXPORT_ENTITY,
    CONF_PRICE_SOURCE,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_CURRENCY,
    DEFAULT_EXPORT_LIMIT_W,
    DEFAULT_FORECAST_RESERVE_ENABLED,
    DEFAULT_INVERTER_EXPORT_LIMIT_KW,
    DEFAULT_INVERTER_MAX_POWER_KW,
    DEFAULT_PRICE_SOURCE,
    DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID,
    DEFAULT_SCHEDULE_ALLOW_FEED_IN,
    DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING,
    DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW,
    DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH,
    DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA,
    DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT,
    DEFAULT_SCHEDULE_USE_STANDBY,
    DOMAIN,
    GRID_POWER_HISTORY_RETENTION_DAYS,
    PRICE_CURVE_RETENTION_DAYS,
    PRICE_ERROR_RETENTION_DAYS,
    PRICE_HISTORY_RETENTION_DAYS,
    PRICE_SOURCE_FIXED,
    SCHEDULE_SLOT_MINUTES,
    STORAGE_KEY_ARRAY_CALIBRATION,
    STORAGE_KEY_BAKED_OBSERVED,
    STORAGE_KEY_CAPACITY,
    STORAGE_KEY_CONSUMPTION_DAILY,
    STORAGE_KEY_COUNTER_SNAPSHOT,
    STORAGE_KEY_DERIVED_POWER,
    STORAGE_KEY_FORECAST_QUALITY,
    STORAGE_KEY_MODE_HISTORY,
    STORAGE_KEY_MONTHLY_BILL,
    STORAGE_KEY_PRICE_CURVE_HISTORY,
    STORAGE_KEY_PRICE_HISTORY,
    STORAGE_KEY_YESTERDAY,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from ..contract.install_capabilities import CAP_PRICES, resolve_install_capabilities
from ..contract.models import (
    ArrayCalibration,
    BakedObservedHistory,
    BaseLoadProfile,
    BatteryConfig,
    BatteryReading,
    BatteryRuntimeEstimate,
    BatteryState,
    BatteryStatus,
    CalculationResult,
    CapacityObservation,
    ConsumptionDailyBuckets,
    CounterSnapshotHistory,
    DailyPeak,
    DegradationCost,
    DerivedPowerHistory,
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
    HouseholdConsumptionReading,
    InverterCapability,
    InverterModeHistory,
    InverterModeReading,
    MonthlyBillResult,
    MonthlyBillState,
    NordpoolData,
    ObservedConsumptionSeries,
    ObservedGenerationSeries,
    ObservedGridSeries,
    ObservedLossesSeries,
    PriceCurveHistory,
    PriceForecast,
    PriceLevelSeries,
    PriceSeries,
    ProfitabilityScore,
    PvPowerHistory,
    Schedule,
    SolarHealth,
    StorageMode,
    SunSaleConfig,
    SunTimes,
    TariffConfig,
    WeatherForecastData,
)
from ..ha_state import normalize_power_to_kw, power_unit_scale, read_power_kw
from ..inbound import price_backfill
from ..inbound.battery import BatteryTranslator
from ..inbound.battery_source import (
    BatterySource,
    ChainedBatterySource,
    InverterBatterySource,
    JkBmsBatterySource,
)
from ..inbound.consumption_daily import (
    backfill_from_derived_history,
)
from ..inbound.forecast import SolarTranslator
from ..inbound.forecast_resolver import combine_forecast_entities, resolve_forecast_entities
from ..inbound.holiday_calendar import holiday_predicate, preload_holidays
from ..inbound.household_consumption import HouseholdConsumptionTranslator
from ..inbound.inverter_entity_resolver import resolve_inverter_entities
from ..inbound.inverter_mode import InverterModeTranslator
from ..inbound.observer.bake_in import try_bake_yesterday
from ..inbound.observer.derived import (
    AcPortPowerTranslator,
    BackupPowerTranslator,
)
from ..inbound.observer.generation import (
    GENERATION_SIDE_ID,
    GenerationTranslator,
    PvPowerTranslator,
    build_generation_engine,
)
from ..inbound.observer.grid import (
    GRID_EXPORT_SIDE_ID,
    GRID_IMPORT_SIDE_ID,
    GridExportPowerObserver,
    GridExportTotalTranslator,
    GridImportPowerObserver,
    GridImportTotalTranslator,
    build_grid_engine,
)
from ..inbound.observer.recorder_resample import (
    RecorderResampler,
    ResampleSources,
)
from ..inbound.platform_profiles import profile_for, unbacked_roles
from ..inbound.pricing import build_price_translator
from ..inbound.telemetry import (
    SignalRole,
    TelemetryCodec,
    TelemetrySignal,
    signed_polarity,
)
from ..inbound.weather import WeatherTranslator
from ..outbound.driver_factory import make_inverter_driver
from ..outbound.entity_control import InverterContext
from ..outbound.export_guard import (
    TRANSITION_CLEARED,
    TRANSITION_TRIPPED,
    ExportGuard,
    SocFloorWatch,
)
from ..outbound.inverter import (
    InverterController,
    InverterPlatform,
)
from ..outbound.inverter_control_module import InverterControlModule
from ..pipeline import online_shape
from ..pipeline import price_fill as price_fill_module
from ..pipeline import price_forecast as price_forecast_module
from ..pipeline import profitability as profitability_module
from ..pipeline import tariff as tariff_module
from ..pipeline.battery import CapacityEstimator
from ..pipeline.dag_engine import DagEngine, run_translators
from ..pipeline.nodes import (
    ArrayCalibrationNode,
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
    PriceForecastNode,
    PriceLevelNode,
    PricingNode,
    ProfitabilityNode,
    ScheduleNode,
)
from ..pipeline.price_level import config_from_entry as price_level_config_from_entry
from .cycle_steps import (
    CapacityStep,
    ConsumptionDailyStep,
    CycleStep,
    DerivedSampleStep,
    PreRolloverSnapshotStep,
    RecorderResampleStep,
    SampleHistoryStep,
    ScheduleKnobs,
    SchedulePolicyStep,
    StoredPrimariesStep,
    WeatherStep,
    YesterdayRotationStep,
)
from .history_stores import (
    ALL_HISTORY_SPECS,
    GRID_EXPORT_POWER_SPEC,
    GRID_IMPORT_POWER_SPEC,
)
from .persistent_store import (
    PersistentStore,
    entry_storage_key,
)
from .price_feed_watchdog import PriceFeedWatchdog
from .store_codecs import (
    SINGLETON_STORE_SPECS,
    YesterdayBuckets,
)

_LOGGER = logging.getLogger(__name__)


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
        # No ``update_interval``: the base class's timer free-runs from whenever
        # the last refresh happened, so its phase relative to the wall clock is
        # set by HA's start time and the cycle that activates a new schedule slot
        # lands a constant 0–5 min *after* the slot began. Cycles are driven
        # instead by a wall-clock-aligned tick (``_start_aligned_tick``) that
        # always fires exactly on the slot boundaries.
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self._entry = config_entry
        # Reloads the integration behind a price sensor that has gone dead and
        # will not come back on its own; see price_feed_watchdog.py.
        self._price_feed_watchdog = PriceFeedWatchdog(hass, config_entry.entry_id)
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
        self._yesterday_store: PersistentStore[YesterdayBuckets] | None = None
        # Rolling sample-history stores keyed by storage key — populated in
        # async_setup from ``history_stores.ALL_HISTORY_SPECS``. See
        # orchestration/history_stores.py.
        self._history_stores: dict[str, PersistentStore] = {}
        # Singleton (non-history) stores keyed by base storage key — populated
        # in async_setup from ``store_codecs.SINGLETON_STORE_SPECS``. The named
        # ``self._<name>_store`` attributes below alias into this dict.
        self._stores: dict[str, PersistentStore] = {}
        self._consumption_daily_store: PersistentStore[ConsumptionDailyBuckets] | None = None
        self._price_history_store: PersistentStore[list[DailyPeak]] | None = None
        self._price_curve_store: PersistentStore[PriceCurveHistory] | None = None
        self._forecast_quality_store: PersistentStore[ForecastQualityStore] | None = None
        self._array_calibration_store: PersistentStore[ArrayCalibration] | None = None
        self._grid_import_power_entity_id: str = ""
        self._grid_export_power_entity_id: str = ""
        self._monthly_bill_store: PersistentStore[MonthlyBillState] | None = None
        self._mode_history_store: PersistentStore[InverterModeHistory] | None = None
        self._counter_snapshot_store: PersistentStore[CounterSnapshotHistory] | None = None
        self._baked_observed_store: PersistentStore[BakedObservedHistory] | None = None
        # Ordered pre-DAG primary-assembly steps — built at the end of
        # async_setup once all stores exist. See orchestration/cycle_steps.py.
        self._cycle_steps: tuple[CycleStep, ...] = ()
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
        # Platform control driver, assigned in async_setup. Read back here only
        # for its live capability projection; dispatch goes through the control
        # module, which owns its own reference.
        self._inverter_driver: Any = None
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
        # Which write path the driver picked for the forced modes (Solis:
        # ``dispatch`` vs ``rc``). Chosen once at setup from what the install
        # actually supports, so it is otherwise invisible to an operator
        # debugging a discharge.
        self.control_path: str | None = None
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
        # Battery-export guard (monitor only): fed on every grid / PV state
        # change, not the 5-min cycle, so its 120 s persistence is meaningful.
        # See outbound/export_guard.py and docs/battery_export_guard.md.
        self._export_guard = ExportGuard()
        self._soc_floor_watch = SocFloorWatch()
        self._unsub_export_guard: Callable[[], None] | None = None
        # Latest SoC from the cycle's BatteryState, recorded at an episode onset.
        self._last_soc: float | None = None
        # Unsubscribe for the wall-clock-aligned cycle tick (set in
        # ``async_setup``), and its re-entrancy guard — unlike the base class's
        # interval, which only re-arms once a refresh has finished, the aligned
        # tick fires on absolute times and would otherwise stack a second cycle
        # on top of one that overran its interval.
        self._unsub_aligned_tick: Callable[[], None] | None = None
        self._cycle_in_flight: bool = False

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
        now = datetime.now(UTC)
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
        self.control_path = getattr(module.driver, "control_path", None)
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

    @staticmethod
    def _aligned_tick_minutes() -> list[int]:
        """Return the minutes-past-the-hour the cycle tick fires on.

        The regular ``UPDATE_INTERVAL_MINUTES`` grid, unioned with every
        schedule-slot boundary. With the shipped values (5 / 15) the boundaries
        are already on the grid and the union is a no-op; it is taken anyway so
        that changing either constant to values that do not divide evenly still
        guarantees a cycle at the instant each slot activates.

        Returns:
            Sorted minute values in ``[0, 60)``.
        """
        return sorted(
            set(range(0, 60, UPDATE_INTERVAL_MINUTES))
            | set(range(0, 60, SCHEDULE_SLOT_MINUTES))
        )

    def _start_aligned_tick(self) -> None:
        """Drive cycles from a wall-clock-aligned tick instead of a free-running timer.

        Fires at ``second=0`` of each minute in :meth:`_aligned_tick_minutes`,
        UTC — the same grid the DAG's slots are laid out on — so the cycle that
        activates a new slot runs *at* the boundary rather than up to one
        interval later. The unsubscribe is stored for teardown in
        ``async_shutdown``.
        """
        self._unsub_aligned_tick = async_track_utc_time_change(
            self.hass,
            self._on_aligned_tick,
            minute=self._aligned_tick_minutes(),
            second=0,
        )

    async def _on_aligned_tick(self, _now: datetime) -> None:
        """Run one cycle on the aligned tick, skipping if the previous one is still running.

        Uses ``async_refresh`` rather than ``async_request_refresh``: the latter
        goes through the base class's debouncer, which would defer the cycle by
        its cooldown and give back the boundary lag this tick exists to remove.

        Args:
            _now: Tick time (unused — the cycle reads its own ``now``).
        """
        if self._cycle_in_flight:
            _LOGGER.warning(
                "sunSale cycle still running at the next aligned tick — skipping this one",
            )
            return
        self._cycle_in_flight = True
        try:
            await self.async_refresh()
        finally:
            self._cycle_in_flight = False

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

    def _subscribe_export_guard(self) -> None:
        """Feed the battery-export guard on every grid / PV state change.

        The guard's 120 s persistence only means something at the sensors'
        own update rate (~10 s on Solis); the 5-min cycle would miss or
        misjudge most episodes. Read-only: the guard never writes.
        """
        entity_ids = [
            e for e in (
                self._inverter_entity_ids.get("grid_power", ""),
                self._pv_power_entity_id,
            ) if e
        ]
        if not entity_ids:
            return
        self._unsub_export_guard = async_track_state_change_event(
            self.hass, entity_ids, self._on_export_guard_input,
        )

    @callback
    def _on_export_guard_input(self, event: Event) -> None:
        """Evaluate the export guard against the live grid / PV readings.

        Args:
            event: The HA state-change event (unused; live state is re-read).
        """
        self._evaluate_export_guard(datetime.now(UTC))

    def _evaluate_export_guard(self, now: datetime) -> None:
        """Run one export-guard sample and act on a trip / clear transition.

        Grid power comes from the driver (platform sign convention already
        applied) but only when its sensor is actually readable — the driver's
        getter reports an outage as 0.0, which would read as "no export".

        Args:
            now: Sample time.
        """
        module = self._control_module
        driver = getattr(self, "_inverter_driver", None)
        if module is None or driver is None:
            return
        grid_entity = self._inverter_entity_ids.get("grid_power", "")
        grid_kw = (
            driver.get_grid_power()
            if grid_entity and read_power_kw(self.hass, grid_entity) is not None
            else None
        )
        pv_kw = (
            read_power_kw(self.hass, self._pv_power_entity_id)
            if self._pv_power_entity_id else None
        )
        transition = self._export_guard.update(
            now, module.last_commanded_mode, grid_kw, pv_kw, self._last_soc,
        )
        if transition == TRANSITION_TRIPPED:
            self._on_export_guard_tripped()
        elif transition == TRANSITION_CLEARED:
            trip = self._export_guard.last_trip
            _LOGGER.info(
                "export guard: battery export ended (%.2f kWh, peak %.1f kW)",
                trip.export_kwh if trip else 0.0, trip.peak_kw if trip else 0.0,
            )
            self.async_update_listeners()

    def _on_export_guard_tripped(self) -> None:
        """Capture the inverter state, log it, and raise a persistent notification.

        Monitor-only (phase 1): nothing is written to the inverter. The capture
        is taken first so an undocumented state is recorded as it was.
        """
        snapshot: dict[str, Any] = {}
        capture = getattr(self._inverter_driver, "diagnostic_snapshot", None)
        if capture is not None:
            try:
                snapshot = dict(capture())
            except Exception:  # noqa: BLE001 — a diagnostic read must never raise
                _LOGGER.debug("export guard: diagnostic snapshot failed", exc_info=True)
        self._export_guard.attach_snapshot(snapshot)
        episode = self._export_guard.episode
        if episode is None:
            return
        _LOGGER.warning(
            "export guard TRIPPED: battery exporting to grid (peak %.1f kW) while "
            "sunSale commands %s — onset %s, SoC at onset %s. Inverter state: %s",
            episode.peak_kw, episode.commanded_mode, episode.onset.isoformat(),
            episode.soc_at_onset, snapshot,
        )
        persistent_notification.async_create(
            self.hass,
            (
                f"The battery has been exporting to the grid for over "
                f"{int(self._export_guard.as_dict()['persist_s'])} s (peak "
                f"{episode.peak_kw:.1f} kW) while sunSale commands "
                f"`{episode.commanded_mode}`, which never exports battery energy. "
                "sunSale is only monitoring this — check the inverter. "
                "Inverter state at the trip is on the observed-inverter-mode "
                "sensor (`export_guard` attribute)."
            ),
            title="sunSale: battery exporting to grid",
            notification_id=f"{DOMAIN}_export_guard_{self._entry.entry_id}",
        )
        self.async_update_listeners()

    @property
    def export_guard_status(self) -> dict[str, Any]:
        """Return the battery-export guard's state for sensors and the debug view."""
        return self._export_guard.as_dict()

    @property
    def soc_floor_status(self) -> dict[str, Any]:
        """Return the SoC-floor watch's state for sensors and the debug view."""
        return self._soc_floor_watch.as_dict()

    def _watch_soc_floor(self, primary: dict, secondary: dict, now: datetime) -> None:
        """Feed the SoC-floor watch from this cycle's battery data (monitor only).

        Also records the cycle's SoC, which the export guard stamps on an
        episode's onset between cycles.

        Args:
            primary: The cycle's primary inputs (supplies ``BatteryReading``).
            secondary: DAG outputs (supplies ``BatteryState``).
            now: Cycle timestamp.
        """
        reading: BatteryReading | None = primary.get(BatteryReading)
        state: BatteryState | None = secondary.get(BatteryState)
        soc = state.soc if state is not None else (reading.soc if reading else None)
        self._last_soc = soc
        module = self._control_module
        config = self._sun_sale_config
        if module is None or config is None:
            return
        commanded = module.last_commanded_mode
        min_soc = config.battery.min_soc
        transition = self._soc_floor_watch.update(
            now, commanded, soc,
            reading.power_kw if reading is not None else None,
            min_soc,
        )
        if transition == "breach" and soc is not None:
            _LOGGER.warning(
                "SoC-floor watch: battery still discharging at %.0f %% (min_soc "
                "%.0f %%) while sunSale commands %s — min_soc is a planning floor, "
                "the inverter drains to its own over-discharge SoC",
                soc * 100.0, min_soc * 100.0,
                commanded.value if commanded is not None else None,
            )
        elif transition == "recovered":
            _LOGGER.info("SoC-floor watch: discharge below min_soc has stopped")

    async def async_shutdown(self) -> None:
        """Tear down the coordinator and its control module on entry unload.

        Cancels the aligned cycle tick (the base class only knows about its own
        ``update_interval`` timer, which this coordinator does not use) and
        shuts down the control module, so neither a cycle nor its pending
        verify-tick can fire after unload and issue ghost Modbus writes against
        the still-valid solis_modbus entities.
        """
        if self._unsub_aligned_tick is not None:
            self._unsub_aligned_tick()
            self._unsub_aligned_tick = None
        if self._unsub_mode_registers is not None:
            self._unsub_mode_registers()
            self._unsub_mode_registers = None
        if self._unsub_export_guard is not None:
            self._unsub_export_guard()
            self._unsub_export_guard = None
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

        tariff_config = tariff_module.tariff_from_config(data)

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
        home_lat, home_lon = self._resolve_home_location()
        self._sun_sale_config = SunSaleConfig(
            tariff=tariff_config, battery=battery_config,
            local_tz=local_tz,
            # With both prices fixed no market sensor is read at all.
            price_source=(
                data.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE)
                if tariff_module.needs_market_price(tariff_config) else PRICE_SOURCE_FIXED
            ),
            currency=(data.get(CONF_CURRENCY) or DEFAULT_CURRENCY).strip().upper(),
            latitude=home_lat,
            longitude=home_lon,
            holiday_country=self._resolve_holiday_country(),
            forecast_reserve_enabled=bool(
                data.get(CONF_FORECAST_RESERVE_ENABLED, DEFAULT_FORECAST_RESERVE_ENABLED)
            ),
            price_levels=price_level_config_from_entry(data),
        )

        # The first holiday lookup imports `holidays`, which reads package
        # metadata off disk. Warm it here, off the loop, so the DAG never trips
        # HA's blocking-call detector mid-cycle.
        await self.hass.async_add_executor_job(
            preload_holidays, self._sun_sale_config.holiday_country,
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
            # Read at write time, so toggling the switch reaches the next
            # dispatch command without a reload.
            grid_charge_permitted=lambda: self.allow_grid_charging,
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
        # Kept so ``_read_schedule_knobs`` can resolve the live control-point
        # bounds each cycle and reduce the planner's caps to what the hardware
        # will actually accept (see ``_current_capability``).
        self._inverter_driver = driver

        # Resolve once and cache so the debug view / integration check can see
        # the actual base entities feeding SolarTranslator (device-based
        # selection resolves these from config-entry IDs at runtime).
        self._forecast_base_entities = self._resolve_forecast_base_entities(data)

        fixed_resolution = (
            timedelta(minutes=15)
            if data.get(CONF_NORDPOOL_RESOLUTION) == "15min"
            else timedelta(hours=1)
        )
        # An install with prices unchecked gets no price feed at all, so
        # PricingNode and everything downstream skip — the same path a
        # missing price sensor already takes. Gating the translator (rather
        # than blanking its entity) also covers the fixed-price feed, which
        # needs no sensor and would otherwise keep emitting slots.
        price_translators = (
            [
                build_price_translator(
                    self._sun_sale_config.price_source,
                    entity_id=data.get(CONF_NORDPOOL_ENTITY, ""),
                    export_entity_id=data.get(CONF_PRICE_EXPORT_ENTITY, ""),
                    resolution=fixed_resolution,
                    local_tz=local_tz,
                ),
            ]
            if resolve_install_capabilities(data)[CAP_PRICES]
            else []
        )
        self._translators = [
            *price_translators,
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
        ]

        nodes = [
            PricingNode(),
            PriceLevelNode(),
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
            ArrayCalibrationNode(),
            ForecastAccuracyNode(),
            ProfitabilityNode(),
            PriceForecastNode(),
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
        self._subscribe_export_guard()

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
        backfill_now = datetime.now(UTC)
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

        # Singleton (non-history) stores — one PersistentStore per spec, built
        # in one loop from the declarative registry. The dict stays keyed by the
        # base ``spec.storage_key``; only the HA store key is per-entry. See
        # orchestration/store_codecs.py.
        for singleton_spec in SINGLETON_STORE_SPECS:
            store = PersistentStore(
                self.hass, STORAGE_VERSION, ekey(singleton_spec.storage_key),
                serialize=singleton_spec.serialize,
                deserialize=singleton_spec.deserialize,
            )
            await store.load()
            self._stores[singleton_spec.storage_key] = store

        # Named aliases into the registry, kept so the existing read sites in
        # ``_async_update_data`` (and tests) keep resolving unchanged.
        self._consumption_daily_store = self._stores[STORAGE_KEY_CONSUMPTION_DAILY]
        self._price_history_store = self._stores[STORAGE_KEY_PRICE_HISTORY]
        self._price_curve_store = self._stores[STORAGE_KEY_PRICE_CURVE_HISTORY]
        self._forecast_quality_store = self._stores[STORAGE_KEY_FORECAST_QUALITY]
        self._array_calibration_store = self._stores[STORAGE_KEY_ARRAY_CALIBRATION]
        self._monthly_bill_store = self._stores[STORAGE_KEY_MONTHLY_BILL]
        self._yesterday_store = self._stores[STORAGE_KEY_YESTERDAY]
        self._mode_history_store = self._stores[STORAGE_KEY_MODE_HISTORY]
        self._counter_snapshot_store = self._stores[STORAGE_KEY_COUNTER_SNAPSHOT]
        self._baked_observed_store = self._stores[STORAGE_KEY_BAKED_OBSERVED]

        await self._async_backfill_price_history()

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
                now=datetime.now(UTC),
            )
            if after is not existing:
                await self._consumption_daily_store.save(after)

        # Ordered pre-DAG primary-assembly steps. Order is load-bearing — later
        # steps read keys earlier ones seed (resample after the history seeds,
        # snapshot after inverter-time deposits the skew). See cycle_steps.py.
        self._cycle_steps = (
            YesterdayRotationStep(self._yesterday_store, self._sun_sale_config),
            SampleHistoryStep(self._history_stores, self._guarded),
            DerivedSampleStep(self._history_stores),
            RecorderResampleStep(self.hass, self._resampler, self._sun_sale_config),
            PreRolloverSnapshotStep(self._counter_snapshot_store, self._sun_sale_config),
            WeatherStep(
                self.hass,
                WeatherTranslator(self._config.get(CONF_WEATHER_ENTITY, "")),
                self._sun_sale_config,
            ),
            StoredPrimariesStep(
                baked_store=self._baked_observed_store,
                monthly_bill_store=self._monthly_bill_store,
                price_history_store=self._price_history_store,
                price_curve_store=self._price_curve_store,
                forecast_quality_store=self._forecast_quality_store,
                array_calibration_store=self._array_calibration_store,
                mode_history_store=self._mode_history_store,
                read_sun_times=self._read_sun_times,
            ),
            ConsumptionDailyStep(
                self._consumption_daily_store, self._history_stores, self._sun_sale_config,
            ),
            CapacityStep(
                self._capacity_estimator,
                self._capacity_store,
                self._build_capacity_observation,
            ),
            SchedulePolicyStep(self._read_schedule_knobs),
        )

        # Last: everything a cycle touches now exists, so the first aligned tick
        # can safely land at any moment.
        self._start_aligned_tick()

    def _read_schedule_knobs(self) -> ScheduleKnobs:
        """Snapshot the user-set schedule knobs for one cycle's policy build.

        The export / discharge-to-grid caps are reduced by the driver's live
        :meth:`capability` before they reach the DP, so the planner never budgets
        a transfer rate the dispatcher would have to clamp on the way out. The
        reduction is one-directional (capability may only lower a configured cap)
        and resolved per cycle, because the underlying entity bounds move —
        solis_modbus resolves them from BMS mirror registers at runtime.
        """
        capability = self._current_capability()
        return ScheduleKnobs(
            use_standby=self.use_standby,
            allow_grid_charging=self.allow_grid_charging,
            allow_feed_in=self.allow_feed_in,
            allow_discharge_to_grid=self.allow_discharge_to_grid,
            mode_change_penalty_eur_per_kwh=self.mode_change_penalty_eur_per_kwh,
            profitability_tilt_alpha=self.profitability_tilt_alpha,
            terminal_value_discount=self.terminal_value_discount,
            max_discharge_to_grid_kw=InverterCapability.reduce(
                self.max_discharge_to_grid_kw,
                capability.max_grid_discharge_kw if capability else None,
            ),
            export_limit_kw=InverterCapability.reduce(
                self._export_limit_w / 1000.0,
                capability.max_export_kw if capability else None,
            ),
            # Battery legs are reduced against the configured limits in
            # ``ScheduleNode._capped_battery``; pass the raw hardware ceilings.
            max_battery_charge_kw=capability.max_battery_charge_kw if capability else None,
            max_battery_discharge_kw=(
                capability.max_battery_discharge_kw if capability else None
            ),
        )

    def _current_capability(self) -> InverterCapability | None:
        """Return the driver's live capability, or ``None`` when unavailable.

        Best-effort: a driver that raises while resolving bounds must not take
        the cycle down with it — the planner then falls back to the configured
        caps, which is the pre-seam behaviour.
        """
        driver = getattr(self, "_inverter_driver", None)
        if driver is None:
            return None
        try:
            return driver.capability(datetime.now(UTC))
        except Exception:  # noqa: BLE001 - diagnostic read must never break a cycle
            _LOGGER.debug("capability() raised — planning on configured caps", exc_info=True)
            return None

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
        # Populated in async_setup() before any update cycle can run.
        assert self._sun_sale_config is not None
        assert self._capacity_estimator is not None
        assert self._engine is not None
        now = datetime.now(UTC)

        try:
            primary = await run_translators(
                self._translators, self.hass, self._sun_sale_config, self._config, now
            )

            # A price sensor whose integration dropped it never recovers by
            # itself, and everything downstream plans on nothing until it does.
            # Guarded: a failed reload must not take the cycle — or the inverter
            # dispatch — down with it.
            with self._guarded("price feed watchdog"):
                await self._price_feed_watchdog.async_tick(
                    self._config, self._sun_sale_config.price_source, now,
                )

            # Assemble the DAG's primary inputs through the ordered pre-DAG
            # steps: each seeds its fallback primary value (infallible), then
            # its best-effort persist runs inside ``_guarded`` so a disk write
            # failure logs and the cycle — including the inverter dispatch —
            # still proceeds with every primary key present. See cycle_steps.py.
            for step in self._cycle_steps:
                step.seed(primary, now)
                with self._guarded(step.label):
                    await step.persist(primary, now)

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
        self._watch_soc_floor(primary, secondary, now)
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
                assert self._sun_sale_config is not None
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

        with self._guarded("array-calibration save"):
            calibration: ArrayCalibration | None = secondary.get(ArrayCalibration)
            if calibration is not None and self._array_calibration_store is not None:
                # Only rewrite when the fit actually moved on; the node returns
                # the cached object unchanged on same-day cycles.
                if calibration is not self._array_calibration_store.value:
                    await self._array_calibration_store.save(calibration)

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
                assert self._sun_sale_config is not None
                local_tz = self._sun_sale_config.local_tz
                today_local = now.astimezone(local_tz).date()
                today_peak_val = profitability_module.today_peak_from_price_series(
                    pricing_result, today_local, local_tz
                )
                if today_peak_val is not None:
                    new_peak = DailyPeak(
                        day=today_local,
                        peak_eur_kwh=today_peak_val,
                        day_class=profitability_module.classify_day(
                            today_local, holiday_predicate(self._sun_sale_config.holiday_country),
                        ),
                    )
                    peaks = [p for p in (self._price_history_store.value or []) if p.day != today_local]
                    peaks.append(new_peak)
                    peaks.sort(key=lambda p: p.day)
                    cutoff_day = today_local - timedelta(days=PRICE_HISTORY_RETENTION_DAYS)
                    peaks = [p for p in peaks if p.day >= cutoff_day]
                    await self._price_history_store.save(peaks)

        with self._guarded("price-curve-history save"):
            await self._save_price_curve_history(primary, secondary, now)

    async def _save_price_curve_history(
        self, primary: dict, secondary: dict, now: datetime,
    ) -> None:
        """Freeze this cycle's feature vintage and record any settled days.

        Two independent updates, both idempotent and both keyed by LOCAL date
        so they line up with the price forecast's day boundaries: the features
        for the day at the model's lead time are captured once and never
        revised, and any fully-settled day missing from the history is added.

        Args:
            primary: The cycle's primary inputs.
            secondary: The DAG's outputs.
            now: Cycle timestamp.
        """
        if self._price_curve_store is None or self._sun_sale_config is None:
            return
        price_series: PriceSeries | None = secondary.get(PriceSeries)
        if price_series is None:
            return

        local_tz = self._sun_sale_config.local_tz
        today_local = now.astimezone(local_tz).date()
        history: PriceCurveHistory = self._price_curve_store.value or PriceCurveHistory()
        generation: GenerationSeries | None = secondary.get(GenerationSeries)
        weather: WeatherForecastData | None = primary.get(WeatherForecastData)
        threshold = price_forecast_module.export_break_even(self._sun_sale_config.tariff)

        updated = price_forecast_module.capture_feature_vintage(
            history, weather, generation, today_local,
        )
        if updated is not None:
            history = updated

        frozen = price_forecast_module.capture_prediction(
            history, secondary.get(PriceForecast), today_local, local_tz,
        )
        if frozen is not None:
            history = frozen

        # Runs before settle_days, which consumes the predictions this reads.
        # Any day the market has now priced and the engine had predicted banks
        # its hourly error here — the afternoon the auction publishes, not at
        # the next midnight when the day settles.
        banked = price_fill_module.bank_fill_errors(
            history, price_series, today_local, local_tz, PRICE_ERROR_RETENTION_DAYS,
        )
        if banked is not None:
            history = banked

        config = self._sun_sale_config
        is_holiday = holiday_predicate(config.holiday_country)
        settled = price_forecast_module.settle_days(
            history,
            price_series,
            generation,
            weather,
            today_local,
            local_tz,
            threshold,
            PRICE_CURVE_RETENTION_DAYS,
            latitude=config.latitude,
            longitude=config.longitude,
            is_holiday=is_holiday,
            neighbour_share=online_shape.neighbour_share_fn(
                config.holiday_country, holiday_predicate,
            ),
        )
        if settled is not None:
            history = settled

        if (updated is not None or frozen is not None or banked is not None
                or settled is not None):
            await self._price_curve_store.save(history)

    async def _async_backfill_price_history(self) -> None:
        """Fetch settled price history once, so the week ahead is ready at boot.

        Runs only when the store is empty — a fresh install, or the first start
        after this engine shipped. Setup waits for it so the very first cycle
        publishes a full week-ahead view instead of the flat one an untrained
        engine would produce, and everything about it is best-effort: an
        uncovered market, a failed request or a slow network leaves the engine
        to learn from the first day it settles, exactly as it would have.
        """
        store = self._price_curve_store
        config = self._sun_sale_config
        if store is None or config is None:
            return
        existing = store.value
        if existing is not None and existing.records:
            return
        if config.latitude is None or config.longitude is None:
            return
        country = config.holiday_country or getattr(self.hass.config, "country", None)
        try:
            session = async_get_clientsession(self.hass)
            history = await asyncio.wait_for(
                price_backfill.backfill(
                    session,
                    country=country,
                    latitude=config.latitude,
                    longitude=config.longitude,
                    local_tz=config.local_tz,
                    today=dt_util.now().astimezone(config.local_tz).date(),
                    negative_threshold_eur_kwh=price_forecast_module.export_break_even(
                        config.tariff
                    ),
                    is_holiday=holiday_predicate(config.holiday_country),
                    neighbour_share=online_shape.neighbour_share_fn(
                        config.holiday_country, holiday_predicate,
                    ),
                ),
                timeout=price_backfill.TIMEOUT_S,
            )
        except TimeoutError:
            _LOGGER.debug("Price backfill timed out; the engine will learn as days settle")
            return
        except Exception:  # noqa: BLE001 — never let an optional fetch block setup
            _LOGGER.debug("Price backfill failed", exc_info=True)
            return
        if history is None or not history.records:
            return
        await store.save(history)

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

        Each chosen forecast device (``CONF_SOLAR_FORECAST_DEVICE_IDS``) is
        resolved to its base "today" sensor, then combined with the manually
        picked sensors by :func:`combine_forecast_entities` — which keeps the
        legacy two-key behaviour for entries saved before the sensor list.

        Args:
            data: The merged config-entry data dict.

        Returns:
            List of base forecast entity IDs (possibly empty).
        """
        device_ids = data.get(CONF_SOLAR_FORECAST_DEVICE_IDS) or []
        resolved = resolve_forecast_entities(self.hass, list(device_ids)) if device_ids else []
        return combine_forecast_entities(resolved, data)

    def _resolve_local_tz(self):
        """Return the HA-configured local timezone, falling back to UTC.

        Reads hass.config.time_zone (e.g. "Europe/Riga"). Falls back to UTC
        when unset, unknown, or unparseable; baseload bucketing depends on this.

        Returns:
            tzinfo for the HA installation's local timezone.
        """
        tz_name = getattr(self.hass.config, "time_zone", None)
        if not tz_name:
            return UTC
        try:
            return ZoneInfo(tz_name)
        except Exception:    # ZoneInfoNotFoundError + anything weird from HA mocks
            return UTC

    def _resolve_holiday_country(self) -> str | None:
        """Return HA's configured country code for the holiday calendar, or None when unset."""
        country = getattr(self.hass.config, "country", None)
        return country if isinstance(country, str) and country else None

    def _resolve_home_location(self) -> tuple[float | None, float | None]:
        """Return the HA home latitude/longitude, or (None, None) when unset.

        Drives the solar-position geometry behind the clear-sky index. HA
        reports (0.0, 0.0) when the home location was never configured, which
        is a real coordinate in the Gulf of Guinea — so that exact pair is
        treated as "unknown". A single zero component is kept: latitude 0 with a
        non-zero longitude is a genuine equatorial site.

        Returns:
            Tuple of (latitude, longitude) in degrees, or (None, None) when the
            location is unset or unreadable.
        """
        raw_lat = getattr(self.hass.config, "latitude", None)
        raw_lon = getattr(self.hass.config, "longitude", None)
        if raw_lat is None or raw_lon is None:
            return None, None
        try:
            lat = float(raw_lat)
            lon = float(raw_lon)
        except (TypeError, ValueError):
            return None, None
        if lat == 0.0 and lon == 0.0:
            return None, None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None, None
        return lat, lon

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
        assert self._capacity_estimator is not None
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
            "price_level": secondary.get(PriceLevelSeries),
            "price_forecast": secondary.get(PriceForecast),
            "consumption_today_kwh": (
                consumption.today_total_kwh if consumption else None
            ),
            "forecast_quality": _acc.quality if _acc else None,
            "array_calibration": secondary.get(ArrayCalibration),
            "solar_health": secondary.get(SolarHealth),
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
                _gen.today_total_kwh
                if (_gen := primary.get(GenerationReading)) is not None else None
            ),
            "today_imported_live_kwh": (
                _imp.today_total_kwh
                if (_imp := primary.get(GridImportTodayReading)) is not None else None
            ),
            "today_exported_live_kwh": (
                _exp.today_total_kwh
                if (_exp := primary.get(GridExportTodayReading)) is not None else None
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
        assert self._sun_sale_config is not None
        local_tz = self._sun_sale_config.local_tz
        local_today = now.astimezone(local_tz).date()

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
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                return None

        def _today_event(next_event: datetime | None) -> datetime | None:
            """Map a sun.sun "next_*" event to today's instance of that event."""
            if next_event is None:
                return None
            local_date = next_event.astimezone(local_tz).date()
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
