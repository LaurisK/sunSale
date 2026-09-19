# Inbound telemetry seam

> **Status: implemented.** This mirrors the outbound `InverterDriver` seam on the
> read side. It lives in the `inbound/telemetry/` package — `binding.py` (pure
> vocabulary), `codec.py` (pure sign/unit transforms), `reader.py` (live read +
> `resolve_bindings`) — plus the `inbound/inverter_discovery.py` registry. See
> [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system it slots into and
> [`control_loop.md`](control_loop.md) / [`solis_control.md`](solis_control.md)
> for the outbound side. The code sketches below match the shipped API; minor
> deviations are called out inline.

The outbound control path is platform-neutral behind `outbound/driver.py`
(`InverterDriver` / `InverterControlDriver`). The **inbound** (telemetry /
observer) path now matches it: vendor sign and unit conventions live in one
codec, every live and historical read flows through it, and entity discovery is
a pluggable registry. A second inverter vendor is a closed, reviewable checklist
rather than a per-file porting exercise. This document records the design and
the file-by-file outcome.

## The problem this solved

Three things *used to be* Solis-coupled on the read side, none behind an
abstraction (all now resolved — see [What was built](#what-was-built)):

1. **Sign / unit transforms are duplicated.** Every telemetry signal is read
   **twice** — once live per-cycle (the translators in `inbound/observer/*` and
   the getters in `outbound/inverter.py`: `get_battery_power`, `get_grid_power`,
   `PvPowerTranslator.parse`, `AcPortPowerTranslator`, …) and once historically
   by `inbound/observer/recorder_resample.py`, which replays the HA recorder's
   state changes and **re-derives the same readings** (`_battery_signed_kw`,
   `_grid_net_kw`, `_pv_power_w`, …). The two implementations must agree
   bit-for-bit; `tests/test_recorder_resample.py` exists only to pin them
   together.

2. **Platform identity is a boolean flag.** The resampler selects vendor
   behaviour with `is_solis=...` threaded through every transform, plus
   `is_signed_role` / `is_fallback` / `has_directional`. Adding a vendor means
   adding branches, not an implementation.

3. **Entity resolution is Solis-first.** `inbound/inverter_entity_resolver.py`
   is already a "front door" but hard-delegates per-role discovery to
   `inbound/solis_entity_resolver.py`.

Battery telemetry is *already* solved the right way:
`inbound/battery_source.py` defines a `BatterySource` Protocol with
`InverterBatterySource` / `JkBmsBatterySource` / `ChainedBatterySource`. The
seam below is the same move for the remaining inbound signals, and it reuses
`BatterySource` unchanged.

## Canonical signal vocabulary

The seam's contract is a fixed set of physical quantities expressed in
**sunSale-canonical** units and signs. The platform adapter's only job is to map
raw vendor entities onto these. Canonical conventions match the rest of the
codebase (see `CLAUDE.md` → *Derived-power observers*):

| Signal | Canonical unit / sign | Today's Solis transform |
|---|---|---|
| `PV_POWER` | W, ≥ 0 | unit-normalise, clamp ≥ 0 |
| `BATTERY_POWER` | kW, **+ = charging** | flip the signed-net role (Solis is discharge-positive) |
| `GRID_NET_POWER` | kW, **+ = import** | flip the ac-port fallback (Solis is inverter→grid-positive) |
| `GRID_IMPORT` / `GRID_EXPORT` | kW, ≥ 0 directional | clamp ≥ 0; or split net per-sample (`import=max(0,net)`, `export=max(0,−net)`) |
| `AC_PORT_POWER` | kW, **+ = inverter→grid** (raw) | unit-normalise (consumed by `derived.py`, which flips) |
| `BACKUP_POWER` | kW magnitude | unit-normalise |
| `GENERATION_DAILY` / `GRID_IMPORT_DAILY` / `GRID_EXPORT_DAILY` | kWh, daily-resetting | unit-normalise |
| `BATTERY_SOC` | 0.0–1.0 fraction | percentage normalise |
| `BATTERY_CHARGE/DISCHARGE_ENERGY_TODAY` | kWh | (stays in `BatterySource`) |
| `YESTERDAY_TOTAL_*` | kWh | entity read |

## The seam: three parts

The seam mirrors the two-tier shape of `outbound/driver.py` — a pure transform
layer plus a live-read layer — with a small resolution type in front.

### 1. `SignalBinding` — resolution output (replaces the role flags)

```python
# inbound/telemetry/binding.py  (pure: no Home Assistant imports)
from dataclasses import dataclass
from enum import Enum, auto


class TelemetrySignal(Enum):
    PV_POWER = auto()
    BATTERY_POWER = auto()
    GRID_NET_POWER = auto()
    GRID_IMPORT = auto()
    GRID_EXPORT = auto()
    AC_PORT_POWER = auto()
    BACKUP_POWER = auto()
    BATTERY_SOC = auto()
    GENERATION_DAILY = auto()
    GRID_IMPORT_DAILY = auto()
    GRID_EXPORT_DAILY = auto()
    # Non-scalar signals (the inverter clock, mode-state registers) are parsed
    # elsewhere and intentionally absent — the codec handles scalar transforms.


class SignalRole(Enum):
    PRIMARY = auto()      # the canonical sensor for this signal
    SIGNED_NET = auto()   # a net sensor projected onto one side (battery / grid)
    FALLBACK = auto()     # a secondary derivation (e.g. ac-port → grid net)


@dataclass(frozen=True)
class SignalBinding:
    """One resolved (signal → entity) mapping plus how to interpret it."""
    signal: TelemetrySignal
    entity_id: str
    role: SignalRole = SignalRole.PRIMARY
```

`resolve_bindings` (in `reader.py`) returns
`dict[TelemetrySignal, tuple[SignalBinding, ...]]` — an ordered chain per signal
(see the reader section). The `is_signed_role` / `is_fallback` /
`has_directional` booleans the resampler used to thread collapse into the
binding's `role` — **decided once** at resolution, never re-decided inside a
transform.

### 2. `TelemetryCodec` — the pure transform layer (deletes `is_solis`)

The single source of truth for vendor sign / unit maths. **No HA, no state**, so
the live reader *and* the recorder resampler call the exact same code — over a
live `State` value or a historical recorder row, indifferently.

A shared `_BaseTelemetryCodec` owns all the unit/clamp/split logic; vendor
polarity is two sign **factors** a subclass overrides (`SolisCodec` sets both to
`-1.0`, `GenericCodec` leaves them `+1.0`). So the only platform difference is a
sign — the transform body is written once.

```python
# inbound/telemetry/codec.py  (pure)
from typing import Protocol, runtime_checkable

from ...ha_state import normalize_energy_to_kwh, normalize_power_to_kw
from .binding import SignalBinding, SignalRole, TelemetrySignal


@runtime_checkable
class TelemetryCodec(Protocol):
    def to_canonical(self, binding: SignalBinding, raw: float, unit: str) -> float:
        """Map a raw vendor reading to the signal's canonical unit / sign."""
        ...


class _BaseTelemetryCodec:
    _signed_battery_factor: float = 1.0   # ×−1 on a discharge-positive vendor
    _fallback_grid_factor: float = 1.0    # ×−1 on an inverter→grid-positive fallback

    def to_canonical(self, binding, raw, unit):
        signal, role = binding.signal, binding.role
        if signal is TelemetrySignal.PV_POWER:
            return _pv_watts(raw, unit)                       # W, clamp ≥ 0
        if signal is TelemetrySignal.BACKUP_POWER:
            return max(0.0, normalize_power_to_kw(raw, unit))
        # … SoC, daily energy, ac-port (sign preserved) …
        kw = normalize_power_to_kw(raw, unit)
        if signal is TelemetrySignal.BATTERY_POWER:
            return kw * self._signed_battery_factor if role is SignalRole.SIGNED_NET else kw
        if signal is TelemetrySignal.GRID_NET_POWER:
            return kw * self._fallback_grid_factor if role is SignalRole.FALLBACK else kw
        if signal is TelemetrySignal.GRID_IMPORT:
            return max(0.0, kw)
        if signal is TelemetrySignal.GRID_EXPORT:
            return max(0.0, -kw) if role is SignalRole.SIGNED_NET else max(0.0, kw)
        raise ValueError(f"codec has no transform for {signal!r}")


class GenericCodec(_BaseTelemetryCodec): ...               # sensors already sunSale-convention
class SolisCodec(_BaseTelemetryCodec):
    _signed_battery_factor = -1.0
    _fallback_grid_factor = -1.0


def signed_polarity(codec, signal, role) -> int:
    """Probe the codec with +1 kW → ±1, for consumers that need just the sign
    (the panel live-flow spec). Flip-only roles only."""
    return -1 if codec.to_canonical(SignalBinding(signal, "", role), 1.0, "kW") < 0 else 1
```

The resampler's `_battery_signed_kw` / `_grid_net_kw` / `_pv_power_w` were
**deleted** and the live translators rewritten to delegate here; the `is_solis`
parameter is gone everywhere. Platform identity is now *which codec instance you
hold* — selected once by `InverterController` and threaded from there.

### 3. `TelemetryReader` — the live per-cycle surface

Live reads, canonical values, `None` on unavailability. This is what the
controller getters and the observer translators call instead of reading entities
and flipping signs themselves. Two shipped refinements over the original sketch:

- **`hass` is passed per call**, so one reader design serves both the hass-bound
  `InverterController` and the per-cycle DAG translators.
- **Each signal maps to an *ordered chain* of bindings**, and the read is
  **freshness-aware** (`max_age_s`, mirroring `ha_state.read_power_kw`). The
  chain encodes the signal's sourcing semantics: a **runtime fallback** for
  battery / grid-net (try preferred role, fall through on unavailability — what
  the controller getters did) versus a **single config-pick** for directional
  grid (read only the configured entity, no runtime substitution — what
  `_DirectionalPowerObserver` did).

```python
# inbound/telemetry/reader.py
@runtime_checkable
class TelemetryReader(Protocol):
    def read(self, hass, signal, *, now=None, max_age_s=float("inf")) -> float | None: ...


class HaTelemetryReader:
    def __init__(self, bindings: dict[TelemetrySignal, tuple[SignalBinding, ...]],
                 codec: TelemetryCodec) -> None:
        self._bindings = bindings
        self._codec = codec

    def read(self, hass, signal, *, now=None, max_age_s=float("inf")):
        for binding in self._bindings.get(signal, ()):
            value, unit = _read_raw(hass, binding.entity_id, now=now, max_age_s=max_age_s)
            if value is not None:
                return self._codec.to_canonical(binding, value, unit)
        return None
```

`resolve_bindings(entity_ids, *, pv_power="", ac_port_power="", …)` builds the
chains, encoding each signal's fallback-vs-config-pick rule in one place. It is
the single binding-construction authority — the controller, the observer
translators, and (the resampler's equivalent) all source their chains from it.

## What was built

Shipped incrementally; each row was an independently-reviewable change.

| File | Outcome |
|---|---|
| `inbound/telemetry/{binding,codec,reader}.py` | **new** — the vocabulary, the pure codec (+ `signed_polarity`), and the live reader + `resolve_bindings`. |
| `observer/recorder_resample.py` | `is_solis` and the `_battery_signed_kw` / `_grid_net_kw` / `_pv_power_w` transforms **deleted**; builders take a `codec` and route every transform through it. |
| `outbound/inverter.py` `get_battery_power` / `get_grid_power` | two lines each — `self._reader.read(self._hass, …)`; the inline `platform == SOLIS` flips and the `_read_power_kw*` helpers **deleted**. `InverterController` owns the single codec selection and exposes it via `.codec`. |
| `observer/generation.py`, `observer/grid.py`, `observer/derived.py` translators | read via `HaTelemetryReader` (freshness-aware); the inline `value*1000` / `max(0, …)` / `_signed_polarity` transforms **deleted**. |
| `orchestration/coordinator.py` | reuses `inverter.codec` for the live-flow panel spec (`sign` via `signed_polarity`) and threads it into the resampler; the `is_solis` / `sign=-1 if solis` literals **deleted**. |
| `inbound/inverter_discovery.py` | **new** — `InverterEntityDiscovery` Protocol + `discovery_for` / `register_discovery` registry + `GenericEntityDiscovery`. |
| `inverter_entity_resolver.py` / `solis_entity_resolver.py` | front door is **vendor-neutral** — delegates role discovery to `discovery_for(platform)`; the Solis role mapping moved into `SolisEntityDiscovery`. |
| `battery_source.py` | **unchanged** — already the seam for SoC / power / energy. |

## What a new vendor implements

Exactly these, all registered — **no edits to any shared module**:

1. A `_BaseTelemetryCodec` subclass — its two sign factors (or reuse `GenericCodec`
   when sensors are already in sunSale convention).
2. `resolve_bindings` coverage for its entities (the battery / grid-net /
   observer chains).
3. An `InverterEntityDiscovery` strategy + one `register_discovery(platform, …)`
   call for entity discovery.
4. *Optionally* a `BatterySource` if a BMS is in play (pluggable via
   `ChainedBatterySource`), and an `InverterDriver` for write/dispatch.

No observer, resampler, reader, DAG, sensor, or integration-check code changes —
those layers speak `TelemetrySignal`, never a vendor entity or polarity.

## Why this closes the confidence gap

- **One transform, no copies.** The duplicated live/historical transforms are
  gone; there is a single `Codec` implementation, so live and historical reads
  *cannot* diverge. The class of bug that makes a new vendor "mostly work but
  drift on the history chart" is removed structurally, not held off by test
  vigilance. `tests/test_telemetry_codec.py` pins the one transform;
  `tests/test_telemetry_reader.py` pins the chain/freshness semantics.
- **Symmetry with outbound.** A vendor is a closed tuple —
  `{ Driver, Codec, bindings, Discovery, BatterySource? }`, every element
  registered — which is the reviewable checklist that "high confidence of
  working as expected" actually requires. The platform now enters the system at
  exactly two pluggable points: the codec selection in `InverterController` and
  the discovery registry.

## Scope boundaries (deliberately out)

- **Battery telemetry** stays in `BatterySource`; the reader merely becomes its
  backing read. No change to that Protocol.
- **Mode decode** (`decode_observed`, `storage_mode_specs.decode_mode`) stays
  outbound — it is control state, not telemetry.
- **Mode capability** — whether a given vendor *can* force grid export at a
  setpoint (the `StorageMode.Discharge` question) is independent of this seam.
  This seam makes the *reads* portable; the dispatchable-mode set is a separate
  conversation owned by the outbound driver.
