# sunSale Architecture

End-to-end description of the tiered DAG that drives every sunSale update cycle.

| | |
|---|---|
| **Status** | Implemented |
| **Pattern** | Translation layer + inbound normalisers + tiered DAG + post-DAG inverter control module |
| **HA boundary** | HA imports confined to root entry points (`__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`, `select.py`, `number.py`), `orchestration/`, and `outbound/inverter.py`. Inbound translators (`inbound/**`) do not import HA — each accepts a duck-typed `hass` at runtime to read `hass.states`. All other modules are pure Python. |

## Contents

1. [Overview](#1-overview)
2. [Package layout](#2-package-layout)
3. [Layer 1 — Translation (inbound)](#3-layer-1--translation-inbound)
4. [Layer 2 — DAG engine and nodes (pipeline)](#4-layer-2--dag-engine-and-nodes-pipeline)
5. [Layer 3 — Inverter control module and output adapters (outbound)](#5-layer-3--inverter-control-module-and-output-adapters-outbound)
6. [Orchestration and update cycle](#6-orchestration-and-update-cycle)
7. [Node reference](#7-node-reference)
8. [Data contracts](#8-data-contracts)
9. [Key design decisions](#9-key-design-decisions)

---

## 1. Overview

```
HA state machine
      │  (read once per cycle, in parallel)
      ▼
┌─ Translation Layer — inbound/**
│   PriceFeedTranslator                     → PriceFeedData
│     (Nordpool / ENTSO-e / Octopus / Amber / TOU; NordpoolData = alias)
│   SolarTranslator                         → SolarData
│   BatteryTranslator (via BatterySource)   → BatteryReading
│   Generation / PvPower    (observer/)     → GenerationReading / PvPowerReading
│   GridImport/ExportPowerObserver          → GridPowerReading
│   GridImport/ExportTotalTranslator        → Grid*TodayReading
│   AcPort / Backup         (observer/)     → DerivedPowerSample parts
│   HouseholdConsumption                    → HouseholdConsumptionReading
│   InverterModeTranslator (via driver)     → InverterModeReading
│     reads flow through the telemetry seam: inbound/telemetry/** —
│     HaTelemetryReader + per-vendor TelemetryCodec (canonical unit/sign)
└──────────────────────────────────────────────────────────────────────
      │  typed primary data (dict[type, Any])
      │  + coordinator-injected primaries: YesterdayPrices, EstimatedCapacity,
      │    PriceHistory, *History, BakedObservedHistory, DerivedPowerHistory,
      │    ConsumptionDailyBuckets, SchedulePolicy
      ▼
┌─ DAG Engine — pipeline/dag_engine.py · nodes/tier{1..4}.py
│   T1  Pricing · BatteryState · BatteryStatus · BaseLoadProfile
│   T2  Generation · ObservedGeneration · ObservedGrid · ObservedConsumption ·
│       ObservedLosses · Degradation · BatteryRuntime · Profitability
│   T3  Lockout · ForecastAccuracy · MonthlyBill
│   T4  Schedule  (SoC-bucketed dynamic programming)
│
│   Pure-Python helpers used by nodes:
│     inbound/pricing.py            (PriceSeries assembly)
│     inbound/forecast.py           (GenerationSeries assembly)
│     inbound/observer/**           (observed-series engine: gen/grid/derived)
│     pipeline/tariff.py            (buy/sell formula)
│     pipeline/battery.py           (capacity + degradation)
│     pipeline/calculation.py       (lockout windows)
│     pipeline/slot_physics.py      (per-slot StorageMode physics)
│     pipeline/schedule.py          (SoC-bucketed DP → StorageMode/slot)
│     pipeline/profitability.py     (rolling daily-peak percentile)
│     pipeline/monthly_bill.py      (running bill = carry + live)
│     pipeline/base_load.py         (P10 baseload profile)
│     pipeline/forecast_accuracy.py (MAE/bias/MAPE + EMAs)
└──────────────────────────────────────────────────────────────────────
      │  Schedule (with StorageMode per slot)
      ▼
┌─ InverterControlModule — outbound/inverter_control_module.py
│   observe → plan → act → verify → reconcile
│   (act gated by the automation switch; a mode override bypasses the gate)
│   → InverterControlDriver  (outbound/driver.py)
│       selected by outbound/driver_factory.make_inverter_driver:
│         Solis      → SolisDriver (register writes via InverterController)
│         entity-driven vendors → EntityControlDriver (entity_control.py)
│         notes-only / unknown   → telemetry-only SolisDriver wrapper (no-op write)
└──────────────────────────────────────────────────────────────────────
```

The coordinator (`orchestration/coordinator.py`) owns the update schedule, feeds the three layers in order, manages all persistent stores via `orchestration/persistent_store.py` and the declarative `orchestration/history_stores.py` registry, and collects results into `coordinator.data` for sensor entities.

---

## 2. Package layout

```
custom_components/sun_sale/
├── __init__.py                  HA entry point (panel, debug view, services)
├── config_flow.py               Config + options flow (solis auto-detect)
├── sensor.py                    HA sensor entities
├── switch.py                    Automation + scheduler-policy switches
├── select.py                    Mode-override select (operator intent)
├── number.py                    Scheduler-policy number knobs
├── currency.py                  Display-currency labels/icons (optimisation stays currency-neutral)
├── ha_state.py                  Shared HA-edge state reads + unit normalisation (no sun_sale imports)
│
├── contract/                    Pure data types — no logic, no imports of other layers
│   ├── const.py                 Storage keys, conf keys, defaults, retention windows
│   └── models.py                All dataclasses (configs, primary types, secondary types)
│
├── inbound/                     HA-read translators + pure-Python normalisers
│   ├── pricing.py               Price-feed translators (Nordpool/ENTSO-e/Octopus/Amber/TOU) + 72h PriceSeries assembly
│   ├── forecast.py              SolarTranslator + GenerationSeries assembly
│   ├── forecast_resolver.py     Auto-resolve forecast-device entity IDs from config entries
│   ├── battery.py               BatteryTranslator (reads via BatterySource) + BatteryStatus
│   ├── battery_source.py        BatterySource seam (Inverter/JkBms/Chained) — SoC/power/energy
│   ├── household_consumption.py HouseholdConsumptionTranslator (today-total kWh)
│   ├── consumption_daily.py     Finalise per-day consumption buckets from derived history
│   ├── inverter_mode.py         InverterModeTranslator (decoded StorageMode, via driver.observe)
│   ├── pre_rollover_snapshot.py Capture daily counters just before local midnight
│   ├── yesterday_total_resolver.py Authoritative yesterday-total (entity or snapshot)
│   ├── inverter_discovery.py    Vendor-pluggable entity-discovery registry (discovery_for)
│   ├── platform_profiles.py     Declarative role→pattern profiles for entity-driven vendors
│   ├── solis_entity_resolver.py SolisEntityDiscovery (auto-resolve solis_modbus entity IDs)
│   ├── inverter_entity_resolver.py Vendor-neutral front door — delegates to discovery_for()
│   ├── telemetry/               Read-side seam mirroring outbound/driver.py
│   │   ├── binding.py           TelemetrySignal / SignalRole / SignalBinding (pure vocabulary)
│   │   ├── codec.py             TelemetryCodec — per-vendor sign/unit transforms (pure)
│   │   └── reader.py            HaTelemetryReader + resolve_bindings (live, freshness-aware)
│   └── observer/                Observed-series engine (shared by gen/grid/derived)
│       ├── engine.py            ObservedSeriesEngine + Side spec (raw averaging)
│       ├── generation.py        Pv/Generation translators + ObservedGenerationSeries
│       ├── grid.py              Directional power observers + ObservedGridSeries
│       ├── derived.py           AcPort/Backup translators + consumption/losses series
│       ├── bake_in.py           Once-per-day yesterday finalisation (proportional scaling)
│       └── recorder_resample.py Lift observer streams to native rate via the HA recorder
│
├── pipeline/                    Pure-Python DAG engine + node logic
│   ├── dag_engine.py            DagNode, DagEngine, NodeContext, run_translators
│   ├── nodes/                   DAG nodes split by execution tier
│   │   ├── tier1.py             Pricing / BatteryState / BatteryStatus / BaseLoadProfile
│   │   ├── tier2.py             Generation / ObservedGeneration / ObservedGrid /
│   │   │                        ObservedConsumption / ObservedLosses / Degradation /
│   │   │                        BatteryRuntime / Profitability
│   │   ├── tier3.py             Lockout / ForecastAccuracy / MonthlyBill
│   │   └── tier4.py             ScheduleNode
│   ├── tariff.py                buy_price / sell_price
│   ├── battery.py               CapacityEstimator, degradation_cost_per_kwh
│   ├── calculation.py           Lockout windows, slot decisions
│   ├── slot_physics.py          simulate_slot — one StorageMode over one slot
│   ├── schedule.py              SoC-bucketed DP → StorageMode per slot
│   ├── profitability.py         Rolling 30d daily-peak percentile (day-class normalised)
│   ├── monthly_bill.py          Running monthly bill = persistent carry + live slot costs
│   ├── base_load.py             24h P10 baseload profile + battery runtime estimate
│   └── forecast_accuracy.py     Forecast vs observed deltas + EMA quality buckets
│
├── outbound/                    HA-write adapters + post-DAG actuation
│   ├── driver.py                InverterDriver / InverterControlDriver protocols + ControlRow
│   ├── driver_factory.py        make_inverter_driver — the platform dispatch seam
│   ├── inverter.py              InverterController (Solis register read/write), normalize_power_to_kw
│   ├── solis_driver.py          SolisDriver — register-level InverterControlDriver
│   ├── storage_mode_specs.py    StorageModeSpec / build_specs / decode_mode (Solis register detail)
│   ├── entity_control.py        EntityControlDriver — entity/service-driven control for non-Solis
│   ├── {huawei,solax,sungrow,goodwe,deye}_driver.py  entity-driven vendor drivers (untested vs HW)
│   ├── {fronius,sma,kostal}_driver.py  notes-only (not implemented; telemetry-only fallback)
│   └── inverter_control_module.py  observe → plan → act → verify → reconcile per cycle (driver-neutral)
│
└── orchestration/               Glue: schedule, persistence, sensor dict mapping
    ├── coordinator.py           SunSaleCoordinator (DataUpdateCoordinator subclass)
    ├── persistent_store.py      PersistentStore[T] — typed wrapper around HA Store
    ├── history_stores.py        HistoryStoreSpec — declarative rolling-history registry
    └── debug_view.py            HTTP view exposing cycle inputs/outputs as JSON
```

Layering rule: `contract` imports nothing from the integration. `inbound`/`pipeline`/`outbound` import only `contract` (plus the deliberate inbound→outbound back-edges in §3). `orchestration` may import from all four. Violations surface as circular-import errors.

> There is no `contract/events.py` any more. The old `ControlEvent` / `InverterActionEvent` event types and the `outbound/event_router.py` that consumed them have been removed — dispatch reads the `Schedule`'s per-slot `StorageMode` directly (§5).

---

## 3. Layer 1 — Translation (inbound)

**Files:** everything under `inbound/` and `inbound/observer/`.

Translators are the primary HA-state readers. The only other module that touches `hass.states` is `outbound/inverter.py`, which reads live device telemetry (SoC, register state, etc.) on demand from its controller methods. All remaining modules are pure Python. The shared `ha_state.py` helpers centralise the unavailable/unknown guard and unit normalisation used by every reader.

Each translator lives in its own module — there is no monolithic `translators.py`. Translator modules do **not** import the `homeassistant` package — they accept `hass: Any` and call methods on it at runtime, which keeps them unit-testable without an HA harness. Each has a synchronous `.parse(hass, now)` (or equivalent) and an asynchronous `.translate(...)` wrapper called by the coordinator, and declares an `output_type` class attribute used by `run_translators` to key the primary dict.

**Telemetry seam (`inbound/telemetry/**`).** The scalar inverter signals (PV/grid/battery power, daily counters, SoC, …) are read through a vendor-neutral seam that mirrors the outbound driver: `resolve_bindings` maps each `TelemetrySignal` to an ordered chain of entity `SignalBinding`s; a per-vendor `TelemetryCodec` (`SolisCodec` / `GenericCodec`) performs the one canonical sign/unit transform; and `HaTelemetryReader.read(...)` does the freshness-aware live read. The same codec is reused by `observer/recorder_resample.py`, so live and historical reads cannot diverge. Battery telemetry (SoC / signed power / today's charge-discharge energy) stays behind `inbound/battery_source.py` (`BatterySource`: `InverterBatterySource`, `JkBmsBatterySource`, `ChainedBatterySource`) so a dedicated BMS can supersede the inverter's view per-field. Entity discovery is a registry (`inverter_discovery.py` + the declarative `platform_profiles.py`); the front door `inverter_entity_resolver.py` is vendor-neutral and delegates to `discovery_for(platform)`. See [`telemetry_seam.md`](telemetry_seam.md).

| Translator | Output type | Module | Source |
|---|---|---|---|
| `NordpoolTranslator` (+ `Entsoe`/`OctopusAgile`/`Amber`/`TouSchedule`) | `PriceFeedData` (`NordpoolData` alias) | `pricing.py` | Selected by `build_price_translator` per `CONF_PRICE_SOURCE`. Nordpool prefers `raw_today`/`raw_tomorrow` (15-min or hourly; resolution auto-detected), falls back to legacy `today`/`tomorrow`; the others read their integration's documented schema. |
| `SolarTranslator` | `SolarData` | `forecast.py` | Open-Meteo `watts` dict preferred; Forecast.Solar / Solcast `forecast` attribute fallback. Sums multi-panel / today+tomorrow sources. |
| `BatteryTranslator` | `BatteryReading` | `battery.py` | `BatterySource` for SoC/power (BMS-or-inverter); grid via the telemetry reader; household-load sensor read inline (with default fallback). |
| `PvPowerTranslator` / `GenerationTranslator` | `PvPowerReading` / `GenerationReading` | `observer/generation.py` | Instantaneous PV power + daily-resetting generation counter. |
| `GridImport/ExportPowerObserver` | `GridPowerReading` | `observer/grid.py` | Per-direction grid power (directional sensor, or signed net projected onto a side). |
| `GridImport/ExportTotalTranslator` | `Grid*TodayReading` | `observer/grid.py` | Daily-resetting cumulative import/export kWh counters. |
| `AcPortPowerTranslator` / `BackupPowerTranslator` | `DerivedPowerSample` parts | `observer/derived.py` | AC-port and backup-load power for the synthetic consumption/losses series. |
| `HouseholdConsumptionTranslator` | `HouseholdConsumptionReading` | `household_consumption.py` | Today-total household-load kWh counter. |
| `InverterModeTranslator` | `InverterModeReading` | `inverter_mode.py` | Reads + decodes the live mode via `InverterControlDriver.observe` (Solis: register 43110 + currents + RC setpoint → `StorageMode`); carries no register knowledge itself. |

All translators run in parallel via `asyncio.gather` (`run_translators` in `pipeline/dag_engine.py`) before the DAG starts.

### Inbound normalisers (pure Python, no HA)

Several files in `inbound/` are not translators — they are pure assembly functions called by DAG nodes or by the coordinator:

- **`inbound/pricing.py`** — `build_price_series_72h(feed, yesterday, config, now, source)` combines `YesterdayPrices.entries + feed.entries` (`feed: PriceFeedData`), applies the tariff, and uses `feed.resolution` as the source of truth. Function `PricingNode` calls. This module is also the multi-region **price-source seam** (`build_price_translator` → one of 5 translators, all emitting `PriceFeedData`).
- **`inbound/forecast.py`** — `build_generation_series(solar, price_series, now)` resamples `SolarData.entries` onto the `PriceSeries` grid to produce a continuous 72h `GenerationSeries` plus per-day totals. Function `GenerationNode` calls.
- **`inbound/battery.py`** — `build_battery_status(reading, config)` combines configured limits with observed SoC into an immutable `BatteryStatus`. Function `BatteryStatusNode` calls.
- **`inbound/observer/**`** — the shared `ObservedSeriesEngine` builds per-slot kWh series from rolling sample histories; `bake_in.py` finalises yesterday once per (date, side); `derived.py` composes the synthetic consumption/losses series; `consumption_daily.py` finalises per-day consumption buckets.

See `docs/MODULES.md` for per-module reference (description, exposed type, dependencies, test coverage).

---

## 4. Layer 2 — DAG engine and nodes (pipeline)

**Files:** `pipeline/dag_engine.py`, `pipeline/nodes/tier{1,2,3,4}.py` (re-exported through `pipeline/nodes/__init__.py`).

### DagNode contract

Every node declares its output and dependency types as class attributes:

```python
class SomeNode(DagNode):
    output_type: type                 # the type this node deposits into NodeContext.secondary
    consumes: list[type]              # primary/secondary types the node requires (gates readiness)
    consumes_optional: list[type] = []  # types read via ctx.get; raise the tier but don't gate readiness
```

Tiers are **not** declared. At construction the engine derives each node's tier by longest-path layering over the `consumes` **+** `consumes_optional` / `output_type` graph (`_assign_tiers`): a node's tier is one more than the deepest tier among the nodes producing the secondary types it consumes (optional or not), or `0` when it consumes only primary inputs. Adding a node is a non-decision — its tier falls out of what it consumes. A cycle (a node transitively consuming its own output) has no finite layering, so construction raises `DependencyCycleError`.

`consumes_optional` exists for nodes that read a *secondary* output via `ctx.get` but must still run when that producer fails — e.g. `ScheduleNode` reads `ProfitabilityScore` optionally: listing it under `consumes` would make a failed `ProfitabilityNode` skip the whole schedule, but it must still force `ScheduleNode`'s tier above its producer.

### Readiness and tier execution

After `_compute()` returns, `run()` deposits the result in `ctx.secondary[output_type]`. A node is "ready" for a tier when every type in `consumes` is present in `ctx.primary` or `ctx.secondary`. Because each tier runs to completion before the next begins, every lower-tier output a node depends on is present by the time its tier is evaluated.

```python
for tier_num in sorted(nodes_by_tier):
    ready = [n for n in nodes_by_tier[tier_num] if n.all_secondary_deps_satisfied(ctx)]
    results = await asyncio.gather(*[n.run(ctx) for n in ready])
```

All ready nodes in a tier run concurrently. Coordinator-injected primaries are always pre-populated and never block readiness.

### NodeContext

```python
@dataclass
class NodeContext:
    primary:   dict[type, Any]    # translator + coordinator-injected primary data
    secondary: dict[type, Any]    # node outputs — accumulated tier by tier
    config:    SunSaleConfig      # tariff + battery config
    now:       datetime

    def require(self, t) -> Any   # raises MissingDependencyError if absent
    def get(self, t) -> Any | None  # returns None if absent (optional deps)
```

---

## 5. Layer 3 — Inverter control module, driver seam, and output adapters (outbound)

**Files:** `outbound/inverter_control_module.py`, `outbound/driver.py`, `outbound/driver_factory.py`, `outbound/solis_driver.py`, `outbound/entity_control.py`, `outbound/storage_mode_specs.py`, `outbound/inverter.py`, the `*_driver.py` vendor modules.

**The driver seam.** Platform-specific control lives behind two protocols in `outbound/driver.py`:

- `InverterDriver` — the minimal platform-neutral surface: `apply_mode`, `refresh_rc`, read grid power.
- `InverterControlDriver` — adds what the control module orchestrates against: `spec_for` / `needs_keepalive` / `control_surface` / `decode_observed` / `observe`. **All per-platform register knowledge lives behind these** — the control module's verify/reconcile loop never names a register.

`outbound/driver_factory.make_inverter_driver` is the single dispatch point: Solis returns `SolisDriver` (which wraps `InverterController` for register reads/writes and owns the `build_specs` / `decode_mode` table in `storage_mode_specs.py`); Huawei / SolaX / Sungrow / GoodWe / Deye return an `EntityControlDriver` (`entity_control.py`) built from a neutral entity/service map (implemented, **untested against hardware**); every notes-only (Fronius / SMA / Kostal) or unknown platform falls back to a telemetry-only `SolisDriver` wrapper whose `apply_mode` no-ops, preserving "read but don't write". (Battery telemetry is deliberately *not* on these interfaces — it lives in `inbound/battery_source.py`; see §3.)

**The control module is driver-neutral.** Dispatch is **not** event-driven and DAG nodes emit nothing — the `Schedule` already encodes the target `StorageMode` per slot. After the DAG run the coordinator calls `InverterControlModule.tick(...)` once per cycle, which runs **observe → plan → act → verify → reconcile** against the injected `InverterControlDriver`:

1. **Observe.** Read the live mode via `driver.observe`, compare against the last entry of the rolling `InverterModeHistory`. On a decoded change, append an `InverterModeChange` and prune samples older than start-of-yesterday (local time).
2. **Plan.** Look up the current `Schedule` slot and resolve its target mode into the driver's opaque spec via `driver.spec_for(mode)`.
3. **Act (write once, on change).** `driver.apply_mode` runs only on the cycle the resolved target *differs* from `last_commanded_mode` (a button press or a slot boundary). An unchanged engaged target re-asserts nothing — except keepalive (RC-backed) modes, where `driver.refresh_rc` refreshes the RAM-only registers each cycle so the inverter does not expire the function.
4. **Verify.** After a commanded change the module re-reads the driver's control surface on a short schedule (+2 s, then every 5 s up to ~30 s), retries once on mismatch, and settles on `ok` / `mismatch`.
5. **Reconcile.** While the schedule holds the same mode, a *later* drift (inverter changed at its own screen, register slipped) is re-commanded after a debounce; a `mismatch` that later reads clean self-heals back to `ok`.

A **mode override** (`select.sunsale_mode_override`) is the single source of operator intent. When set, the dispatcher resolves the override as the target regardless of the automation switch; the switch only gates the *scheduler* path. A button press routes straight to `dispatch_override` (plan → act → verify) without a full coordinator refresh. The full state-machine contract is in [`control_loop.md`](control_loop.md); the Solis register details are in [`solis_control.md`](solis_control.md).

---

## 6. Orchestration and update cycle

**Files:** `orchestration/coordinator.py`, `orchestration/persistent_store.py`, `orchestration/history_stores.py`, `orchestration/debug_view.py`

`SunSaleCoordinator` is a `DataUpdateCoordinator` subclass with `update_interval = UPDATE_INTERVAL_MINUTES` (5 min).

`async_setup()` (once at integration load):

1. Build `TariffConfig`, `BatteryConfig`, `SunSaleConfig` from the config entry.
2. Instantiate `InverterController`, select the `BatterySource`, and build the `InverterControlDriver` via `driver_factory.make_inverter_driver` (Solis register driver / entity-driven vendor driver / telemetry-only fallback).
3. Build the translator list, DAG node list, `DagEngine`, and `InverterControlModule` (injected with the driver).
4. Load all `PersistentStore[T]` instances. The rolling sample histories (generation, PV power, both grid-power directions, both grid today-totals, derived cross-stream sample) are declared once via `history_stores.py:HistoryStoreSpec`; the rest (capacity, yesterday prices, price-peak history, inverter-mode history, monthly-bill state, forecast-quality EMAs, baked-observed history, consumption-daily buckets, inverter-time samples) are declared individually.

`_async_update_data()` (every cycle):

1. **Translate** — `run_translators(...)` runs all translators in parallel; collect the `primary` dict.
2. **Inject coordinator primaries** — append per-cycle observations into their `PersistentStore[T]`s with trim, then deposit history primaries (`*History`, `BakedObservedHistory`, `DerivedPowerHistory`, `ConsumptionDailyBuckets`, `PriceHistory`, …) plus `YesterdayPrices`, `EstimatedCapacity`, and `SchedulePolicy` (from the switch/number knobs) into `primary`.
3. **Day-boundary work** — capture pre-rollover counter snapshots (`pre_rollover_snapshot.py`, optionally shifted by the inverter clock skew), bake yesterday's observed series once per (date, side) (`observer/bake_in.py`), finalise yesterday consumption buckets, and rotate yesterday prices/solar.
4. **Capacity estimation** — update the `CapacityEstimator` from the inverter's energy counters and persist; inject `EstimatedCapacity`.
5. **DAG run** — `DagEngine.run(primary, config, now)` executes tiers T1→T4; returns the `secondary` dict.
6. **Inverter control tick** — call `InverterControlModule.tick(...)` (observe → plan → act → verify → reconcile).
7. **Build sensor dict** — map type-keyed `secondary` entries to the string-keyed dict sensors read (`"pricing"`, `"forecast"`, `"calculation"`, `"schedule"`, `"battery_state"`, `"battery_status"`, `"battery_runtime"`, `"degradation_cost"`, `"estimated_capacity"`, `"profitability_score"`, `"observed_generation"`, `"observed_grid"`, `"observed_consumption"`, `"observed_losses"`, `"forecast_accuracy"`/`"forecast_quality"`, `"monthly_bill"`, `"consumption_today_kwh"`, `"today_*_live_kwh"`, …).

The coordinator contains no domain computation — it owns the schedule, the persistent stores, and the string↔type bridge to sensors. The verify loop runs on `async_call_later` *between* ticks and pushes an `on_state_change` callback so the panel tracks it in real time.

`debug_view.py` registers an HTTP view at `/api/sun_sale/debug` exposing the most recent `primary` / `secondary` / inputs / outputs as JSON for the panel UI and `tools/integration_check.py`.

---

## 7. Node reference

The Tier column is illustrative — tiers are derived from the `consumes` (+ `consumes_optional`) / `output_type` graph at construction (§4), not declared on the node.

| Node | Tier | Consumes | Produces |
|---|---|---|---|
| `PricingNode` | 1 | `PriceFeedData`, `YesterdayPrices` | `PriceSeries` (72h) |
| `BatteryStateNode` | 1 | `BatteryReading`, `EstimatedCapacity` | `BatteryState` |
| `BatteryStatusNode` | 1 | `BatteryReading` | `BatteryStatus` |
| `BaseLoadProfileNode` | 1 | `ConsumptionDailyBuckets` | `BaseLoadProfile` |
| `GenerationNode` | 2 | `SolarData`, `PriceSeries` | `GenerationSeries` |
| `ObservedGenerationNode` | 2 | `PvPowerHistory`, `PriceSeries`, `BakedObservedHistory` | `ObservedGenerationSeries` |
| `ObservedGridNode` | 2 | `GridImportPowerHistory`, `GridExportPowerHistory`, `PriceSeries`, `BakedObservedHistory` | `ObservedGridSeries` |
| `ObservedConsumptionNode` | 2 | `DerivedPowerHistory`, `PriceSeries`, `BakedObservedHistory` | `ObservedConsumptionSeries` |
| `ObservedLossesNode` | 2 | `DerivedPowerHistory`, `PriceSeries`, `BakedObservedHistory` | `ObservedLossesSeries` |
| `DegradationNode` | 2 | `BatteryState` | `DegradationCost` |
| `BatteryRuntimeNode` | 2 | `BatteryStatus`, `BaseLoadProfile` | `BatteryRuntimeEstimate` |
| `ProfitabilityNode` | 2 | `PriceSeries`, `PriceHistory` | `ProfitabilityScore` |
| `LockoutNode` | 3 | `PriceSeries`, `GenerationSeries`, `BatteryState` | `CalculationResult` |
| `ForecastAccuracyNode` | 3 | `GenerationSeries`, `ObservedGenerationSeries` | `ForecastAccuracyResult` |
| `MonthlyBillNode` | 3 | `PriceSeries`, `ObservedGridSeries` | `MonthlyBillResult` |
| `ScheduleNode` | 4 | `PriceSeries`, `CalculationResult`, `GenerationSeries`, `BatteryState`, `DegradationCost`, `BaseLoadProfile`, `SchedulePolicy` (+ optional `ProfitabilityScore`, primary `InverterModeReading`) | `Schedule` |

There is no `ChargingProfileNode` — the SoC-bucketed DP in `ScheduleNode` subsumes the old solar-disposition stage. Dispatch happens post-DAG via `InverterControlModule.tick()`, not via events.

---

## 8. Data contracts

All types are dataclasses in `contract/models.py`. Frozen unless they're mutated in place by the coordinator.

### Primary data (selected; in `NodeContext.primary`)

| Type | Origin | Key fields |
|---|---|---|
| `PriceFeedData` (`NordpoolData` alias) | price-feed translator | `entries: list[PriceEntry]`, `resolution: timedelta` |
| `YesterdayPrices` | Coordinator | `entries: tuple[PriceEntry, ...]` (empty when stored date ≠ yesterday) |
| `SolarData` | `SolarTranslator` | `entries`, `total_today_kwh`, `today_remaining_kwh`, `primary_source` |
| `BatteryReading` | `BatteryTranslator` | `soc`, `power_kw`, `grid_power_kw`, `household_load_kw` |
| `EstimatedCapacity` | Coordinator (`CapacityEstimator`) | `value_kwh: float` |
| `*History` / `BakedObservedHistory` / `DerivedPowerHistory` / `ConsumptionDailyBuckets` | Coordinator | rolling persisted samples re-injected each cycle |
| `SchedulePolicy` | Coordinator (switch/number knobs) | mode-change penalty, profitability tilt, terminal-value discount, allow-* flags |

### Secondary data (in `NodeContext.secondary`)

| Type | Produced by | Key fields |
|---|---|---|
| `PriceSeries` | `PricingNode` | `slots: tuple[PriceSlot, ...]` (72h), `resolution`, `computed_at`; helpers `slot_at(t)`, `window(t1, t2)` |
| `BatteryState` | `BatteryStateNode` | `soc`, `estimated_capacity_kwh` |
| `BatteryStatus` | `BatteryStatusNode` | `total_capacity_kwh`, `max_charge/discharge_power_kw`, `soc`, `remaining_capacity_kwh` |
| `GenerationSeries` | `GenerationNode` | `slots: tuple[GenerationSlot, ...]`, `primary`, `overlays`, `computed_at` |
| `DegradationCost` | `DegradationNode` | `value_kwh` |
| `CalculationResult` | `LockoutNode` | `slots`, `feed_in_lockout_windows`, `total_negative_sale_kwh`, `computed_at` |
| `Schedule` | `ScheduleNode` | `slots: list[ScheduleSlot]` (each carries target `StorageMode`), `total_expected_profit_eur`, `degradation_cost_per_kwh`, `computed_at` |
| `ObservedGenerationSeries` | `ObservedGenerationNode` | per-slot kWh; yesterday baked-in, today raw |
| `ObservedGridSeries` | `ObservedGridNode` | per-slot gross `import_kwh` / `export_kwh`, today totals |
| `ObservedConsumptionSeries` / `ObservedLossesSeries` | `ObservedConsumption/LossesNode` | synthetic per-slot kWh from the derived cross-stream sample |
| `BatteryRuntimeEstimate` | `BatteryRuntimeNode` | hours of household runtime from current SoC against the P10 baseload |
| `BaseLoadProfile` | `BaseLoadProfileNode` | 24 hourly P10 kW buckets from consumption-daily buckets |
| `ProfitabilityScore` | `ProfitabilityNode` | `score ∈ [0, 1]` (None until enough history), `today_peak_eur_kwh`, `day_class` |
| `ForecastAccuracyResult` | `ForecastAccuracyNode` | `errors: ForecastErrorSeries`, EMA buckets |
| `MonthlyBillResult` | `MonthlyBillNode` | `live_slots`, `carry_eur`, `running_total_eur`, `previous_month_total_eur` |

`PriceSlot` carries `buy_eur_kwh`, `sell_eur_kwh` (can be negative or zero), `spot_eur_kwh`, and `sources` for diagnostics. Sellability (the strict `> 0` check) lives downstream in `slot_physics` / `schedule`.

---

## 9. Key design decisions

**HA boundary.** HA imports are confined to the root entry points (`__init__.py`, `config_flow.py`, `sensor.py`, `switch.py`, `select.py`, `number.py`), `orchestration/`, and the outbound writers (`outbound/inverter.py`, `entity_control.py`, the entity-driven `*_driver.py` modules). Inbound translators read `hass.states` but never import `homeassistant` — they work on a duck-typed `hass`. Every other module is pure Python and testable with plain `pytest`.

**Sub-package layering enforces direction.** `contract` depends on nothing. `inbound` / `pipeline` / `outbound` depend only on `contract`. `orchestration` is the only layer allowed to glue them. Enforced by convention (a violation surfaces as a circular import).

**Inbound owns the 72h pricing assembly.** The Nordpool translator emits today + tomorrow only; the coordinator supplies yesterday as `YesterdayPrices`; `inbound/pricing.build_price_series_72h` combines them and applies the tariff.

**Tiers derived at construction, cycles caught there.** Tiers are computed by topological longest-path layering over the `consumes`/`consumes_optional`/`output_type` graph (`DagEngine._assign_tiers`), not hand-assigned. A dependency cycle raises `DependencyCycleError` at startup.

**Dispatch is post-DAG and event-free.** Pipeline nodes emit nothing; the `Schedule` carries the target `StorageMode` per slot and `InverterControlModule.tick()` actuates after the DAG. The old `ControlEvent` types and `event_router.py` are gone.

**Write-once → verify → reconcile, not blind re-assert.** The control module writes only when the resolved target changes, then verifies the registers took and reconciles a later drift — instead of re-asserting the same mode every cycle (which wears flash and never confirmed engagement). RC-backed modes are the one exception, refreshed each cycle so the inverter doesn't expire them. See [`control_loop.md`](control_loop.md).

**Platform-neutral driver seam, both sides.** The control module speaks only `InverterControlDriver` (`outbound/driver.py`); the read path speaks only `TelemetrySignal` through the `inbound/telemetry/**` seam. Per-vendor register/entity knowledge is confined to a closed tuple — `{ Driver, Codec, bindings, Discovery, BatterySource? }` — registered at one dispatch point each (`driver_factory` / `discovery_for`). A new vendor is a reviewable checklist, not a per-file porting exercise. Solis (register-level) is the only hardware-verified platform; the entity-driven vendors are implemented but untested against hardware. See [`telemetry_seam.md`](telemetry_seam.md).

**SoC-bucketed dynamic programming.** The scheduler is backward DP over `(slot, soc_bucket)` cells with per-slot physics delegated to `slot_physics.simulate_slot`, so the planner and any diagnostics agree on energy flows for the same `(mode, slot)` pair. It replaced the earlier greedy pair-match + charging-profile stage.

**Coordinator-injected primary data.** `YesterdayPrices`, `EstimatedCapacity`, `SchedulePolicy`, and every `*History` primary are stateful values the coordinator owns across cycles (loaded from `PersistentStore[T]`, appended-and-trimmed each cycle) and deposited into `primary` before `DagEngine.run()`. This keeps DAG nodes stateless and the engine free of cross-cycle state.
