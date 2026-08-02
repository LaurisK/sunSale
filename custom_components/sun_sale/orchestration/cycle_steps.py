"""Ordered pre-DAG primary-assembly steps for the coordinator cycle.

``_async_update_data`` used to assemble the cycle's ``primary`` dict through
~10 inline "contribute to ``primary``, maybe persist" blocks. Each block is
individually simple; the aggregate was unreviewable and only testable through
the whole coordinator. This module extracts each block into a small
:class:`CycleStep` so the coordinator loop becomes a flat, ordered list and each
step is unit-testable in isolation.

**The load-bearing invariant** (documented in the original code): the DAG must
receive every primary key *even when a disk write fails*. Each step therefore
splits into two phases:

- :meth:`CycleStep.seed` — infallible, I/O-free; deposits the fallback primary
  value. Runs unguarded.
- :meth:`CycleStep.persist` — best-effort I/O (store appends/saves) plus any
  post-write re-injection. Runs inside the coordinator's ``_guarded()`` so a
  failure logs and the cycle continues.

Steps never hold the coordinator — every dependency (stores, the resampler,
config, and the few coordinator bound methods that carry per-cycle anchor state)
is constructor-injected. Cross-step values travel through the typed
:class:`CycleScratch`, never through ``primary`` under fake keys.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..contract.const import (
    COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
    SCHEDULE_MODE_CHANGE_PENALTY_MAX,
    SCHEDULE_MODE_CHANGE_PENALTY_MIN,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
    SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
    SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
    STORAGE_KEY_DERIVED_POWER,
)
from ..contract.models import (
    AcPortPowerReading,
    BackupPowerReading,
    BakedObservedHistory,
    BatteryReading,
    CapacityObservation,
    ConsumptionDailyBuckets,
    CounterSnapshotHistory,
    DerivedPowerHistory,
    EstimatedCapacity,
    ForecastQualityStore,
    GenerationReading,
    GridExportPowerHistory,
    GridExportTodayReading,
    GridImportPowerHistory,
    GridImportTodayReading,
    InverterModeHistory,
    InverterTimeReading,
    MonthlyBillState,
    NordpoolData,
    PriceHistory,
    PvPowerHistory,
    PvPowerReading,
    SchedulePolicy,
    SolarData,
    SunSaleConfig,
    SunTimes,
    YesterdayPrices,
)
from ..inbound.consumption_daily import try_finalise_yesterday_consumption
from ..inbound.inverter_time import (
    InverterTimeHistory,
    current_skew_seconds,
)
from ..inbound.inverter_time import (
    empty_history as empty_inverter_time_history,
)
from ..inbound.inverter_time import (
    update_history as update_inverter_time_history,
)
from ..inbound.observer.derived import build_derived_power_sample
from ..inbound.observer.generation import GENERATION_SIDE_ID
from ..inbound.observer.grid import GRID_EXPORT_SIDE_ID, GRID_IMPORT_SIDE_ID
from ..inbound.observer.recorder_resample import RecorderResampler, merge_by_timestamp
from ..inbound.pre_rollover_snapshot import maybe_capture_snapshots
from .history_stores import (
    DERIVED_POWER_SPEC,
    SAMPLE_HISTORY_SPECS,
    append_and_inject,
)
from .persistent_store import PersistentStore
from .store_codecs import YesterdayBuckets, rotate_yesterday_buckets

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..pipeline.battery import CapacityEstimator

# Guard factory: ``coordinator._guarded`` — a context manager that swallows and
# logs a best-effort step's exception. Injected so steps can guard sub-items
# (e.g. per-history-spec appends) without holding the coordinator.
GuardFactory = Callable[[str], AbstractContextManager[None]]


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


@dataclass
class CycleScratch:
    """Typed scratchpad for values handed between steps within one cycle.

    A fresh instance is created each cycle. Currently carries only the inverter
    clock skew deposited by :class:`InverterTimeStep` and read by
    :class:`PreRolloverSnapshotStep`.
    """

    clock_skew_seconds: float | None = None


@dataclass(frozen=True)
class ScheduleKnobs:
    """Snapshot of the coordinator's user-set schedule knobs for one cycle.

    Read from the switch/number entities via the coordinator's knob-reader so
    :class:`SchedulePolicyStep` can build (and clamp) the policy without holding
    the coordinator.
    """

    use_standby: bool
    allow_grid_charging: bool
    allow_feed_in: bool
    allow_discharge_to_grid: bool
    mode_change_penalty_eur_per_kwh: float
    profitability_tilt_alpha: float
    terminal_value_discount: float
    max_discharge_to_grid_kw: float | None
    export_limit_kw: float | None = None


class CycleStep:
    """One pre-DAG contribution to the cycle's ``primary`` dict.

    Subclasses override :meth:`seed` and/or :meth:`persist`; both default to a
    no-op so a step declares only the phase it needs. ``label`` names the step
    in the coordinator's ``_guarded()`` log for the persist phase.
    """

    label: str = "cycle step"

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Deposit the fallback primary value(s). Infallible and I/O-free."""

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Best-effort I/O + post-write re-injection. Runs inside ``_guarded()``."""


class YesterdayRotationStep(CycleStep):
    """Inject yesterday prices/solar and rotate the two-bucket store at rollover.

    Day boundaries are LOCAL so the chart's yesterday/today/tomorrow axis aligns
    with the persisted store: computing from ``now.date()`` (UTC) would leave a
    UTC-offset-sized window after local midnight where the store holds "today's
    UTC date" but the lookup asks for "yesterday LOCAL", silently dropping
    yesterday from the chart.

    ``seed`` mutates ``SolarData.entries`` in place to prepend yesterday's
    stored solar (so the forecast series spans yesterday→tomorrow). ``persist``
    filters today's slices back out and rotates the store — a best-effort write
    that must not abort the cycle now the primaries are already assembled.
    """

    label = "yesterday-bucket rotation"

    def __init__(self, store: PersistentStore, config: SunSaleConfig) -> None:
        """Bind the two-bucket store and config (for the local timezone)."""
        self._store = store
        self._config = config

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Inject ``YesterdayPrices`` and prepend yesterday's stored solar."""
        local_now = now.astimezone(self._config.local_tz)
        yesterday_str = (local_now.date() - timedelta(days=1)).isoformat()
        buckets = (self._store.value if self._store else None) or YesterdayBuckets()

        # Pricing: pass yesterday in via a primary input; inbound.pricing owns
        # the 72h yesterday→today→tomorrow assembly. Stored data older than
        # yesterday is treated as empty.
        yesterday_pricing_entries = (
            tuple(buckets.yesterday_nordpool)
            if buckets.yesterday_date == yesterday_str
            else ()
        )
        primary[YesterdayPrices] = YesterdayPrices(entries=yesterday_pricing_entries)

        solar_data: SolarData | None = primary.get(SolarData)
        if buckets.yesterday_date == yesterday_str and solar_data is not None:
            solar_data.entries = buckets.yesterday_solar + solar_data.entries

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Persist today's slice and rotate yesterday at LOCAL date rollover."""
        nordpool_data: NordpoolData | None = primary.get(NordpoolData)
        solar_data: SolarData | None = primary.get(SolarData)
        if nordpool_data is None or solar_data is None or self._store is None:
            return
        local = self._config.local_tz
        today_str = now.astimezone(local).date().isoformat()
        buckets = self._store.value or YesterdayBuckets()
        today_nordpool = [
            e for e in nordpool_data.entries
            if e.start.astimezone(local).date().isoformat() == today_str
        ]
        today_solar = [
            e for e in solar_data.entries
            if e.start.astimezone(local).date().isoformat() == today_str
        ]
        new_buckets = rotate_yesterday_buckets(buckets, today_str, today_nordpool, today_solar)
        await self._store.save(new_buckets)


class SampleHistoryStep(CycleStep):
    """Append each cycle's reading to its rolling sample-history store.

    Seeds every ``*History`` primary with the un-appended window first so that
    if an append (a disk write) fails, the DAG still receives the key and the
    cycle — including the inverter dispatch — proceeds. Each spec's append is
    guarded independently so one store's failure cannot skip the others.
    """

    label = "sample histories"

    def __init__(self, history_stores: dict[str, PersistentStore], guard: GuardFactory) -> None:
        """Bind the rolling-history store registry and the per-item guard."""
        self._history_stores = history_stores
        self._guard = guard

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Seed each spec's history primary with its current (un-appended) window."""
        for spec in SAMPLE_HISTORY_SPECS:
            store = self._history_stores.get(spec.storage_key)
            primary[spec.history_type] = spec.build_history(store)

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Append this cycle's reading per spec, re-injecting the updated window."""
        for spec in SAMPLE_HISTORY_SPECS:
            store = self._history_stores.get(spec.storage_key)
            with self._guard(f"history append {spec.storage_key}"):
                await append_and_inject(
                    spec, store, primary, primary.get(spec.reading_type), now,
                )


class DerivedSampleStep(CycleStep):
    """Compose and append the cross-stream derived-power sample.

    Combines AC-port + backup + battery (carrying battery_power + grid_net) + PV
    into one synchronised tuple, persisted as a rolling history so the
    consumption + losses observers can power-average per slot. The composer
    returns ``None`` when any source is unavailable this cycle — a partial sample
    would bias the per-slot mean asymmetrically. Must run after the translators
    have populated the four reading primaries.
    """

    label = "derived-power append"

    def __init__(self, history_stores: dict[str, PersistentStore]) -> None:
        """Bind the rolling-history store registry (for the derived store)."""
        self._history_stores = history_stores

    def _store(self) -> PersistentStore | None:
        """Return the derived-power history store, or None before setup."""
        return self._history_stores.get(STORAGE_KEY_DERIVED_POWER)

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Seed the derived-power history primary with its current window."""
        primary[DERIVED_POWER_SPEC.history_type] = DERIVED_POWER_SPEC.build_history(self._store())

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Compose this cycle's derived sample and append it (re-injecting)."""
        derived_sample = build_derived_power_sample(
            now=now,
            ac_port=primary.get(AcPortPowerReading),
            backup=primary.get(BackupPowerReading),
            battery=primary.get(BatteryReading),
            pv=primary.get(PvPowerReading),
        )
        await append_and_inject(
            DERIVED_POWER_SPEC, self._store(), primary, derived_sample, now,
        )


class RecorderResampleStep(CycleStep):
    """Lift the observer streams to the inverter's native update rate.

    The resampler fetches only the recorder rows since the last cycle, keeps a
    trimmed in-memory buffer per source sensor, and rebuilds the full-resolution
    samples — merged into each stream's primary before the DAG averages them per
    slot. Best-effort: on recorder failure the coarse per-tick primaries seeded
    by :class:`SampleHistoryStep` / :class:`DerivedSampleStep` are used as-is, so
    this step **must run after** those two have seeded the four history keys.
    The whole thing is ``persist`` (recorder I/O); ``seed`` is a no-op.
    """

    label = "recorder resample"

    _MERGE_TARGETS: tuple[tuple[str, type], ...] = (
        ("pv", PvPowerHistory),
        ("grid_import", GridImportPowerHistory),
        ("grid_export", GridExportPowerHistory),
        ("derived", DerivedPowerHistory),
    )

    def __init__(self, hass: HomeAssistant, resampler: RecorderResampler, config: SunSaleConfig) -> None:
        """Bind HA (for recorder reads), the resampler, and config."""
        self._hass = hass
        self._resampler = resampler
        self._config = config

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Resample the observer streams and merge into the history primaries."""
        local_midnight = now.astimezone(self._config.local_tz).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        resample_start = local_midnight.astimezone(UTC) - timedelta(days=1)
        resampled = await self._resampler.update(self._hass, resample_start, now)
        for key, history_type in self._MERGE_TARGETS:
            extra = resampled.get(key)
            if not extra:
                continue
            current = primary.get(history_type)
            existing = current.samples if current is not None else ()
            primary[history_type] = history_type(
                samples=merge_by_timestamp(existing, extra),
            )


class InverterTimeStep(CycleStep):
    """Track the HA↔inverter clock skew and deposit it into the scratchpad.

    The rolling skew estimate drives :class:`PreRolloverSnapshotStep`'s window
    shift so the capture aligns with INVERTER-local midnight when the two clocks
    have drifted. Returns ``None`` (no shift) until enough samples accumulate for
    confidence. Owns its own rolling history — no coordinator state involved.
    """

    label = "inverter-time tracking"

    def __init__(self) -> None:
        """Start with an empty rolling inverter-time history."""
        self._history: InverterTimeHistory = empty_inverter_time_history()

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Absorb this cycle's reading and deposit the median skew into scratch."""
        current: InverterTimeReading | None = primary.get(InverterTimeReading)
        self._history = update_inverter_time_history(self._history, current)
        scratch.clock_skew_seconds = current_skew_seconds(self._history)


class PreRolloverSnapshotStep(CycleStep):
    """Capture per-side today-total counters within the late-evening window.

    Gives the next-day bake-in an authoritative value when no dedicated
    yesterday-total sensor is mapped. ``seed`` injects the current (pre-capture)
    history so the DAG always receives the key — it consumes this via
    ``ctx.require``, so a missing key would raise ``MissingDependencyError``.
    ``persist`` captures (shifting the window by the scratch's clock skew), saves
    on change, and re-injects the post-capture history.
    """

    label = "pre-rollover snapshot capture"

    def __init__(self, store: PersistentStore, config: SunSaleConfig) -> None:
        """Bind the counter-snapshot store and config (for the local timezone)."""
        self._store = store
        self._config = config

    def _current(self) -> CounterSnapshotHistory:
        """Return the stored snapshot history, or an empty one before first save."""
        return (self._store.value if self._store else None) or CounterSnapshotHistory(records=())

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Seed the counter-snapshot primary with the pre-capture history."""
        primary[CounterSnapshotHistory] = self._current()

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Capture within the rollover window and re-inject the updated history."""
        if self._store is None:
            return
        current = self._current()
        updated = maybe_capture_snapshots(
            snapshot_history=current,
            sources=[
                (GENERATION_SIDE_ID, primary.get(GenerationReading)),
                (GRID_IMPORT_SIDE_ID, primary.get(GridImportTodayReading)),
                (GRID_EXPORT_SIDE_ID, primary.get(GridExportTodayReading)),
            ],
            now=now,
            local_tz=self._config.local_tz,
            retention_days=COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS,
            clock_skew_seconds=scratch.clock_skew_seconds,
        )
        # Inject the computed history before the (best-effort) save so the DAG
        # sees this cycle's capture even if the disk write then fails.
        primary[CounterSnapshotHistory] = updated
        if updated is not current:
            await self._store.save(updated)


class StoredPrimariesStep(CycleStep):
    """Inject the read-only stored primaries the DAG consumes each cycle.

    Pure seeds — every value is read straight from a store (or the sun.sun
    entity) with its empty fallback; there is no persist phase. ``MonthlyBillState``
    is injected raw (``None`` is meaningful — the bill node distinguishes "no
    ledger yet" from "empty ledger"). The prior-cycle ``InverterModeHistory`` tip
    is a few minutes stale (re-written downstream in ``_dispatch_inverter_mode``),
    which is harmless: only the in-progress slot is affected and it is still
    accumulating anyway.
    """

    label = "stored primaries"

    def __init__(
        self,
        *,
        baked_store: PersistentStore,
        monthly_bill_store: PersistentStore,
        price_history_store: PersistentStore,
        forecast_quality_store: PersistentStore,
        mode_history_store: PersistentStore,
        read_sun_times: Callable[[datetime], SunTimes],
    ) -> None:
        """Bind the read-only stores and the sun-times reader."""
        self._baked_store = baked_store
        self._monthly_bill_store = monthly_bill_store
        self._price_history_store = price_history_store
        self._forecast_quality_store = forecast_quality_store
        self._mode_history_store = mode_history_store
        self._read_sun_times = read_sun_times

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Inject baked/bill/price/quality/mode histories, sun times."""
        primary[BakedObservedHistory] = (
            self._baked_store.value if self._baked_store else None
        ) or BakedObservedHistory(records=())
        primary[MonthlyBillState] = (
            self._monthly_bill_store.value if self._monthly_bill_store else None
        )
        primary[PriceHistory] = PriceHistory(
            peaks=tuple((self._price_history_store.value or []) if self._price_history_store else []),
        )
        primary[ForecastQualityStore] = (
            self._forecast_quality_store.value if self._forecast_quality_store else None
        ) or ForecastQualityStore()
        primary[SunTimes] = self._read_sun_times(now)
        primary[InverterModeHistory] = (
            self._mode_history_store.value if self._mode_history_store else None
        ) or InverterModeHistory(samples=())


class ConsumptionDailyStep(CycleStep):
    """Finalise yesterday's consumption rollup once per local date.

    The finalise hook is idempotent — when yesterday's record already exists only
    trimming runs, so calling every cycle is fine. Runs after
    :class:`RecorderResampleStep` so the finalise sees the full-resolution
    derived history. ``seed`` injects the current rollup; ``persist`` finalises
    (saving on change) and re-injects the latest window for the baseload node.
    """

    label = "consumption-daily finalise"

    def __init__(
        self,
        store: PersistentStore,
        history_stores: dict[str, PersistentStore],
        config: SunSaleConfig,
    ) -> None:
        """Bind the consumption store, history registry, and config."""
        self._store = store
        self._history_stores = history_stores
        self._config = config

    def _current(self) -> ConsumptionDailyBuckets:
        """Return the stored rollup, or an empty one before first save."""
        return (self._store.value if self._store else None) or ConsumptionDailyBuckets(records=())

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Seed the consumption-daily primary with the current rollup."""
        primary[ConsumptionDailyBuckets] = self._current()

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Finalise yesterday's rollup (best-effort) and re-inject the window."""
        derived_store = self._history_stores.get(STORAGE_KEY_DERIVED_POWER)
        if self._store is None or derived_store is None or self._config is None:
            return
        existing = self._store.value or ConsumptionDailyBuckets(records=())
        derived_for_finalise = primary.get(DerivedPowerHistory)
        if derived_for_finalise is None:
            derived_for_finalise = DerivedPowerHistory(
                samples=tuple(derived_store.value or []),
            )
        updated = try_finalise_yesterday_consumption(
            derived_history=derived_for_finalise,
            existing=existing,
            local_tz=self._config.local_tz,
            now=now,
        )
        # Inject the (possibly just-finalised) rollup before the best-effort save
        # so the baseload node sees it even if the disk write then fails.
        primary[ConsumptionDailyBuckets] = updated
        if updated is not existing:
            await self._store.save(updated)


class CapacityStep(CycleStep):
    """Observe battery throughput and inject the current capacity estimate.

    ``seed`` injects the pre-observation estimate so the DAG always has the key.
    ``persist`` builds this cycle's observation (via the coordinator's anchor-aware
    ``build_observation`` — kept there because it manages cross-cycle anchor
    state and is unit-tested directly), feeds it to the shared estimator, saves,
    and re-injects the post-observation estimate. The estimator object is shared
    with the sensors, so it is injected, not owned.
    """

    label = "capacity observation"

    def __init__(
        self,
        estimator: CapacityEstimator,
        capacity_store: PersistentStore,
        build_observation: Callable[[BatteryReading, datetime], CapacityObservation | None],
    ) -> None:
        """Bind the shared estimator, its store, and the observation builder."""
        self._estimator = estimator
        self._capacity_store = capacity_store
        self._build_observation = build_observation

    def _inject(self, primary: dict) -> None:
        """Inject the estimator's current capacity as a primary."""
        primary[EstimatedCapacity] = EstimatedCapacity(
            value_kwh=self._estimator.estimated_capacity_kwh,
        )

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Seed the pre-observation capacity estimate."""
        self._inject(primary)

    async def persist(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Add this cycle's observation (best-effort save) and re-inject."""
        current_reading: BatteryReading | None = primary.get(BatteryReading)
        if current_reading is None:
            return
        obs = self._build_observation(current_reading, now)
        if obs is None:
            return
        self._estimator.add_observation(obs)
        # Re-inject the post-observation estimate before the best-effort save so
        # the DAG sees it even if the disk write then fails (the estimator is
        # already mutated in memory regardless).
        self._inject(primary)
        if self._capacity_store is not None:
            await self._capacity_store.save(self._estimator)


class SchedulePolicyStep(CycleStep):
    """Build the clamped ``SchedulePolicy`` from the user-set knobs.

    Pure seed — reads the switch/number knobs through the injected reader and
    clamps the numeric ones into their valid ranges. No I/O.
    """

    label = "schedule policy"

    def __init__(self, read_knobs: Callable[[], ScheduleKnobs]) -> None:
        """Bind the knob-reader callable."""
        self._read_knobs = read_knobs

    def seed(self, primary: dict, now: datetime, scratch: CycleScratch) -> None:
        """Inject the clamped schedule policy."""
        k = self._read_knobs()
        primary[SchedulePolicy] = SchedulePolicy(
            use_standby=k.use_standby,
            allow_grid_charging=k.allow_grid_charging,
            allow_feed_in=k.allow_feed_in,
            allow_discharge_to_grid=k.allow_discharge_to_grid,
            mode_change_penalty_eur_per_kwh=_clamp(
                k.mode_change_penalty_eur_per_kwh,
                SCHEDULE_MODE_CHANGE_PENALTY_MIN,
                SCHEDULE_MODE_CHANGE_PENALTY_MAX,
            ),
            profitability_tilt_alpha=_clamp(
                k.profitability_tilt_alpha,
                SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN,
                SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX,
            ),
            terminal_value_discount=_clamp(
                k.terminal_value_discount,
                SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN,
                SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX,
            ),
            max_discharge_to_grid_kw=(
                _clamp(
                    k.max_discharge_to_grid_kw,
                    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN,
                    SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX,
                )
                if k.max_discharge_to_grid_kw is not None
                else None
            ),
            # Deployment config from the config flow, not a user-set entity
            # knob — passed through unclamped.
            export_limit_kw=k.export_limit_kw,
        )
