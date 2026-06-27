# Solis inverter control — research & target design

Reference for redesigning `outbound/inverter.py` away from per-cycle TOU-slot
rewrites and toward a register-level state machine.

Target inverter on this deployment: **Solis S6 3-Phase LV Hybrid (10–15 kW)**,
type 8562, exposed in Home Assistant under the `namai_inv_*` entity prefix
through the `solis_modbus` integration.

---

## 1. Why the current implementation should be discarded

`InverterController._async_dispatch_solis` writes the inverter's TOU slot times,
slot currents, and TOU-mode switch every dispatch cycle. Three problems:

1. **Flash wear.** The TOU `time.*_slot_*` entities write to inverter flash;
   continuous rewrites risk bricking the inverter.
2. **Coarse granularity.** Slots are written aligned to 5-minute boundaries with
   ≥1 h windows — too coarse for sub-hour Nordpool 15-min decisions.
3. **It isn't actually working.** Observed in the live system:
   `sensor.sunsale_inverter_mode` flips ~30×/day, but register `43110`
   (Storage Mode) changed only 3 times in 3 days. The decision engine produces
   a plan; almost none of it reaches the inverter.

Root cause of (3) is `automation_enabled: false` in the debug payload, but even
once enabled the TOU rewrite is the wrong control surface.

---

## 2. Control surface to use

Solis hybrids are controlled through four register groups. Every operating
state is a *composition* of writes across these — never one register alone.

| Group | Modbus reg (S6) | HA entity on this deployment | Role |
|---|---|---|---|
| **A. Storage Mode word** (bitmask) | **43110** | read: `sensor.namai_inv_storage_control_switch_value_2`<br>write via bit switches below | Operating philosophy |
| **B. Backflow / export limit** | 43073 enable + 43074 limit; 43071 absolute cap | `number.namai_inv_backflow_power_2` (10000 W)<br>`number.namai_inv_peak_max_usable_grid_power_2` (3000 W)<br>`switch.grid_feed_in_power_limit_switch` | Caps export power |
| **C. Battery current limits** | 43117/43118 (slot), max-current regs | `number.namai_inv_battery_max_charge_current_2` (290 A)<br>`number.namai_inv_battery_max_discharge_current_2` (290 A)<br>`number.namai_inv_max_charge_current_2` / `_discharge_current_2` (999 A) | Bounds the energy rate |
| **D. Remote-Control AC active power setpoint** | **43128** (S16 signed, ×10 W) + 43132 function selector + 43282 timeout | `number.namai_inv_rc_inverter_ac_grid_active_power_2`<br>`select.rc_grid_adjustment`<br>`number.namai_inv_rc_timeout_2` | Real-time setpoint; RAM-only, no flash wear. *(43141 is Time-Charging Charge Current — an earlier revision of this doc mislabelled it as the RC setpoint.)* |
| **E. Force charge/discharge** (alternative to D) | 43135 mode + 43136 rate | not currently exposed in this build | Discrete force mode |
| **F. Inverter on/off (standby)** | 43006 / 43007 | not exposed; closest is self-use + RC=0 + currents=0 | Hard standby |

### Bits of register 43110

| Bit | Value | Function | HA switch |
|---|---:|---|---|
| 0 | 1 | Self-Use | `switch.self_use_mode` |
| 1 | 2 | Time-of-Use | `switch.time_of_use` |
| 2 | 4 | Off-Grid | — |
| 3 | 8 | Battery Wake-Up | — |
| 4 | 16 | Reserved | — |
| 5 | 32 | Allow Grid Charge | `switch.allow_grid_to_charge_the_battery` |
| 6 | 64 | Feed-in Priority | `switch.feed_in_priority_mode` |

Additional gates that are *not* part of 43110 but participate in dispatch:

- `switch.allow_export_switch_under_self_generation_and_self_use` — export master
- `switch.grid_feed_in_power_limit_switch` — export limit enforcement
- `sensor.namai_inv_grid_feed_in_switch_value_2` — readback of feed-in switch word

---

## 3. Target operating states

Each state is defined by a `(43110 value, export limit, charge_A, discharge_A,
RC setpoint W)` tuple. `I_max` = max safe battery current; `P_export_max_W` =
configured backflow limit.

| Name | Intent | 43110 | Export limit (B) | Charge A | Discharge A | RC setpoint (D) |
|---|---|---:|---|---|---|---|
| **SELL** | Export-priority for surplus PV (above cap → charge); battery still covers household load | 64 (FeedIn) | switch ON, limit = `P_export_max_W` | `I_max` | `I_max` | 0 |
| **STORE** | Self-use: battery balances solar vs load; surplus → capped export | 1 (SelfUse) | switch ON, limit = `P_export_max_W` | `I_max` | `I_max` | 0 |
| **HOARD** | Self-use with export prohibited | 1 (SelfUse) | switch ON, limit = 0 (or `allow_export_*` = off) | `I_max` | `I_max` | 0 |
| **DUMP** | Export at deployment cap + actively discharge battery | 64 (FeedIn) | switch ON, limit = `P_export_max_W` (written explicitly — `None` would inherit a 0 cap from a prior HOARD/GULP/STBY) | 0 | `I_max` | `+P_inv_max_W` *(or 43135=2, 43136=P/10)* |
| **GULP** | Charge battery from grid at max (cheap-hour absorb) | 33 (SelfUse + GridCharge) | switch ON, limit = 0 | `I_max` | 0 | `-P_charge_max_W` *(or 43135=1, 43136=P/10)* |
| **STBY** | Battery hold — discharge barred (preserve charge), still soaks surplus solar; no export | 1 (SelfUse) | switch ON, limit = 0 | `I_max` | 0 | 0 |
| **TRACK** | Real-time VPP setpoint follower (FCR / spot-price tick) | 1 (SelfUse) | at limits | `I_max` | `I_max` | per-cycle `P_setpoint_W` (+ = export, − = charge), clamped by C and inverter rating |

### Caveats

- **D (43128) only acts while the RC Grid Adjustment selector (43132) is
  engaged** — `select.rc_grid_adjustment`: 0 = OFF, 1 = System Grid
  Connection Point, 2 = Inverter AC Grid Port. sunSale engages option 2 for
  RC-backed modes and releases to OFF otherwise.
- **The RC function expires inverter-side** when no RC write arrives within
  the RC timeout (43282, 1–30 min; firmware default ~5 min). The timeout
  only latches when written *after* the function is enabled
  (Pho3niX90/solis_modbus#352). sunSale writes selector → timeout (30 min) →
  setpoint on engagement and refreshes timeout + setpoint every coordinator
  tick while the mode holds — the timeout doubles as a deadman: if sunSale
  stops dispatching, the inverter falls back to its base 43110 mode within
  30 min.
- **Never combine D (43128) and E (43135) simultaneously** — 43135 non-zero
  overrides 43128.
- **Drop the TOU `time.*_slot_*` writes entirely.** They become unused in this
  model. Keep the bit switches (`self_use_mode`, `allow_grid_to_charge_the_battery`,
  `feed_in_priority_mode`) since they collectively *are* the bits of 43110.

---

## 4. Observed register 43110 behaviour — last 7 days

Window: **2026-05-21 22:23 UTC → 2026-05-28 22:23 UTC**.
Source: `sensor.namai_inv_storage_control_switch_value_2`.

### Timeline

| UTC time | Value | Bits | Meaning |
|---|---:|---|---|
| 2026-05-21 19:23 | 1 | SelfUse | Self-Use only |
| 2026-05-22 04:30 | — | — | unavailable (brief) |
| 2026-05-22 05:00:57 | 96 | GridCharge \| FeedIn | Feed-in Priority + grid-charge |
| 2026-05-22 08:31:02 | 33 | SelfUse \| GridCharge | Self-Use + grid-charge |
| 2026-05-22 09:00:57 | 96 | GridCharge \| FeedIn | Feed-in Priority + grid-charge |
| 2026-05-22 09:40:57 | 33 | SelfUse \| GridCharge | Self-Use + grid-charge |
| 2026-05-22 15:30:57 | 1 | SelfUse | Self-Use only |
| 2026-05-23 05:11:01 | 33 | SelfUse \| GridCharge | Self-Use + grid-charge |
| 2026-05-23 05:51:01 | 1 | SelfUse | Self-Use only |
| 2026-05-23 18:39 | — | — | unavailable (~2 min) |
| 2026-05-23 18:42:07 | 1 | SelfUse | Self-Use only |
| 2026-05-26 04:00:02 | 96 | GridCharge \| FeedIn | Feed-in Priority + grid-charge |
| 2026-05-26 07:50:07 | 33 | SelfUse \| GridCharge | Self-Use + grid-charge |
| 2026-05-26 10:50:02 | 1 | SelfUse | Self-Use only |
| held since | 1 | SelfUse | Self-Use only — current state |

### Findings

1. **Only four distinct register values appeared all week**: `1`, `33`, `96`,
   plus brief `unavailable` gaps. `35`, `64`, `3`, etc. were never seen.
2. **TOU bit (2) was never set.** `switch.time_of_use` stayed `off` for the
   entire week — confirming sunSale's TOU-slot rewrites are not engaging the
   TOU mode bit.
3. **Two active periods only**: 2026-05-22 (5 transitions) and
   2026-05-26 04:00–10:50 (3 transitions). 2026-05-24/25 and 2026-05-27/28 had
   zero register writes — the inverter sat in plain `Self-Use=1` for ~3.5 days
   untouched, while `sensor.sunsale_inverter_mode` flipped ~30×/day.
4. **Decision vs. action gap is the dominant finding**: 33 sunSale decisions
   in 3 days vs. 3 actual register changes in the same window. Matches
   `automation_enabled: false` in the debug payload.
5. **`_switch_value_2` vs `_switching_value_2`** show small offsets (seconds
   to ~2 min). One is readback, the other the commanded value — useful for
   verifying writes landed. Use `_switch_value_2` as source of truth for
   *what the inverter is doing*.

---

## 5. Reproduction

Helper scripts used to generate the data in this document:

- `/tmp/sunsale_register_history.py` — 3-day dump of TOU slot times, switches,
  current limits, RC setpoint, and `sunsale_inverter_mode`.
- `/tmp/sunsale_43110_history.py` — 7-day dump of register 43110 + its bit
  switches, with bit decoding.

Both query the HA REST API at `<HA_URL>/api/history/period/...` using the
credentials resolved by `tools/checks/credentials.py` (CLI arg, the `HA_URL` /
`HA_TOKEN` environment variables, or a local `tools/secrets.json` — see
`tools/secrets.example.json`).

---

## 6. Next step

Refactor `InverterController` to expose a single primitive:

```python
def apply_mode(
    mode: StorageMode,            # SELL | STORE | HOARD | DUMP | GULP | STBY | TRACK
    export_limit_w: int,
    charge_a: float,
    discharge_a: float,
    rc_setpoint_w: int = 0,
) -> None: ...
```

`StorageMode` resolves to a `43110` bitmask via the table in §3. The dispatcher
composes the writes (bit switches → export limit → currents → RC setpoint),
verifies via `_switch_value_2` readback, and skips writes when the readback
already matches the target (idempotency = fewer Modbus transactions and zero
flash wear when nothing changes).
