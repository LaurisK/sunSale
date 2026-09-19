"""Unit tests for the pre-DAG cycle steps (orchestration/cycle_steps.py).

Each step is exercised through the same ``seed → guarded persist`` pattern the
coordinator loop uses, with fake stores/primaries — the scenarios previously
only reachable through the whole coordinator: a store-write failure still seeds
the primary, empty-store defaults, rotation at the date boundary, the resample
merge order.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.sun_sale.contract.const import (
    STORAGE_KEY_DERIVED_POWER,
)
from custom_components.sun_sale.contract.models import (
    AcPortPowerReading,
    ArrayCalibration,
    BackupPowerReading,
    BakedObservedHistory,
    BatteryReading,
    CapacityObservation,
    ConsumptionDailyBuckets,
    CounterSnapshotHistory,
    DerivedPowerHistory,
    DerivedPowerSample,
    EstimatedCapacity,
    ForecastQualityStore,
    GenerationHistory,
    GenerationReading,
    GridExportTodayReading,
    GridImportTodayReading,
    InverterModeHistory,
    MonthlyBillState,
    NordpoolData,
    PriceCurveHistory,
    PriceEntry,
    PriceHistory,
    PvPowerHistory,
    PvPowerReading,
    SchedulePolicy,
    SolarData,
    SolarEntry,
    SunTimes,
    YesterdayPrices,
)
from custom_components.sun_sale.orchestration.cycle_steps import (
    CapacityStep,
    ConsumptionDailyStep,
    DerivedSampleStep,
    PreRolloverSnapshotStep,
    RecorderResampleStep,
    SampleHistoryStep,
    ScheduleKnobs,
    SchedulePolicyStep,
    StoredPrimariesStep,
    YesterdayRotationStep,
)
from custom_components.sun_sale.orchestration.history_stores import (
    GENERATION_SPEC,
    SAMPLE_HISTORY_SPECS,
)
from custom_components.sun_sale.orchestration.store_codecs import YesterdayBuckets
from custom_components.sun_sale.pipeline.battery import CapacityEstimator

_TZ = ZoneInfo("Europe/Vilnius")   # UTC+3 in July — exercises the local/UTC gap
_CFG = SimpleNamespace(local_tz=_TZ)
# 2026-07-10 09:00 UTC → 12:00 local; local date 2026-07-10, yesterday 2026-07-09.
_NOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


@contextlib.contextmanager
def _swallow_guard(label):
    """Mimic ``coordinator._guarded`` — swallow the persist phase's exceptions."""
    try:
        yield
    except Exception:
        pass


class FakeStore:
    """Minimal stand-in for PersistentStore with optional save failure."""

    def __init__(self, value=None, fail_save=False):
        """Store an initial value; ``fail_save`` makes every save raise."""
        self.value = value
        self.fail_save = fail_save
        self.saves: list = []

    async def save(self, value):
        """Record and apply the save, or raise when ``fail_save`` is set."""
        if self.fail_save:
            raise RuntimeError("disk full")
        self.value = value
        self.saves.append(value)

    async def append_and_trim(self, new_item, cutoff, timestamp_fn):
        """Mirror PersistentStore.append_and_trim for history stores."""
        items = [x for x in list(self.value or []) if timestamp_fn(x) >= cutoff]
        items.append(new_item)
        await self.save(items)


async def _run(step, primary, now=_NOW):
    """Drive one step exactly as the coordinator loop does (seed → guarded persist)."""
    step.seed(primary, now)
    with _swallow_guard(step.label):
        await step.persist(primary, now)


# ---------------------------------------------------------------------------
# YesterdayRotationStep
# ---------------------------------------------------------------------------

def test_yesterday_seed_injects_prices_and_prepends_solar():
    """seed injects yesterday prices and prepends yesterday's stored solar."""
    pe = PriceEntry(start=_NOW, end=_NOW + timedelta(hours=1), price_eur_kwh=0.1)
    se_y = SolarEntry(start=_NOW - timedelta(days=1), end=_NOW, expected_kwh=0.5, source="y")
    se_t = SolarEntry(start=_NOW, end=_NOW + timedelta(hours=1), expected_kwh=0.7, source="t")
    store = FakeStore(YesterdayBuckets(
        yesterday_date="2026-07-09", yesterday_nordpool=[pe], yesterday_solar=[se_y],
    ))
    primary = {SolarData: SolarData(entries=[se_t], total_today_kwh=0.0,
                                    today_remaining_kwh=0.0, primary_source="none")}
    YesterdayRotationStep(store, _CFG).seed(primary, _NOW)
    assert primary[YesterdayPrices].entries == (pe,)
    assert primary[SolarData].entries == [se_y, se_t]


def test_yesterday_seed_stale_date_yields_empty_prices():
    """A stale yesterday date (not local-yesterday) yields empty yesterday prices."""
    pe = PriceEntry(start=_NOW, end=_NOW, price_eur_kwh=0.1)
    store = FakeStore(YesterdayBuckets(yesterday_date="2020-01-01", yesterday_nordpool=[pe]))
    primary = {}
    YesterdayRotationStep(store, _CFG).seed(primary, _NOW)
    assert primary[YesterdayPrices].entries == ()


async def test_yesterday_persist_rotates_at_local_date_boundary():
    """persist rolls the previous today bucket into yesterday on a new local day."""
    old_pe = PriceEntry(start=_NOW - timedelta(days=1), end=_NOW, price_eur_kwh=0.2)
    store = FakeStore(YesterdayBuckets(today_date="2026-07-09", today_nordpool=[old_pe]))
    today_pe = PriceEntry(start=_NOW, end=_NOW + timedelta(hours=1), price_eur_kwh=0.3)
    primary = {
        NordpoolData: NordpoolData(entries=[today_pe], resolution=timedelta(hours=1)),
        SolarData: SolarData(entries=[], total_today_kwh=0.0,
                             today_remaining_kwh=0.0, primary_source="none"),
    }
    await _run(YesterdayRotationStep(store, _CFG), primary)
    saved = store.saves[-1]
    assert saved.yesterday_date == "2026-07-09"
    assert saved.yesterday_nordpool == [old_pe]
    assert saved.today_date == "2026-07-10"
    assert saved.today_nordpool == [today_pe]


# ---------------------------------------------------------------------------
# SampleHistoryStep
# ---------------------------------------------------------------------------

async def test_sample_history_appends_and_injects():
    """persist appends this cycle's reading and injects the updated window."""
    reading = GenerationReading(today_total_kwh=5.0, timestamp=_NOW)
    stores = {GENERATION_SPEC.storage_key: FakeStore(value=[])}
    primary = {GENERATION_SPEC.reading_type: reading}
    await _run(SampleHistoryStep(stores, _swallow_guard), primary)
    hist = primary[GenerationHistory]
    assert hist.samples[-1] == reading


async def test_sample_history_append_failure_keeps_seeded_window():
    """A store-write failure is isolated; the seeded (un-appended) window stays."""
    existing = GenerationReading(today_total_kwh=1.0, timestamp=_NOW - timedelta(hours=1))
    reading = GenerationReading(today_total_kwh=5.0, timestamp=_NOW)
    stores = {GENERATION_SPEC.storage_key: FakeStore(value=[existing], fail_save=True)}
    primary = {GENERATION_SPEC.reading_type: reading}
    await _run(SampleHistoryStep(stores, _swallow_guard), primary)
    # DAG still receives the key, holding only the pre-append sample.
    assert primary[GenerationHistory].samples == (existing,)


def test_sample_history_seed_covers_every_spec():
    """seed injects a history primary for every sample-history spec."""
    stores = {spec.storage_key: FakeStore(value=[]) for spec in SAMPLE_HISTORY_SPECS}
    primary = {}
    SampleHistoryStep(stores, _swallow_guard).seed(primary, _NOW)
    for spec in SAMPLE_HISTORY_SPECS:
        assert spec.history_type in primary


# ---------------------------------------------------------------------------
# DerivedSampleStep
# ---------------------------------------------------------------------------

async def test_derived_sample_appends_when_all_inputs_present():
    """persist composes and appends a derived sample when every input is present."""
    store = FakeStore(value=[])
    primary = {
        AcPortPowerReading: AcPortPowerReading(power_kw=0.1, timestamp=_NOW),
        BackupPowerReading: BackupPowerReading(power_kw=0.0, timestamp=_NOW),
        BatteryReading: _battery_reading(),
        PvPowerReading: PvPowerReading(power_w=2000.0, timestamp=_NOW),
    }
    await _run(DerivedSampleStep({STORAGE_KEY_DERIVED_POWER: store}), primary)
    assert len(store.value) == 1
    assert isinstance(store.value[0], DerivedPowerSample)


async def test_derived_sample_skips_when_input_missing():
    """persist appends nothing when any of the five inputs is missing."""
    store = FakeStore(value=[])
    primary = {AcPortPowerReading: AcPortPowerReading(power_kw=0.1, timestamp=_NOW)}
    await _run(DerivedSampleStep({STORAGE_KEY_DERIVED_POWER: store}), primary)
    assert store.value == []
    assert primary[DerivedPowerHistory].samples == ()


# ---------------------------------------------------------------------------
# RecorderResampleStep
# ---------------------------------------------------------------------------

async def test_recorder_resample_merges_full_resolution_samples():
    """persist merges the resampler's extra samples into the seeded primary."""
    coarse = PvPowerReading(power_w=1000.0, timestamp=_NOW)
    extra = PvPowerReading(power_w=1500.0, timestamp=_NOW + timedelta(minutes=1))

    class _Resampler:
        async def update(self, hass, start, end):
            return {"pv": [extra]}

    primary = {PvPowerHistory: PvPowerHistory(samples=(coarse,))}
    step = RecorderResampleStep(object(), _Resampler(), _CFG)
    await _run(step, primary)
    assert set(primary[PvPowerHistory].samples) == {coarse, extra}


async def test_recorder_resample_noop_leaves_primary_untouched():
    """An empty resample result leaves the seeded primary unchanged."""
    coarse = PvPowerReading(power_w=1000.0, timestamp=_NOW)

    class _Resampler:
        async def update(self, hass, start, end):
            return {}

    primary = {PvPowerHistory: PvPowerHistory(samples=(coarse,))}
    await _run(RecorderResampleStep(object(), _Resampler(), _CFG), primary)
    assert primary[PvPowerHistory].samples == (coarse,)


# ---------------------------------------------------------------------------
# PreRolloverSnapshotStep
# ---------------------------------------------------------------------------

def test_pre_rollover_seed_injects_empty_history():
    """seed injects an empty snapshot history when the store holds nothing."""
    primary = {}
    PreRolloverSnapshotStep(FakeStore(value=None), _CFG).seed(primary, _NOW)
    assert primary[CounterSnapshotHistory] == CounterSnapshotHistory(records=())


async def test_pre_rollover_captures_inside_window():
    """persist captures per-side counters when now is within the rollover window."""
    # 2026-07-10 20:40 UTC → 23:40 local — inside the 23:30–23:59 window.
    now = datetime(2026, 7, 10, 20, 40, tzinfo=UTC)
    store = FakeStore(value=CounterSnapshotHistory(records=()))
    primary = {
        GenerationReading: GenerationReading(today_total_kwh=12.0, timestamp=now),
        GridImportTodayReading: GridImportTodayReading(today_total_kwh=3.0, timestamp=now),
        GridExportTodayReading: GridExportTodayReading(today_total_kwh=4.0, timestamp=now),
    }
    await _run(PreRolloverSnapshotStep(store, _CFG), primary, now=now)
    assert len(store.saves) == 1
    assert len(primary[CounterSnapshotHistory].records) == 3


# ---------------------------------------------------------------------------
# StoredPrimariesStep
# ---------------------------------------------------------------------------

def test_stored_primaries_seeds_all_keys_with_defaults():
    """seed injects every stored primary, defaulting empties and passing bill None."""
    primary = {}
    StoredPrimariesStep(
        baked_store=FakeStore(value=None),
        monthly_bill_store=FakeStore(value=None),
        price_history_store=FakeStore(value=None),
        price_curve_store=FakeStore(value=None),
        forecast_quality_store=FakeStore(value=None),
        array_calibration_store=FakeStore(value=None),
        mode_history_store=FakeStore(value=None),
        read_sun_times=lambda now: SunTimes(today_sunrise=None, today_sunset=None),
    ).seed(primary, _NOW)
    assert primary[BakedObservedHistory] == BakedObservedHistory(records=())
    assert primary[MonthlyBillState] is None      # None is meaningful, not defaulted
    assert primary[PriceHistory] == PriceHistory(peaks=())
    assert primary[PriceCurveHistory] == PriceCurveHistory()
    assert primary[ForecastQualityStore] == ForecastQualityStore()
    # None is meaningful: a zero-capacity calibration would make every
    # clear-sky index infinite, so "not fitted yet" must stay distinguishable.
    assert primary[ArrayCalibration] is None
    assert primary[InverterModeHistory] == InverterModeHistory(samples=())
    assert isinstance(primary[SunTimes], SunTimes)


# ---------------------------------------------------------------------------
# ConsumptionDailyStep
# ---------------------------------------------------------------------------

async def test_consumption_daily_seed_and_reinject_empty():
    """seed and persist both inject an empty rollup when nothing is stored."""
    primary = {}
    step = ConsumptionDailyStep(
        FakeStore(value=None), {STORAGE_KEY_DERIVED_POWER: FakeStore(value=[])}, _CFG,
    )
    await _run(step, primary)
    assert primary[ConsumptionDailyBuckets] == ConsumptionDailyBuckets(records=())


# ---------------------------------------------------------------------------
# CapacityStep
# ---------------------------------------------------------------------------

async def test_capacity_step_saves_observation_and_reinjects():
    """persist feeds a built observation to the estimator and saves it."""
    est = CapacityEstimator(nominal_capacity_kwh=28.0, round_trip_efficiency=1.0)
    store = FakeStore()
    obs = CapacityObservation(timestamp=_NOW, soc_start=0.2, soc_end=0.8,
                              energy_kwh=15.0, direction="charge")
    primary = {BatteryReading: _battery_reading()}
    await _run(CapacityStep(est, store, lambda reading, now: obs), primary)
    assert store.saves == [est]
    assert isinstance(primary[EstimatedCapacity], EstimatedCapacity)


async def test_capacity_seed_survives_persist_failure():
    """The invariant: seed injects the estimate even if the persist phase fails."""
    est = CapacityEstimator(nominal_capacity_kwh=28.0, round_trip_efficiency=1.0)
    obs = CapacityObservation(timestamp=_NOW, soc_start=0.2, soc_end=0.8,
                              energy_kwh=15.0, direction="charge")
    store = FakeStore(fail_save=True)
    primary = {BatteryReading: _battery_reading()}
    # _run wraps persist in the swallow-guard, mirroring the coordinator loop.
    await _run(CapacityStep(est, store, lambda reading, now: obs), primary)
    assert EstimatedCapacity in primary


# ---------------------------------------------------------------------------
# SchedulePolicyStep
# ---------------------------------------------------------------------------

def test_schedule_policy_clamps_out_of_range_knobs():
    """seed clamps the numeric knobs into their valid ranges."""
    knobs = ScheduleKnobs(
        use_standby=True, allow_grid_charging=False, allow_feed_in=True,
        allow_discharge_to_grid=False,
        mode_change_penalty_eur_per_kwh=999.0,
        profitability_tilt_alpha=-999.0,
        terminal_value_discount=999.0,
        max_discharge_to_grid_kw=None,
    )
    primary = {}
    SchedulePolicyStep(lambda: knobs).seed(primary, _NOW)
    pol = primary[SchedulePolicy]
    assert pol.use_standby is True
    assert pol.max_discharge_to_grid_kw is None
    # Knobs default export_limit_kw to None → policy stays uncapped.
    assert pol.export_limit_kw is None
    # Clamped values differ from the wild inputs (exact bounds live in const).
    assert pol.mode_change_penalty_eur_per_kwh != 999.0
    assert pol.profitability_tilt_alpha != -999.0


def test_schedule_policy_passes_export_limit_unclamped():
    """seed forwards export_limit_kw as-is — deployment config, not a knob."""
    knobs = ScheduleKnobs(
        use_standby=True, allow_grid_charging=True, allow_feed_in=True,
        allow_discharge_to_grid=True,
        mode_change_penalty_eur_per_kwh=0.005,
        profitability_tilt_alpha=0.5,
        terminal_value_discount=0.5,
        max_discharge_to_grid_kw=None,
        export_limit_kw=8.0,
    )
    primary = {}
    SchedulePolicyStep(lambda: knobs).seed(primary, _NOW)
    assert primary[SchedulePolicy].export_limit_kw == 8.0


def _battery_reading() -> BatteryReading:
    """Build a minimal BatteryReading for the derived/capacity steps."""
    return BatteryReading(
        soc=0.5,
        power_kw=0.0,
        grid_power_kw=0.0,
        household_load_kw=0.2,
    )
