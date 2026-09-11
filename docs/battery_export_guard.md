# Battery-export guard — evidence, detector, monitoring plan

Status: **Phase 1 (monitor only) implemented in v0.5.1** (2026-09-11) —
`outbound/export_guard.py`, wired in the coordinator. Phase 2 (act) is not.
Companion to [`control_loop.md`](control_loop.md).

## 1. The incident this guards against

Reference install, 2026-09-09 (local time, UTC+3). sunSale ran three
Remote-Dispatch Discharge bursts (20:30–21:15, 21:30–21:45, 22:00–22:05) at a
sell price of 0.16–0.20 EUR/kWh — a correct planner decision.

| Time | sunSale commanded | Inverter (observed) |
|---|---|---|
| 21:15, 21:45 | release dispatch → `feed_in` | operating mode (33122) 2048 → **8**, export stops (grid ≈ 0) |
| 22:05:05 | release dispatch → `feed_in` | operating mode 2048 → **4096**, export **continues** |
| 22:05–22:25 | `feed_in` (holding, verify `ok`) | grid −10.0 kW (= backflow cap), battery ~7.8 kW (BMS-A view) |
| 22:25–22:45 | `feed_in` → `self_use` at 22:45 | grid −5 kW, then −2.5 kW; tracks BMS discharge limit 224 → 112 → 56 A |
| 23:36:29 | `self_use` | SoC 3 %, operating mode → **2**, export stops |

SoC fell 19 % → 3 %: through sunSale's `min_soc` (6 %) **and** the inverter's
own Over-discharge SOC (43011 = 5 %). About 7.2 kWh left the battery to the
grid after sunSale had stopped asking for it.

### Why nothing caught it

* The verify / reconcile loop compares **what sunSale wrote** (43110, currents,
  backflow, dispatch block) against readbacks. Every one of those matched —
  the fault was in behaviour, not in a register.
* Remote Dispatch was genuinely off: `remote_dispatch_running_status` (34504)
  read 0 at 22:08:59 and stayed 0. So this is **not** a dispatch that failed to
  release, and the dispatch SOC window (44109) would not have stopped it.
* `min_soc` exists only in the planner. In passive modes the inverter enforces
  its own Over-discharge SOC — which the 4096 state ignored.

## 2. Operating mode 4096 — what is known

Register 33122 over 2026-09-01 … 09-11 (all recorder history):

| Value | Count | Seen when |
|---|---|---|
| 2 | 17 | 43110 = 1 (Self-use) |
| 8 | 21 | 43110 = 64 (Feed-in priority) |
| 2048 | 21 | Remote Dispatch running |
| **4096** | **1** | 09-09 22:05:08 → 23:36:29 only |

* The public Solis protocol (RS485_MODBUS ESINV-33000ID, 2020-09-15) lists
  33122 as **Reserved**; `solis_modbus` labels it "Operating Mode" with no
  value table. The documented status word is 33121 (Appendix 6), which stayed
  `1` (Normal operation) throughout.
* Nothing else in the status/BMS entity set changed at 22:05 except the BMS
  charge/discharge limit (397.6 → 280 A) — a transition that happens daily
  without 4096, so not a trigger on its own.
* The release sequence (`solis_dispatch_stop`: 44105 = 1, 44108 = 1,
  44100 = 0) was identical at 21:15, 21:45 and 22:05; only the last one
  landed in 4096.

Operator note (2026-09-11): the same behaviour has very likely happened at
least once before, outside the recorder's 10-day window — rare, but
recurring. Treat it as intermittent, not a one-off.

**To identify it (open):**
1. SolisCloud → device history for 09-09 22:00–23:40: the "work mode"
   shown there, and whether any cloud command was issued.
2. Solis support ticket: serial, firmware, "33122 = 0x1000 for 91 min after
   Remote Dispatch stop, battery discharging to grid at BMS limit".
3. On recurrence (the detector below fires), dump raw registers with
   `solis_modbus.solis_read_register`: 33121–33125, 34504, 43110, 43135,
   44100–44112 — before any corrective write, so the state is captured.

## 3. Detector

**Rule:** a *passive* mode is commanded (`self_use` / `no_export` /
`stand_by` / `feed_in`) **and** Remote Dispatch is not active **and**
`grid_export_kw − pv_kw ≥ 1.0` continuously for **≥ 120 s**.

In words: more power is leaving to the grid than the panels produce, so the
battery is feeding the grid — which no passive mode ever asks for.

Inputs (all already resolved roles): `grid_power` (signed, + = import),
`pv_power`, `dispatch_active`, plus the control module's
`last_commanded_mode`. Evaluated on every relevant state change (the same
listener that drives the live mode readout), not only on the 5-min tick.

**Why grid-based, not battery-based.** The battery power / current registers
only see BMS-A (see the second-pack findings). A battery-based rule
(`min(battery_discharge, export) ≥ 1 kW`) caught the first 42 min (4.0 kWh)
but went blind to the 2.5 kW tail, where the visible pack showed 0.9 kW.
"Export not covered by PV" is independent of which pack is visible.

**Why 120 s persistence.** Without it, every dispatch→passive transition
produces a single-sample blip (readback lag while dispatch is still
winding down).

### Replay proof (2026-09-01 09:05 → 09-11 14:08, 233 h of passive-mode time)

| Variant | Persistence | Episodes | Of which the incident |
|---|---|---|---|
| battery→grid ≥ 1 kW | 0 s | 5 | 1 (misses tail) + 4 transition blips |
| **export − PV ≥ 1 kW** | 0 s | 5 | 1 (full) + 4 transition blips |
| battery→grid ≥ 1 kW | 120 s | 1 | onset 22:05:12, fires 22:07:12, until 22:44 |
| **export − PV ≥ 1 kW** | **120 s** | **1** | onset 22:05:12, **fires 22:07:12**, until 23:36 (7.2 kWh) |
| export − PV ≥ 0.5 kW | 120 s | 1 | same |

Headroom: outside the incident, `export − PV` during passive modes never
exceeded **0.02 kW** (one 10 kW single-sample transition blip on 09-08 01:00,
removed by persistence). The 1 kW threshold sits ~50× above normal operation.

The replay was an ad-hoc script over HA recorder history (REST
`/api/history/period`, 10 s forward-filled grid); it is not kept in the repo —
the Phase 1 `tools/checks` deep-check below is its permanent form.

## 4. Companion rule — hard floor at `min_soc`

**Rule:** SoC ≤ `min_soc` **and** a passive mode is commanded **and** the
battery is discharging → command StandBy (discharge current 0 A) until SoC
recovers above `min_soc` + hysteresis.

Recorder history, SoC < 6 % (`min_soc`): 09-04 03:27–10:05 (min 5 %, battery
*not* discharging — the inverter's own 5 % floor held) and the 09-09 incident
(min 3 %, ~1.35 kWh still discharged in the BMS-A view). It would have acted
only on the incident.

## 5. Rollout

### Phase 1 — monitor only (no writes)

* State machine `idle → suspect (condition true) → tripped (≥ 120 s)`,
  held on the control module.
* Exposed on `sensor.sunsale_observed_inverter_mode` attributes and in
  `debug_view`: `export_guard_state`, `export_guard_onset`,
  `export_guard_kwh`, `export_guard_operating_mode` (33122 raw),
  `export_guard_soc_at_onset`.
* On `tripped`: log WARNING with the register snapshot above, and raise an HA
  persistent notification ("Battery is exporting to grid while sunSale
  commands <mode>").
* Also log (not act on) the §4 floor rule.
* `tools/checks`: a deep-check replaying the rule over the snapshot's
  histories and cross-checking the exposed state.

Exit criterion: no false trips over ≥ 2 weeks, and — if 4096 recurs — the
register capture from §2.

### Phase 2 — act

Escalation on `tripped`, each step verified by the detector clearing:

1. Re-issue `solis_dispatch_stop`, then force-re-command the held passive mode.
2. Still tripped after one more persistence window → StandBy (0 A discharge).
3. SoC ≤ `min_soc` at any point → StandBy immediately (§4).

Unknown until observed: whether the 4096 state honours a 0 A discharge limit.
Phase 1's capture answers that; if it does not, step 2 becomes a user alert
only.

## 6. Limits

* Detection latency: 120 s persistence + the grid sensor's update cadence
  (~10 s here). The incident would have been flagged after ~0.3 kWh instead
  of 7.2 kWh.
* Does not cover battery → *house* drain below `min_soc` in passive modes;
  that is §4's job.
* Assumes `pv_power` is live; with PV unavailable the rule degrades to
  "export ≥ 1 kW", which is still invalid in any passive mode at night but
  could false-trip on a daytime PV-sensor outage — the guard should skip when
  `pv_power` is unavailable.
