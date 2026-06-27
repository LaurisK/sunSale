# Inbound — Generation module

Reference for `custom_components/sun_sale/inbound/observer/generation.py` (formerly `inbound/generation.py`; the observed-series logic now runs on the shared `inbound/observer/engine.py`).

The generation module produces an **`ObservedGenerationSeries`** — per-slot kWh actually produced by the PV system, aligned to the same grid as `PriceSeries` and covering **yesterday 00:00 → now**. It is pure Python with no Home Assistant imports.

It is the *observed* counterpart to `inbound/forecast.py`: forecast predicts future generation; this module measures past generation.

## Summary

`PvPowerTranslator` snapshots the inverter's instantaneous PV power (W) each cycle. The helper `build_observed_generation_series` averages those samples within each price-grid slot and converts to kWh: `slot_kwh = mean(power_W) × slot_duration_h / 1000`.

- **Today** slots are always raw power-averaged.
- **Yesterday** slots come from the once-per-day **bake-in** (`BakedObservedHistory`) when a `BakedDayRecord` exists for yesterday's local date; otherwise they too are raw power-averaged (e.g. the morning after a fresh install, before any bake has run).

The proportional end-of-day correction that reconciles a day's slot sum against the inverter's authoritative daily-total counter is **not** in this builder — it lives in `inbound/observer/bake_in.py:try_bake_yesterday` (see `CLAUDE.md` → *Observed-series bake-in*). `GenerationTranslator` (the daily-resetting today-total kWh counter) feeds that bake-in and the live `today_generation_live_kwh` sensor, **not** this per-cycle series.

- **Exposes:** `PvPowerTranslator`, `GenerationTranslator` (translators), `build_observed_generation_series` (helper).
- **Depends on:** `contract.models`, `inbound.observer.engine`, `ha_state`.
- **Tests:** `tests/test_generation_inbound.py` — full per-test coverage table in §9.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `ObservedGenerationSeries`](#4-output-observedgenerationseries)
5. [Per-slot energy derivation](#5-per-slot-energy-derivation)
6. [Window and clamping](#6-window-and-clamping)
7. [Per-day totals](#7-per-day-totals)
8. [Integration in the DAG](#8-integration-in-the-dag)
9. [Tests](#9-tests)

---

## 1. Responsibilities

- Reconstruct per-slot kWh on the `PriceSeries` grid by averaging instantaneous PV-power samples within each slot.
- Emit one slot per price-grid slot in the window `[yesterday 00:00 local, now)` — historical/observed data, not future projections.
- Substitute yesterday's slots with the finalised baked values when a `BakedDayRecord` is present, so yesterday reflects the inverter's authoritative daily total; otherwise leave yesterday raw.
- Compute two scalar totals: `total_yesterday_kwh`, `total_today_so_far_kwh`.
- Survive sampling gaps and midnight resets: each day is bucketed on its own window, so the cross-midnight boundary is never differenced.

What this module deliberately does **not** do:

- It does not read Home Assistant state. The translators snapshot HA; the coordinator persists the rolling `PvPowerHistory` and deposits it (and `BakedObservedHistory`) as primary.
- It does not perform the proportional bake-in scaling itself — that is `bake_in.py`'s job, surfaced here only as the already-baked yesterday slots.
- It does not back-fill missing power samples. A slot with no PV-power samples gets `0.0` kWh on the raw path.
- It does not look ahead. Slots whose `start >= now` are not emitted, and the slot in progress is clamped to end at `now`.

---

## 2. Public API

```python
def build_observed_generation_series(
    pv_power_history: PvPowerHistory,
    price_slots: tuple,                   # PriceSeries.slots
    now: datetime | None = None,
    local_tz: tzinfo = timezone.utc,
    baked_history: BakedObservedHistory | None = None,
) -> ObservedGenerationSeries
```

`now` defaults to `datetime.now(timezone.utc)`. It is recorded as `computed_at`, is the right edge of the emitted window (the in-progress slot is clamped to `now`), and is the reference for the yesterday/today date buckets.

`local_tz` controls day-boundary calculations (yesterday-start, today-start). Defaults to UTC; the coordinator passes `ctx.config.local_tz`.

Decision tree at call time:

1. If `price_slots` is empty **or** `pv_power_history.samples` is empty → empty series.
2. Build **today** slots by raw power-averaging over `[today 00:00 local, now)`.
3. Build **yesterday** slots: if `baked_history` carries a `BakedDayRecord` for the generation side on yesterday's local date, use its `baked_slots`; otherwise raw power-average over `[yesterday 00:00 local, today 00:00 local)`.
4. Concatenate `yesterday + today` slots; compute the two day totals.

---

## 3. Inputs

**`PvPowerHistory`** — `tuple[PvPowerReading, ...]` ordered by `timestamp` ascending. Each `PvPowerReading` is `(power_w: float, timestamp: datetime)` — a single snapshot of the inverter's instantaneous PV power. Built by the coordinator across cycles and persisted; trimmed to the configured retention window (see `contract/const.py`). Per-cycle samples are additionally lifted to the inverter's native update rate by `inbound/observer/recorder_resample.py` (HA recorder replay) before the DAG runs. This is the input used to derive every per-slot kWh.

**`BakedObservedHistory`** — the persisted, finalised per-(date, side) records produced once per day by `inbound/observer/bake_in.py`. This module reads only the **generation-side** record for yesterday's local date, substituting its `baked_slots` for the raw yesterday slots. When absent (no bake yet), yesterday stays raw.

**`price_slots`** — the slot tuple from the 72h `PriceSeries` produced by `PricingNode`. Used as the single source of truth for slot boundaries and resolution; the generation module never re-derives them.

> `GenerationTranslator` (the daily-resetting today-total kWh counter) is **not** consumed by this builder. Its `GenerationReading` samples feed the bake-in's authoritative yesterday-total (via `inbound/yesterday_total_resolver.py`) and the live `today_generation_live_kwh` sensor.

---

## 4. Output: `ObservedGenerationSeries`

```python
@dataclass(frozen=True)
class ObservedGenerationSeries:
    slots: tuple[ObservedGenerationSlot, ...]
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0
```

Each `ObservedGenerationSlot`:

| Field | Meaning |
|---|---|
| `start`, `end` | UTC slot boundaries, identical to the matching `PriceSlot` |
| `generated_kwh` | Per-slot kWh: raw power-averaged (§5a) for today and for un-baked yesterday, or the baked value (§5b) for a baked yesterday. `0.0` where the slot has no samples. |
| `source` | `"inverter"` on every slot |

Slots whose `start < yesterday 00:00 local` or `start >= now` are omitted entirely — the result is a contiguous run only within that window. Inside the window, every price slot has exactly one corresponding generation slot.

---

## 5. Per-slot energy derivation

### 5a. Raw path — instantaneous power averaging

The shared `ObservedSeriesEngine` builds slots for a window by averaging the generation side's power samples into each in-window price slot:

```
slot_kwh = mean(power_W for sample.timestamp in [slot.start, end_clamped)) × duration_h / 1000
```

`end_clamped = min(slot.end, now)` for the in-progress slot; full slots use `slot.end`. Slots with no samples in their interval get `0.0`. This path does not interpolate across gaps — gaps just produce zero-kWh slots until samples arrive. The `recorder_resample` step means each slot normally sees every point the inverter published, not just the ~5-min coordinator ticks.

Today is **always** built this way (`[today 00:00, now)`). Yesterday is built this way **only when no bake exists** (`[yesterday 00:00, today 00:00)`).

### 5b. Yesterday from the bake-in

Once per local day, `inbound/observer/bake_in.py:try_bake_yesterday` finalises yesterday's generation slots: it resolves an authoritative yesterday-total (a dedicated HA counter entity, or a pre-rollover counter snapshot), proportionally scales the raw slots to match it (`slot × counter_total / slot_sum`, skipped if the factor is out of range or the slot sum is zero), and writes a frozen `BakedDayRecord` into `BakedObservedHistory`.

This builder simply **reads** that record: when one exists for yesterday's local date, its `baked_slots` replace the raw yesterday slots verbatim. So the proportional correction that used to run inside this module now happens once, upstream, and applies to **yesterday** (the day whose counter total is final) — never to today, whose counter is still climbing. See `CLAUDE.md` → *Observed-series bake-in* for the full bake contract.

---

## 6. Window and clamping

A slot is included iff `yesterday_00:00 <= slot.start < now`. This single rule handles three boundary cases: slots before yesterday, slots in tomorrow, and slots in the future of `now`.

For the slot that contains `now` (i.e., `slot.start < now <= slot.end`), the right edge is clamped: the kWh is computed against `min(slot.end, now)`, so `total_today_so_far_kwh` reflects only the elapsed portion of the in-progress slot. The slot's `end` field on the emitted `ObservedGenerationSlot` is still the original `slot.end` (the grid is preserved); only the value is partial.

---

## 7. Per-day totals

| Field | Slots included |
|---|---|
| `total_yesterday_kwh` | the yesterday-window slots (`[yesterday 00:00, today 00:00)`) |
| `total_today_so_far_kwh` | the today-window slots (`[today 00:00, now)`) |

Both are rounded to 4 decimals. `total_today_so_far_kwh` reflects energy actually observed in slots that have already started — it grows monotonically through the day.

---

## 8. Integration in the DAG

`ObservedGenerationNode` (`pipeline/nodes/tier2.py`):

```python
output_type = ObservedGenerationSeries
consumes = [PvPowerHistory, PriceSeries, BakedObservedHistory]
```

Tiers are not declared — `DagEngine._assign_tiers` derives them from `consumes`. The node lands in tier 2 because it depends on the tier-1 secondary `PriceSeries`; `PvPowerHistory` and `BakedObservedHistory` are coordinator-injected primaries.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[PvPowerReading]` (instantaneous power snapshot) and `primary[GenerationReading]` (today-total counter snapshot). Either may be `None` if the sensor is missing/unavailable.
2. Coordinator appends each non-`None` reading to its `PersistentStore[T]` (with `recorder_resample` lifting the PV-power stream to native resolution), trims to retention, and deposits `primary[PvPowerHistory]`. On a day boundary it bakes yesterday and deposits `primary[BakedObservedHistory]`.
3. `DagEngine.run(primary, ...)` → `PricingNode` produces `PriceSeries` → `ObservedGenerationNode` calls `build_observed_generation_series(pv_power_history, price_series.slots, now=ctx.now, local_tz=ctx.config.local_tz, baked_history=...)` → `secondary[ObservedGenerationSeries]`.
4. `_build_sensor_dict` exposes the value under the `"observed_generation"` key.

A translator returning `None` simply skips that cycle's append — existing history still feeds the node. The empty-history guard covers fresh installs (empty series when there are no PV-power samples).

---

## 9. Tests

All generation-inbound tests live in `tests/test_generation_inbound.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `BASE_DT`, `make_price()`, and `default_tariff_config()`.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_generation_inbound.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Empty / degenerate | `test_empty_history_yields_empty_series` | No PV-power samples → empty `slots`, totals = 0 |
| | `test_empty_price_grid_yields_empty_series` | No price slots → no generation slots even with valid history |
| Power averaging (§5a) | `test_power_averaging_one_sample_per_slot` | A single power sample inside a slot maps to `power_W × duration_h / 1000` |
| | `test_power_averaging_multiple_samples_averaged` | Multiple samples in a slot are averaged before conversion |
| | `test_power_slot_with_no_samples_gives_zero` | No samples in a slot → `0.0` kWh (no interpolation across the gap) |
| | `test_power_path_covers_yesterday_slots` | Raw path covers the full `[yesterday 00:00, now)` window when no bake exists |
| | `test_resamples_onto_quarter_hour_grid` | Power samples resample onto a 15-min price grid |
| Baked yesterday (§5b) | `test_yesterday_slots_substituted_from_baked_history` | A `BakedDayRecord` for yesterday replaces the raw yesterday slots |
| Window | `test_slots_in_tomorrow_excluded` | Tomorrow's price slots never appear in output |
| | `test_slots_starting_at_or_after_now_excluded` | `slot.start >= now` excluded; the in-progress slot is included |
| | `test_slots_before_yesterday_midnight_excluded` | Samples from two days ago don't leak slots into output |
| Per-day totals | `test_totals_split_between_yesterday_and_today` | Yesterday/today totals bucketed by window |
| Source label | `test_source_is_inverter_on_every_slot` | Every emitted slot has `source == "inverter"` |
| Node wiring | `test_observed_generation_node_produces_series_from_pv_power` | `ObservedGenerationNode.run(ctx)` reads `PvPowerHistory` + `BakedObservedHistory` from primary and `PriceSeries` from secondary, depositing the result at `ctx.secondary[ObservedGenerationSeries]` |

### What is intentionally **not** covered here

- **HA-side reads** (`PvPowerTranslator` / `GenerationTranslator` parsing sensor state) — translator-level concern, covered via `tests/test_recorder_resample.py` and the coordinator tests.
- **The bake-in** (`try_bake_yesterday`, proportional scaling, yesterday-total resolution) — covered in `tests/test_observed_bake_in.py` and `tests/test_yesterday_total_resolver.py`; this module only consumes its output.
- **Persistent sample-history I/O** and **end-to-end engine wiring** — coordinator's responsibility, exercised in `tests/test_coordinator.py`.
</content>
