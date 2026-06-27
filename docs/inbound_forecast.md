# Inbound — Forecast module

Reference for `custom_components/sun_sale/inbound/forecast.py`.

The forecast module owns the **72h yesterday→today→tomorrow `GenerationSeries`** that downstream consumers (calculation, schedule, sensors, dashboard) read for expected solar generation. It is pure Python with no Home Assistant imports.

## Summary

`SolarTranslator` reads Open-Meteo `watts` (preferred) or Forecast.Solar/Solcast `forecast`; sums multi-source / today+tomorrow entities. `build_generation_series` resamples onto the `PriceSeries` grid 1:1 (zero-filled), computes per-day totals and `today_remaining_kwh`.

- **Exposes:** `SolarData` (translator), `GenerationSeries` (helper).
- **Depends on:** `contract.models`.
- **Tests:** `tests/test_forecast.py` — full per-test coverage table in §8.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `GenerationSeries`](#4-output-generationseries)
5. [Resampling](#5-resampling)
6. [Per-day totals](#6-per-day-totals)
7. [Integration in the DAG](#7-integration-in-the-dag)
8. [Tests](#8-tests)

---

## 1. Responsibilities

- Resample solar entries onto the `PriceSeries` grid so every price slot has exactly one matching generation slot.
- Produce a **continuous 72h** series (yesterday 00:00 → tomorrow 23:59). Slots without overlapping solar are emitted with `expected_kwh = 0.0` — coverage is contiguous, never sparse.
- Compute the four scalar totals consumers actually need: `total_yesterday_kwh`, `total_today_kwh`, `total_tomorrow_kwh`, `today_remaining_kwh`. Nothing else is added.
- Treat yesterday-from-store identically to today/tomorrow-from-HA. How the inputs were obtained is invisible to downstream modules — they see one uniform 72h series.

The yesterday-stitching that pricing handles via `YesterdayPrices` is, for solar, currently performed in the coordinator by prepending stored `SolarEntry` items onto `SolarData.entries` before the DAG runs. The forecast module itself does not know the difference between yesterday/today/tomorrow segments — it just resamples whatever entries it receives.

---

## 2. Public API

```python
def build_generation_series(
    solar: SolarData,
    price_slots: tuple,                # PriceSeries.slots
    now: datetime | None = None,
    local_tz: tzinfo | None = None,    # used for date-bucket totals
) -> GenerationSeries
```

Resample `solar.entries` onto `price_slots` and compute per-day totals.

- `now` defaults to `datetime.now(timezone.utc)` and is recorded as `GenerationSeries.computed_at`. It is also the reference for the yesterday/today/tomorrow date buckets and the `today_remaining_kwh` "future" cutoff.
- `local_tz` controls the date-bucket boundaries (yesterday / today / tomorrow). Defaults to UTC when omitted; the coordinator passes `ctx.config.local_tz`.
- If `solar.entries` is empty or `price_slots` is empty, returns an empty `GenerationSeries` with all totals at `0.0`.
- Otherwise emits **exactly `len(price_slots)` generation slots**, one per price slot, in the same order.

Two private helpers do the work and are not part of the public surface but are useful to understand:

- `_resample_to_grid(entries, target_slots, source)` — overlap-weighted redistribution.
- `_compute_totals(slots, now)` — date-bucket sums over the resampled slots.

---

## 3. Inputs

**`SolarData`** (`contract/models.py`) — produced by `inbound.forecast.SolarTranslator`. Covers today + tomorrow from the configured HA entities (Open Meteo `watts` dict preferred; Forecast.Solar / Solcast `forecast` attribute as fallback). Multiple panels (`entity_1`, `entity_2`) are summed per-timestamp in the translator. The coordinator prepends yesterday's persisted entries before passing the value to the DAG.

`SolarData.primary_source` ∈ `{"open_meteo", "forecast_solar", "none"}` — used verbatim as the `source` label on every emitted `GenerationSlot`. Yesterday slots inherit this label too: consumers cannot tell from the slot whether it came from the store or from HA.

**`PriceSeries`** — the 72h price grid produced by `PricingNode`. The forecast module trusts it as the single source of truth for slot granularity and window. There is no independent re-derivation of resolution and no rederivation of the 72h window inside the forecast module.

---

## 4. Output: `GenerationSeries`

```python
@dataclass(frozen=True)
class GenerationSeries:
    slots: tuple[GenerationSlot, ...]
    primary: str                       # "open_meteo" | "forecast_solar" | "none"
    overlays: tuple[str, ...]          # reserved; always () today
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_kwh: float = 0.0
    total_tomorrow_kwh: float = 0.0
    today_remaining_kwh: float = 0.0

    def energy_between(self, t1: datetime, t2: datetime) -> float
```

Each `GenerationSlot` carries:

| Field | Meaning |
|---|---|
| `start`, `end` | UTC slot boundaries, identical to the matching `PriceSlot` |
| `expected_kwh` | Overlap-weighted solar energy for this slot, rounded to 6 decimals; `0.0` where no solar overlaps |
| `source` | `solar.primary_source` for every slot (uniform across yesterday/today/tomorrow) |
| `confidence` | `None` (sources don't expose per-slot confidence today) |

`energy_between(t1, t2)` keeps the existing fractional-overlap semantics: it sums `expected_kwh * (overlap_secs / slot_secs)` across slots whose source equals `primary`. Useful for queries that don't align to the grid.

---

## 5. Resampling

Every emitted slot's kWh is

```
expected_kwh(target) = Σ entry.expected_kwh * overlap_seconds / entry_duration_seconds
```

over all solar entries that intersect the target slot. The same formula handles **both directions**:

- **Downsampling** (e.g. four 15-min watts samples → one 1h slot): each 15-min entry contributes its full kWh because it sits entirely inside the hour.
- **Upsampling** (e.g. one 1h Forecast.Solar entry → four 15-min slots): the entry's kWh is split into quarters proportional to overlap.

Mixed grids (some entries 15-min, others hourly) compose without special-casing.

Every target slot is emitted — including those with zero overlap, where `expected_kwh = 0.0` — so the output covers the full price grid continuously. This is what makes the 72h coverage invariant hold even when solar data is partial (e.g., no stored yesterday on first run, or HA tomorrow entity empty).

---

## 6. Per-day totals

Buckets are derived from `now.date()` (UTC):

| Field | Slots included |
|---|---|
| `total_yesterday_kwh` | `slot.start.date() == now.date() - 1 day` |
| `total_today_kwh` | `slot.start.date() == now.date()` |
| `total_tomorrow_kwh` | `slot.start.date() == now.date() + 1 day` |
| `today_remaining_kwh` | `slot.start.date() == today` **and** `slot.start >= now` |

`today_remaining_kwh` is a strict `>=` on slot start, matching the pre-resample convention from `_make_solar_data` in `translators.py`. The slot currently in progress (whose `start < now`) is **not** included; precision is whatever the grid resolution gives (15-min grids yield a tighter "remaining" than hourly).

Totals are rounded to 4 decimals.

---

## 7. Integration in the DAG

`GenerationNode` (`pipeline/nodes/tier2.py`):

```python
output_type = GenerationSeries
consumes = [SolarData, PriceSeries]
```

Tiers are not declared — `DagEngine._assign_tiers` derives them from `consumes`. This node lands in tier 2 because it depends on the tier-1 secondary output `PriceSeries`. The translator-produced `SolarData` is primary; the coordinator mutates `SolarData.entries` to prepend yesterday before `DagEngine.run(...)` is invoked.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[SolarData]` (today + tomorrow from HA).
2. If the persistent yesterday store's date is exactly yesterday, prepend `SolarEntry` items from the store onto `solar_data.entries`.
3. `DagEngine.run(primary, ...)` → `PricingNode` produces `PriceSeries` → `GenerationNode` calls `build_generation_series(solar, price_series.slots, now=ctx.now, local_tz=ctx.config.local_tz)` → `secondary[GenerationSeries]`.
4. After the cycle, today's solar entries (filtered out of `SolarData.entries` by date) are written to the yesterday store for the next day.

Downstream consumers:
- `LockoutNode` (`pipeline/calculation.py`) — calls `generation.energy_between(slot.start, slot.end)` per price slot. The 1:1 grid alignment means each call returns exactly the matching slot's `expected_kwh`.
- `ScheduleNode` (`pipeline/schedule.py`) — iterates `generation.slots` directly.
- `ForecastAccuracyNode` (`pipeline/forecast_accuracy.py`) — pairs each forecast slot against the observed series. (The former `ChargingProfileNode` consumer was removed; per-slot solar disposition now lives inside the DP via `pipeline/slot_physics.py`.)
- `sensor.py` "Solar forecast" diagnostic sensor — exposes the full slot list plus the four totals for chart rendering.

---

## 8. Tests

All forecast tests live in `tests/test_forecast.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `BASE_DT`, `make_price()`, and `default_tariff_config()`.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_forecast.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Empty / missing | `test_empty_when_no_data` | Empty `SolarData` → empty `slots`, `primary == "none"` |
| | `test_empty_when_empty_forecast_slots` | Same path with explicit empty entries list |
| | `test_empty_solar_yields_zero_totals` | All four totals = 0.0 on empty input |
| Open Meteo (watts) | `test_open_meteo_watts_parsed` | Hourly watts entries land in matching hourly slots |
| | `test_open_meteo_15min_aggregated_to_hourly` | Four 15-min watts samples → one hourly slot at the summed kWh (downsampling) |
| | `test_open_meteo_two_arrays_summed` | Translator-summed watts (2 arrays) flow through as one combined slot |
| Forecast.Solar fallback | `test_forecast_solar_pv_estimate_parsed` | `pv_estimate` attribute parsed into expected kWh |
| | `test_forecast_solar_energy_fallback` | `energy` key used when `pv_estimate` is missing |
| | `test_forecast_solar_skips_bad_entries` | Unparseable `time` field is dropped; valid entries survive |
| Primary selection | `test_primary_is_set_for_forecast_solar` | `primary == "forecast_solar"` when no watts present |
| | `test_primary_is_open_meteo_when_watts_present` | watts dict wins over `forecast` attribute |
| Resampling | `test_hourly_forecast_upsampled_to_quarter_hour_grid` | One 4 kWh hourly entry on a 15-min grid → four 1 kWh slots (upsampling) |
| | `test_resampled_slots_match_price_grid_one_to_one` | `len(gen.slots) == len(price_series.slots)`; identical start/end per slot |
| | **`test_continuous_72h_coverage_with_zero_fill`** | 72h hourly grid + a single solar entry → 72 slots emitted, nighttime/yesterday at 0.0 kWh, noon carries the kWh |
| `energy_between` | `test_energy_between_full_slot` | Full-slot overlap returns the slot's kWh |
| | `test_energy_between_partial_slot` | Half-slot overlap returns 50% of the kWh |
| | `test_energy_between_no_overlap` | Non-overlapping window returns 0.0 |
| Per-day totals | `test_per_day_totals_split_across_yesterday_today_tomorrow` | Three single-entry days on a 72h grid → totals bucketed by `start.date()` |
| | `test_today_remaining_excludes_past_slots` | `now = 10:30`: past slot excluded, future slots summed; `total_today_kwh` includes the past slot, `today_remaining_kwh` does not |
| `_tomorrow_entity` helper | `test_tomorrow_entity_today_suffix` | `..._today` → `..._tomorrow` |
| | `test_tomorrow_entity_today_infix` | `..._today_2` → `..._tomorrow_2` |
| | `test_tomorrow_entity_no_today` | No `today` substring → empty string |

### What is intentionally **not** covered here

- **HA-side solar parsing** (multi-entity merging, Open Meteo vs Forecast.Solar attribute shapes, `_tomorrow_entity` look-ups beyond the suffix helper) — covered in `SolarTranslator` tests, not in the forecast module's tests.
- **Persistent yesterday-solar store I/O** — coordinator's responsibility; covered in `tests/test_coordinator.py`.
- **End-to-end DAG wiring** (GenerationNode within the engine) — exercised in `tests/test_coordinator.py`; downstream consumption is exercised via `tests/test_calculation.py` and `tests/test_schedule.py`, which construct `GenerationSeries` directly as fixtures.
