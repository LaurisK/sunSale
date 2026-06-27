# Pipeline — Base load module

Reference for `custom_components/sun_sale/pipeline/base_load.py`.

The base-load module produces two values: a **`BaseLoadProfile`** (24-hour low-percentile floor of household consumption, keyed by local hour-of-day) and a **`BatteryRuntimeEstimate`** (how long the battery can sustain that baseload before hitting `min_soc`). It is pure Python with no Home Assistant imports.

## Summary

24h hour-of-day baseload profile from `ConsumptionDailyBuckets` (P15 per bucket, local-time keyed, per-hour completeness gated) plus a battery-runtime worst-case estimate.

- **Exposes:** `BaseLoadProfile`, `BatteryRuntimeEstimate`.
- **Depends on:** `contract.{const, models}`.
- **Tests:** `tests/test_base_load.py` — full per-test coverage table in §9.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `BaseLoadProfile`](#4-output-baseloadprofile)
5. [Output: `BatteryRuntimeEstimate`](#5-output-batteryruntimeestimate)
6. [Profile algorithm](#6-profile-algorithm)
7. [Runtime-estimate algorithm](#7-runtime-estimate-algorithm)
8. [Integration in the DAG](#8-integration-in-the-dag)
9. [Tests](#9-tests)

---

## 1. Responsibilities

- Bucket finalised per-day hour-bucket consumption rollups by **local hour-of-day** (24 buckets, no weekend/holiday split) and emit a P15 floor per bucket — the load level the home reliably draws even on its quietest days for that hour.
- Always emit an exhaustive 24-slot profile: sparse buckets get a cross-bucket fallback, and a fully empty history gets a stub of `0.2 kW`. Downstream code never has to handle `None`.
- Forward-simulate **pure baseload drain** of the battery from `now` over a fixed horizon and report `runtime_minutes` + an absolute `until` timestamp.
- Be re-entrant and stateless: every call rebuilds the profile and re-runs the simulation from the inputs provided.

What this module deliberately does **not** do:

- It does not look at weekday/weekend/holiday classes. Day-class normalisation is reserved for `pipeline/profitability.py`; baseload is intentionally simpler.
- It does not model forecast solar generation in the runtime estimate. The output is a worst-case "household-only depletion" reserve, comparable across cycles and unaffected by weather noise.
- It does not model the scheduler's planned charge/discharge. Same reason: keep the estimate a fixed lower bound on the time we have before household consumption alone empties the battery.
- It does not own persistence or daily finalisation. `inbound/consumption_daily.py` finalises one `ConsumptionDayRecord` per local date from the day's `ObservedConsumptionSeries`; the coordinator persists the rolling `ConsumptionDailyBuckets` and deposits it as primary. This module only reads it.

---

## 2. Public API

```python
def build_base_load_profile(
    buckets: ConsumptionDailyBuckets,
    local_tz: tzinfo,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,                       # 30
    percentile: float = DEFAULT_PERCENTILE,                       # 0.15
    min_hour_completeness: float = CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS,
) -> BaseLoadProfile

def estimate_battery_runtime(
    battery_status: BatteryStatus,
    battery_config: BatteryConfig,
    profile: BaseLoadProfile,
    local_tz: tzinfo,
    now: datetime,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,    # 48
) -> BatteryRuntimeEstimate
```

`now` on `build_base_load_profile` defaults to `datetime.now(timezone.utc)` and is recorded as `BaseLoadProfile.computed_at`. It also defines the right edge of the rolling window — only records whose `local_date` is newer than `today_local - window_days` are considered. `now` on `estimate_battery_runtime` is required (the runtime estimate is meaningless without an explicit anchor).

Module-level tunables, all importable for tests:

| Constant | Default | Meaning |
|---|---|---|
| `MIN_HISTORY_DAYS` | `7` | Qualified day-records required in the window before per-bucket percentiles are computed. Below this, the profile is sparse — every slot gets the fallback. |
| `MIN_BUCKET_DAYS` | `5` | Per-day contributions required in a single hour-bucket before it gets its own P15. Below this, that hour falls back. |
| `DEFAULT_PERCENTILE` | `0.15` | Per-bucket percentile (the "floor"). P15 over the historical P10 to reduce the single-quiet-day anchor effect during a 7-day bootstrap window. |
| `DEFAULT_FALLBACK_PERCENTILE` | `0.20` | Cross-bucket fallback percentile — wider than P15 because it pools every hour (less signal per hour kept). |
| `DEFAULT_STUB_KW` | `0.2` | Last-resort floor on a fresh install with no qualified history. Matches `inbound/battery._DEFAULT_HOUSEHOLD_LOAD_KW`. |
| `DEFAULT_WINDOW_DAYS` | `30` | Rolling window of finalised days considered. Aligned with `CONSUMPTION_DAILY_WINDOW_DAYS`, passed explicitly to keep this module free of storage-layer coupling. |
| `CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS` | (`const.py`) | Per-day per-hour minimum fraction of expected coordinator cycles that must have produced a derived sample for that hour's contribution to qualify. |
| `DEFAULT_HORIZON_HOURS` | `48` | How far ahead `estimate_battery_runtime` simulates before giving up and returning `until=None`. |
| `SIMULATION_STEP_MINUTES` | `5` | Simulation step. Matches the coordinator update interval and gives sub-percent precision on the `until` timestamp. |

---

## 3. Inputs

**`ConsumptionDailyBuckets`** (`contract/models.py`) — `records: tuple[ConsumptionDayRecord, ...]` sorted ascending by `local_date`. Each `ConsumptionDayRecord` is one finalised local day:

- `local_date: date`
- `hour_kwh: tuple[float, ...]` — length 24, kWh consumed by local hour 0..23 (the sum of `consumed_kwh` across the price-grid slots whose local start-hour equals `h`).
- `hour_completeness: tuple[float, ...]` — length 24, the fraction (0..1) of expected price-grid slots in that hour that contributed at least one derived sample.

Records are produced once per day by `inbound/consumption_daily.try_finalise_yesterday_consumption` from the day's `ObservedConsumptionSeries`. The coordinator persists the rolling list (`STORAGE_KEY_CONSUMPTION_DAILY`), trims it to `CONSUMPTION_DAILY_WINDOW_DAYS`, and deposits the full `ConsumptionDailyBuckets` as primary each cycle. Because consumption is a *derived* observed series (see `CLAUDE.md` → *Derived-power observers*), there is no separate household-load sensor or per-cycle translator feeding this profile.

**`local_tz`** — a `tzinfo` (typically `ZoneInfo("Europe/Riga")`). The window cutoff and all hour-of-day bucketing happen in local time; the persisted records already key on local `date`, so DST and timezone changes are unambiguous. The coordinator pulls this from `hass.config.time_zone` via `SunSaleCoordinator._resolve_local_tz` and threads it through `SunSaleConfig.local_tz`, which the DAG node reads as `ctx.config.local_tz`.

**`BatteryStatus`** and **`BatteryConfig`** (runtime estimate only) — `BatteryStatus.total_capacity_kwh` is the configured nominal capacity, `BatteryStatus.soc` is the live SoC, and `BatteryConfig.min_soc` is the floor the simulation drains toward.

---

## 4. Output: `BaseLoadProfile`

```python
@dataclass(frozen=True)
class BaseLoadProfile:
    slots: tuple[BaseLoadSlot, ...]    # length 24, indexed by hour
    fallback_kw: float                  # used for sparse buckets and as a stub
    overall_p10_kw: float               # diagnostic: P10 of all qualified values
    overall_median_kw: float            # diagnostic
    confidence: float | None            # 0..1, distinct_days / window_days; None when sparse
    sample_count: int                   # total qualified per-hour contributions in window
    distinct_days: int                  # qualified day-records in window
    computed_at: datetime

    def at(self, t: datetime, local_tz) -> float
```

Each `BaseLoadSlot`:

| Field | Meaning |
|---|---|
| `hour` | Local hour 0..23. `slots[h].hour == h` always. |
| `baseload_kw` | P15 of the hour's qualified per-day contributions, **or** `profile.fallback_kw` when fewer than `MIN_BUCKET_DAYS` qualified. |
| `sample_count` | Number of qualified per-day contributions that fell into this bucket within the rolling window. |
| `is_fallback` | `True` iff `baseload_kw` came from the cross-bucket fallback rather than the bucket's own P15. |

`confidence is None` is the explicit "too sparse to trust" signal: it means fewer than `MIN_HISTORY_DAYS` qualified day-records fell in the window. In that state every slot is fallback, and `fallback_kw` is the overall P20 of whatever qualified values did make it (or `DEFAULT_STUB_KW` if there were none).

`profile.at(t, local_tz)` is a one-liner: `slots[t.astimezone(local_tz).hour].baseload_kw`. Sensors and the runtime simulation both call it.

---

## 5. Output: `BatteryRuntimeEstimate`

```python
@dataclass(frozen=True)
class BatteryRuntimeEstimate:
    remaining_kwh_usable: float         # max(0, (soc − min_soc) * total_capacity)
    avg_drain_kw_next_hour: float       # mean drain over the first simulated hour
    runtime_minutes: float | None
    until: datetime | None              # now + runtime_minutes, tz-aware
    horizon_hours: int
    computed_at: datetime
```

| State | `runtime_minutes` | `until` | Meaning |
|---|---|---|---|
| SoC ≤ min_soc | `0.0` | `now` | Battery already empty (from the reserve's perspective). |
| Drains within horizon | float, minutes | tz-aware datetime in the future | Forward-sim hit zero at this time. |
| Survives full horizon | `None` | `None` | Battery did not drain within `horizon_hours`. Stable signal; the sensor reads "unavailable". |

`avg_drain_kw_next_hour` is the arithmetic mean of `profile.at(t, local_tz)` over the first hour's worth of simulation steps. It's a stable proxy for "how fast are we draining right now" for the dashboard — flickers only when the wall-clock crosses an hour boundary, not on every cycle.

`remaining_kwh_usable` is exposed even in the SoC-at-min case so the UI can show "0.0 of 9.0 kWh usable" rather than "unknown".

---

## 6. Profile algorithm

```
window  = [r for r in buckets.records if r.local_date > today_local - window_days]
qualified[h] = [r.hour_kwh[h] for r in window if r.hour_completeness[h] >= min_hour_completeness]
distinct_days = len(window)

if distinct_days < MIN_HISTORY_DAYS:
    fallback = P20(all qualified values)  if any  else DEFAULT_STUB_KW
    return BaseLoadProfile(
        slots = [BaseLoadSlot(h, fallback, 0, is_fallback=True) for h in 0..23],
        confidence = None,
        ...
    )

fallback_kw = P20(all qualified values)
for h in 0..23:
    if len(qualified[h]) < MIN_BUCKET_DAYS:
        slots[h] = BaseLoadSlot(h, fallback_kw, len(qualified[h]), is_fallback=True)
    else:
        slots[h] = BaseLoadSlot(h, P15(qualified[h]), len(qualified[h]), is_fallback=False)

confidence = min(1.0, distinct_days / window_days)
```

**Per-hour completeness gate** — a day contributes its `hour_kwh[h]` to bucket `h` only when `hour_completeness[h] >= min_hour_completeness`. This drops hours where the inverter was offline (few derived samples), so a comms outage cannot artificially depress the floor.

**Percentile interpolation** — `_percentile(values, p)` is a linear-interpolation quantile (same convention as `numpy.percentile` with `interpolation="linear"`): rank `= p * (n - 1)`, then interpolate between the two flanking sorted values. Empty input returns `0.0`; single-element input returns that element.

**Why P15 instead of P10 or true minimum** — single-sample minimum is dominated by sensor dropouts; P10 of a 7-day bootstrap window collapses to "min of 7" and is volatile. P15 across 20–30 day-records picks roughly the 3rd-quietest day — the reliable floor — while staying robust against a single anomalously quiet day.

**Why a separate cross-bucket fallback at P20** — P15 of a thin bucket (fewer than `MIN_BUCKET_DAYS` contributions) is unreliable. We'd rather use a slightly conservative cross-bucket estimate (P20 of everything) than commit to a flaky per-bucket P15.

**Why local time** — bucketing by UTC hour would mean the same wall-clock pattern (e.g. "morning coffee at 07:00 local") falls into different buckets depending on DST and timezone. Bucketing locally is what makes the profile meaningful to a human looking at "what does my house draw at 3am?".

---

## 7. Runtime-estimate algorithm

```
usable_kwh = max(0, (status.soc - cfg.min_soc) * status.total_capacity_kwh)

if usable_kwh == 0:
    return BatteryRuntimeEstimate(0, avg, runtime_minutes=0, until=now, ...)

remaining = usable_kwh
elapsed_minutes = 0
t = now

while t < now + horizon_hours:
    drain_kw  = profile.at(t, local_tz)        # local-hour lookup
    drain_kwh = drain_kw * (SIMULATION_STEP_MINUTES / 60)

    if drain_kwh >= remaining:                 # drains mid-step
        fraction = remaining / drain_kwh
        return runtime_minutes = elapsed + STEP * fraction,
               until           = t + STEP * fraction

    remaining       -= drain_kwh
    elapsed_minutes += SIMULATION_STEP_MINUTES
    t += STEP

return runtime_minutes=None, until=None         # survived the horizon
```

A few notes:

- The step is **fixed at 5 minutes** regardless of what other modules use. Independence from the price/generation grid resolution means this works even if those streams are empty.
- The first-hour average is collected during the same loop. When the battery drains mid-step inside the first hour, the partial step is still counted toward the average so a sensor reading taken right at the cutoff still makes sense.
- A bucket with `baseload_kw == 0` (would only happen with a pathological profile) means no drain that hour — the simulation just walks forward.

**Why no schedule / no solar** — the scheduler's planned discharges and the solar forecast are both useful in different views, but they make the runtime estimate noisy and harder to reason about ("we had 8 hours yesterday, now we have 14 — did anything actually change?"). A "what if we did nothing" reserve is monotonic in inputs you can name: SoC, battery size, household pattern, hour of day. If a plan-aware "expected drain under current schedule" metric becomes valuable, it should be its own output type next to this one, not a parameter on this one.

---

## 8. Integration in the DAG

Two nodes — `BaseLoadProfileNode` in `pipeline/nodes/tier1.py`, `BatteryRuntimeNode` in `pipeline/nodes/tier2.py`:

```python
class BaseLoadProfileNode(DagNode):
    output_type = BaseLoadProfile
    consumes = [ConsumptionDailyBuckets]

class BatteryRuntimeNode(DagNode):
    output_type = BatteryRuntimeEstimate
    consumes = [BatteryStatus, BaseLoadProfile]
```

Tiers are **derived** at construction (see `ARCHITECTURE.md §4`), not declared. `BaseLoadProfileNode` lands in tier 1 because its only input is the primary `ConsumptionDailyBuckets`; `BatteryRuntimeNode` lands in tier 2 because it depends on `BaseLoadProfile` (tier-1 secondary) — `BatteryStatus` is also tier-1 secondary, produced by `BatteryStatusNode`. Both nodes read `ctx.config.local_tz`, which the coordinator populated from `hass.config.time_zone` at setup.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. On a day boundary, `inbound/consumption_daily.py` finalises yesterday's `ConsumptionDayRecord` from the day's `ObservedConsumptionSeries` and appends it to the persisted store (`STORAGE_KEY_CONSUMPTION_DAILY`), trimmed to `CONSUMPTION_DAILY_WINDOW_DAYS`.
2. `primary[ConsumptionDailyBuckets] = ConsumptionDailyBuckets(records=tuple(...))` is deposited every cycle regardless of whether step 1 fired — existing records still produce a profile.
3. `DagEngine.run(primary, ...)` → `BaseLoadProfileNode` produces `BaseLoadProfile` → `BatteryRuntimeNode` produces `BatteryRuntimeEstimate`.
4. `_build_sensor_dict` exposes them under the `"base_load_profile"` and `"battery_runtime"` keys.

Downstream consumers:

- `sensor.CurrentBaseloadSensor` — calls `profile.at(now, local_tz)` for the current numeric value; full 24-slot breakdown lives in `extra_state_attributes`.
- `sensor.BatteryRuntimeMinutesSensor` / `BatteryDrainUntilSensor` / `BaseloadConfidenceSensor` — read the obvious fields straight off the dataclass.

Nothing in the calculation/schedule pipeline consumes `BaseLoadProfile` today; it feeds `BatteryRuntimeNode` and the diagnostic sensors. The profile may eventually replace the 0.2 kW stub inside `BatteryTranslator` (`inbound/battery._DEFAULT_HOUSEHOLD_LOAD_KW`).

---

## 9. Tests

All base-load tests live in `tests/test_base_load.py` and run under pure-Python pytest (no HA harness needed). Fixtures are inline helpers at the top of the file — `_record()` / `_buckets()` build `ConsumptionDayRecord` / `ConsumptionDailyBuckets` directly (padding `hour_kwh` / `hour_completeness` to 24), plus `_battery_status()`, `_battery_config()`, `_flat_profile()`.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_base_load.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| `_percentile` | `test_percentile_empty_returns_zero` | Empty input → 0.0 (used by sparse path) |
| | `test_percentile_single_value` | Single-element input returns that element regardless of `p` |
| | `test_percentile_p15_of_ten_values` | Linear interpolation: P15 of [1..10] |
| | `test_percentile_p100_returns_max` | `p=1.0` → max |
| | `test_percentile_p0_returns_min` | `p=0.0` → min |
| Sparsity | `test_empty_buckets_returns_sparse_profile_with_stub` | Empty history → 24 fallback slots all at `DEFAULT_STUB_KW`, `confidence=None` |
| | `test_below_min_history_days_returns_sparse` | Fewer than `MIN_HISTORY_DAYS` records → sparse profile, `fallback_kw = overall P20` (not the stub, since we have data) |
| | `test_minimum_history_yields_confidence` | At `MIN_HISTORY_DAYS` qualified days → `confidence == distinct_days / window_days` |
| Bucketing | `test_per_hour_p15_floor_picks_quietest_days` | Per-hour P15 across day contributions picks the quiet-day floor |
| | `test_p15_rejects_single_ev_charging_day` | One EV-charging spike day does not lift a bucket's P15 |
| | `test_completeness_below_threshold_excludes_hour` | A day's hour with `hour_completeness < threshold` is dropped from that bucket |
| | `test_min_bucket_days_falls_back` | A bucket with `< MIN_BUCKET_DAYS` contributions is marked `is_fallback`, value equals `fallback_kw` |
| | `test_records_outside_window_excluded` | Records older than `window_days` don't affect the profile |
| | `test_profile_default_percentile_is_p15` | The default per-bucket percentile is 0.15 |
| `BaseLoadProfile.at` | `test_profile_at_uses_local_hour` | `.at(t, local_tz)` indexes by `t.astimezone(local_tz).hour` |
| Runtime estimate | `test_runtime_zero_when_soc_at_min` | SoC == min_soc → `runtime_minutes == 0`, `until == now`, `remaining_kwh_usable == 0` |
| | `test_runtime_constant_drain` | 9 kWh usable @ 1 kW → ≈ 540 min runtime, `avg_drain_kw_next_hour == 1.0` |
| | `test_runtime_partial_step_interpolation` | Small usable @ 1 kW → mid-step interpolation in the very first step |
| | `test_runtime_horizon_limits_simulation` | Large usable with short horizon → `runtime_minutes is None`, `until is None`, `horizon_hours` echoed |
| | `test_runtime_drain_follows_profile_per_hour` | Drain rate changes between local hours; total matches manual math |

### What is intentionally **not** covered here

- **Daily finalisation** — `inbound/consumption_daily.py` (building each `ConsumptionDayRecord` from `ObservedConsumptionSeries`, the completeness fractions) is covered in `tests/test_consumption_daily.py`; this file takes records as given.
- **Coordinator persistence I/O** — `STORAGE_KEY_CONSUMPTION_DAILY` load + append + trim + save is the coordinator's responsibility, exercised via `tests/test_coordinator.py`.
- **End-to-end DAG wiring** — `BaseLoadProfileNode` / `BatteryRuntimeNode` registration with the engine is exercised implicitly by `tests/test_coordinator.py`; the per-node `_compute()` is a thin wrapper over the functions tested above.
- **DST transitions** — the bucketing math works across the spring/autumn DST jump (Python's `astimezone` handles it), but no test asserts behaviour on the specific transition days. Worth adding if a regression ever surfaces; not blocking.
</content>
</invoke>
