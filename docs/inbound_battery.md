# Inbound — Battery module

Reference for `custom_components/sun_sale/inbound/battery.py`.

The battery module produces a normalised **`BatteryStatus`** snapshot that combines the user-configured battery limits with the live inverter telemetry. It is pure Python with no Home Assistant imports.

## Summary

`BatteryTranslator` reads SoC/power via the `BatterySource` seam (defaulting to `InverterBatterySource`, which wraps `InverterController`; a BMS source can supersede it per-field), grid power via the telemetry reader, and household-load from HA state (with default fallback). `build_battery_status(reading, config)` snapshots configured capacity + power limits + observed SoC into immutable `BatteryStatus` and derives `remaining_capacity_kwh = soc * total_capacity_kwh`.

- **Exposes:** `BatteryReading` (translator), `BatteryStatus` (helper).
- **Depends on:** `contract.models`, `inbound.battery_source`, `outbound.inverter`.
- **Tests:** `tests/test_battery_inbound.py` — full per-test coverage table in §8.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `BatteryStatus`](#4-output-batterystatus)
5. [Remaining-capacity formula](#5-remaining-capacity-formula)
6. [Relationship to `BatteryState`](#6-relationship-to-batterystate)
7. [Integration in the DAG](#7-integration-in-the-dag)
8. [Tests](#8-tests)

---

## 1. Responsibilities

- Read the user-configured nominal capacity and charge/discharge power limits from `BatteryConfig`.
- Read the current SoC from `BatteryReading` (the translator-produced inverter telemetry).
- Derive `remaining_capacity_kwh = soc * total_capacity_kwh`.
- Emit a single immutable `BatteryStatus` value for downstream consumers — no learning, no history.

What this module deliberately does **not** do:

- It does not consume `EstimatedCapacity`. Total capacity is the **configured nominal** value, not the learned one. The learned capacity stays on `BatteryState`, which is a separate type produced by `BatteryStateNode`.
- It does not enforce SoC bounds (`min_soc`, `max_soc`). Those are policy applied by the scheduler; this module reports the raw observed SoC.

---

## 2. Public API

```python
def build_battery_status(
    reading: BatteryReading,
    config: BatteryConfig,
) -> BatteryStatus
```

Combine live telemetry with configured limits into a `BatteryStatus`. There is no `now` parameter — the snapshot is stateless and the cycle timestamp lives on the surrounding `NodeContext`, not on the value itself.

---

## 3. Inputs

**`BatteryReading`** (`contract/models.py`) — produced by `inbound.battery.BatteryTranslator`. Carries `soc`, `power_kw`, `grid_power_kw`, and `household_load_kw`. This module reads only `soc`; the other fields are consumed elsewhere.

**`BatteryConfig`** — user-configured battery parameters, built once at coordinator setup from the config entry. This module reads `nominal_capacity_kwh`, `max_charge_power_kw`, and `max_discharge_power_kw`. Fields used by other layers (`purchase_price_eur`, `rated_cycle_life`, `min_soc`, `max_soc`, `round_trip_efficiency`, `nominal_voltage_v`) are ignored here.

---

## 4. Output: `BatteryStatus`

```python
@dataclass(frozen=True)
class BatteryStatus:
    total_capacity_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    soc: float                       # 0.0–1.0
    remaining_capacity_kwh: float    # soc * total_capacity_kwh
```

| Field | Source | Meaning |
|---|---|---|
| `total_capacity_kwh` | `BatteryConfig.nominal_capacity_kwh` | Configured nominal capacity (fixed) |
| `max_charge_power_kw` | `BatteryConfig.max_charge_power_kw` | Configured charge-power limit |
| `max_discharge_power_kw` | `BatteryConfig.max_discharge_power_kw` | Configured discharge-power limit |
| `soc` | `BatteryReading.soc` | Live state-of-charge, fractional |
| `remaining_capacity_kwh` | derived | `soc * total_capacity_kwh` |

The dataclass is frozen — the snapshot is immutable for the cycle in which it is produced.

---

## 5. Remaining-capacity formula

```
remaining_capacity_kwh = soc * total_capacity_kwh
```

No rounding is applied — the value is exact within `float` precision. Edge cases:

- `soc = 0.0` → `remaining = 0.0`.
- `soc = 1.0` → `remaining = total_capacity_kwh`.
- `soc` outside `[0, 1]` is **not clamped**; whatever the translator reports is propagated. Sanitising telemetry is the translator's responsibility, not this module's.

---

## 6. Relationship to `BatteryState`

`BatteryState` (produced by `BatteryStateNode`) and `BatteryStatus` (produced by `BatteryStatusNode`) coexist:

| Type | Capacity field | Comes from |
|---|---|---|
| `BatteryState` | `estimated_capacity_kwh` | `CapacityEstimator` — learned over time |
| `BatteryStatus` | `total_capacity_kwh` | `BatteryConfig.nominal_capacity_kwh` — fixed user config |

`BatteryState` is what the scheduler and degradation model consume (they need the *real* usable capacity). `BatteryStatus` is the user-facing snapshot exposed via `coordinator.data["battery_status"]`. They are intentionally separate — replacing one with the other would conflate "what the scheduler reasons about" with "what the user sees".

---

## 7. Integration in the DAG

`BatteryStatusNode` (`pipeline/nodes/tier1.py`):

```python
output_type = BatteryStatus
consumes = [BatteryReading]
```

Tiers are not declared — `DagEngine._assign_tiers` derives them from `consumes`. This node lands in tier 1 because its only data input is primary (`BatteryReading` from the translator). `BatteryConfig` is read from `ctx.config.battery`, which is constant for the run. The node runs in parallel with `PricingNode` and `BatteryStateNode`.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[BatteryReading]`.
2. `DagEngine.run(primary, ...)` → `BatteryStatusNode` calls `build_battery_status(reading, ctx.config.battery)` → `secondary[BatteryStatus]`.
3. `_build_sensor_dict` exposes the value under the `"battery_status"` key.

No downstream DAG node currently consumes `BatteryStatus` — it is a sink-shaped output destined for sensors and the dashboard.

---

## 8. Tests

All battery-inbound tests live in `tests/test_battery_inbound.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `default_battery_config()` provides the canonical config.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_battery_inbound.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Config passthrough | `test_total_capacity_from_config` | `total_capacity_kwh == config.nominal_capacity_kwh` |
| | `test_max_charge_power_from_config` | `max_charge_power_kw == config.max_charge_power_kw` |
| | `test_max_discharge_power_from_config` | `max_discharge_power_kw == config.max_discharge_power_kw` |
| Telemetry passthrough | `test_soc_passthrough` | `BatteryReading.soc` propagated verbatim |
| Remaining capacity | `test_remaining_capacity_at_half_soc` | `soc=0.5` → `remaining = 0.5 * total` |
| | `test_remaining_capacity_at_empty` | `soc=0.0` → `remaining = 0.0` |
| | `test_remaining_capacity_at_full` | `soc=1.0` → `remaining = total_capacity_kwh` |
| Immutability | `test_status_is_immutable` | Assigning to `status.soc` raises `FrozenInstanceError` |
| **Node wiring** | `test_battery_status_node_produces_status_from_primary` | `BatteryStatusNode.run(ctx)` reads `BatteryReading` from `ctx.primary`, calls `build_battery_status` with `ctx.config.battery`, and deposits the result at `ctx.secondary[BatteryStatus]` |

### What is intentionally **not** covered here

- **HA-side inverter telemetry parsing** (`BatteryTranslator` reading SoC from `InverterController`) — covered in `tests/test_inverter_solis.py` and translator-level tests in `tests/test_coordinator.py`.
- **Capacity learning** (`CapacityEstimator`, `EstimatedCapacity`, `BatteryState.estimated_capacity_kwh`) — covered in `tests/test_battery.py`. `BatteryStatus` deliberately ignores the learned value.
- **End-to-end DAG wiring with the full engine** (`DagEngine` orchestration of all tiers) — covered in `tests/test_coordinator.py`; the node-wiring test above runs `BatteryStatusNode` directly against a hand-built `NodeContext`.
- **SoC sanitisation** (clamping to `[0, 1]`, handling `unavailable`) — translator responsibility, not this module's.
