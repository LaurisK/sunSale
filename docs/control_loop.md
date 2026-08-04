# Control loop & mode-override contract

How a `Schedule` slot (or an operator override) becomes a verified set of Modbus writes.
This is the authoritative contract for `outbound/inverter_control_module.py`. The
register-level details of the Solis writes themselves live in
[`solis_control.md`](solis_control.md); the surrounding pipeline is in
[`ARCHITECTURE.md`](ARCHITECTURE.md) and [`MODULES.md`](MODULES.md).

> **Capability seam (v0.2.0).** `driver.spec_for(mode)` returns an *effective*
> spec — every target already reduced to what the hardware advertises it will
> accept — so the value written and the value verified are the same number. Rows
> carry a four-state `status` (`match` / `mismatch` / `unknown` / `no_target`),
> and only `mismatch` counts as drift: an unreadable register is a comms gap, not
> a control fault. Before this, a spec target above an upstream bound could never
> verify, and an unavailable entity pinned the drift check True indefinitely. See
> [`capability_seam.md`](capability_seam.md).

## Mode override semantics

`select.sunsale_mode_override` is the single source of operator mode intent. Options are the
dispatchable `StorageMode` values plus the sentinel `sunsale` (rendered as **"sunSale"** in the
panel UI via an option-label map in `sun-sale-panel.js`; entity option strings stay snake_case
for HA).

Three rules govern dispatch in `InverterControlModule.tick`:

- **Write once, on change.** `apply_mode` runs only on the cycle the resolved target *differs*
  from `last_commanded_mode` — a button press, or a schedule-slot boundary. Holding an
  unchanged, engaged target re-asserts nothing: `_dispatch_current_slot` returns the `holding`
  outcome and issues no Modbus write. The mode is set once and then observed; a freshly-failed
  write is recovered by the verify loop (below). A *drift* that appears after the verify window
  has closed is recovered by the slow reconciliation path (further below) — not by blind
  re-asserts every cycle.
  *One deliberate exception:* RC-backed modes (non-zero `rc_setpoint_w`, i.e. GridCharge /
  Discharge) refresh the RAM-only RC registers on a dedicated 3-minute heartbeat
  (`_on_rc_keepalive_tick` → `InverterController.refresh_rc`, armed by `_force_write_and_verify`)
  plus every holding tick as a secondary re-arm (`_maybe_refresh_rc`), because the inverter
  expires the RC function within minutes of the last engaging write (register 43282's 30-min
  request does not latch on S6 firmware). The flash-wear rationale doesn't apply to the RC group,
  and the rolling timeout doubles as a deadman — if sunSale stops dispatching, the inverter falls
  back to its base 43110 mode. The 43110 bitmask itself is still write-once.
  **The keep-alive is deliberately not gated on `verify_state`.** It must keep running exactly
  when the loop is least sure of itself: `unknown` means some *unrelated* control point is
  unreadable (a solis_modbus register-group outage takes the battery-current entities down while
  the RC registers answer fine), and `mismatch` is often the RC selector itself having reverted —
  the very drop the re-arm repairs. Gating it on `ok` is what let a commanded Discharge expire a
  few minutes in while every register still read "engaged".
- **Override bypasses `automation_enabled`.** When `coordinator.mode_override` is set, the
  dispatcher resolves the override as the target regardless of the automation switch. Operator
  intent always reaches the inverter (once, on the cycle it changes).
- **`automation_enabled` only gates the scheduler path.** When the override is `None`, the
  dispatcher only writes the current schedule slot's mode while the switch is on; with it off,
  the module is observer-only.

**A mode is a sequence of writes, and no single one may abort it.** Every write in `apply_mode`
goes through `EntityActuator` (`outbound/entity_control.py`), which owns two guarantees the
sequence depends on. First, a `number` target is **clamped into the entity's advertised
`min`/`max`** before the readback comparison — the integration on the other side owns those
bounds and changes them under us (solis_modbus 4.2.0 began enforcing the declared 0–200 A on
registers 43117/43118), and HA rejects an out-of-range `number.set_value` before the integration
ever sees it. Clamping *before* the comparison also keeps the write idempotent: an unclamped
target could never match the clamped readback, so every cycle would re-issue a doomed write. A
clamp logs a warning — it means the hardware will not deliver what the planner priced in, so the
configured battery/inverter limits need reconciling with the entity bounds. Second, a service
call that raises is **logged, not propagated**: the writes that matter most come last (the RC
engage/release block), and letting one rejected register skip them leaves the inverter
half-configured — or, on a non-RC mode, leaves a stale forced discharge running. The verify loop
below is the designed detector for a write that did not take.

**Direct dispatch on button press.** Selecting an option in `select.sunsale_mode_override` does
*not* trigger a full coordinator refresh — that would re-run 14 translators, 16 DAG nodes, and a
dozen store saves just to reach the dispatcher. Instead `async_select_option` calls
`coordinator.dispatch_mode_override`, which routes straight to
`InverterControlModule.dispatch_override` — a lightweight sibling of `tick` that runs only the
plan → act → verify path (no observe, no history). It shares the act path with `tick` via the
private `_apply_dispatch` core; releasing the override to `sunsale` (`None`) still dispatches the
current schedule slot's mode immediately, using the last-computed `Schedule` cached in
`coordinator.data`. The button thus feels instant, and the next regular tick reconciles the rest
of the pipeline. Unlike `tick`, the direct path deliberately leaves the automation-transition
bookkeeping (`_reassert_next` / `_last_automation_enabled`) untouched — a button press is not an
automation toggle.

`sensor.sunsale_observed_inverter_mode` surfaces per-tick dispatch diagnostics —
`last_dispatch_outcome` (`ok` write issued / `reconcile` re-write for an unchanged target /
`holding` target unchanged and engaged / `no_target` / `no_spec` / `automation_disabled`),
`last_dispatch_target`, `last_dispatch_tick_at`, and `automation_enabled_at_dispatch` — so a user
can confirm whether a UI mode change actually reached `apply_mode` or fell through one of the
gates.

## Commanded-mode tracking + verify loop

The control module keeps its own truth of what the inverter was last asked to do —
`last_commanded_mode` / `last_commanded_at` — independent of the solis_modbus state cache. On any
cycle where the resolved target differs from this stored value:

1. `apply_mode` is invoked with `force=True`, which bypasses the cached-readback comparison in
   `_apply_43110_bits` / `_set_number`. The Solis integration's polling cache can lag a
   successful write by up to one slow-poll interval (~30 s), so trusting it on the
   commanded-change cycle would risk silently dropping the write.
2. The first verify is scheduled at +2 s (`_VERIFY_INITIAL_DELAY_S`), then poll every 5 s
   (`_VERIFY_POLL_INTERVAL_S`) up to 30 s of total elapsed wall-clock (`_VERIFY_WINDOW_S`). Each
   poll re-reads **every register the commanded mode writes** — the 43110 bitmask plus whichever
   of `charge_a` / `discharge_a` / `export_limit_w` / `rc_setpoint_w` the spec carries, plus the
   `rc_enable` row (RC function selector, register 43132) for RC-backed modes — and compares each
   to its target (`_build_register_status`, exact match for the bitmask and the selector,
   `_NUMBER_WRITE_EPSILON` slack for the numbers, mirroring `apply_mode`'s write tolerance).
3. **All** registers match → `verify_state = "ok"` and the loop stops. Any mismatch within the
   window → keep polling. Mismatch past the window (first time) → log warning, force-rewrite,
   reset the window, resume polling. Mismatch past the second window → `verify_state =
   "mismatch"`, log error, stop. Worst-case time to verdict ≈ 60 s.

A new commanded change always supersedes any pending verify (cancel + reschedule).
`sensor.sunsale_observed_inverter_mode` exposes `last_commanded_mode`, `last_commanded_at`,
`verify_state`, `last_verify_at`, `last_verify_observed_reg`, `register_status` (the verify/drift
per-register rows), plus `observed_mode` and `register_panel` (the always-populated
intended-vs-observed comparison the panel renders — see *Panel readout* below) so the operator
can see the loop status at a glance.

## Slow drift reconciliation

The verify loop only lives ~60 s after a commanded change. Once it settles on `verify_state ==
"ok"`, nothing in the write/verify path corrects a *later* drift — the inverter being changed at
its own screen, or a register slipping — while the schedule holds the same mode.
`_dispatch_current_slot` would keep returning `holding` and the panel would show a red register
grid for hours without acting. The reconciliation path closes that gap:

- **Drift re-command.** On every `holding` tick where `verify_state == "ok"`,
  `_registers_drifted()` re-checks the commanded mode's registers against the inverter. A drift
  increments a consecutive-cycle counter; at `_DRIFT_RECONCILE_CYCLES` (default 2 ticks ≈ 10 min
  — a debounce against a single transient readback glitch) the unchanged target is re-commanded
  via the same force-write + verify-loop path (`outcome = "reconcile"`). The counter resets the
  moment a tick reads clean, or on any write.
- **Conditioned on `ok` or `unknown`.** Drift reconciliation runs from the steady engaged state
  *and* from `unknown` — that verdict is about the rows that could not be read, so a row that
  *was* read and disagrees is real drift regardless; excluding it let one unavailable entity
  disable drift recovery for every other control point indefinitely. It deliberately does **not**
  fire while `verify_state` is `pending` (the verify loop owns that window) or `mismatch` (the
  verify loop's terminal "this write won't take — check the Modbus chain" verdict is respected; a
  stuck inverter is not re-spammed every few cycles).
- **`mismatch` self-heal.** If a `holding` tick finds `verify_state == "mismatch"` but the
  registers now all match (operator fixed it at the inverter, comms restored), `verify_state`
  flips back to `ok` — the terminal mismatch never re-checks on its own, so the badge would
  otherwise stay red forever.
- **Automation re-enable re-asserts.** A False→True `automation_enabled` transition arms
  `_reassert_next`, so the next dispatch re-commands the scheduled mode even if it equals
  `last_commanded_mode` (the inverter may have drifted while automation was paused). Without
  this, toggling automation off→on never re-asserted, because `last_commanded_mode` survived the
  pause.

Both triggers re-enter `_force_write_and_verify` (the shared force-write + verify-restart block),
so a reconciliation is verified exactly like a fresh command — it can itself converge to `ok` or,
if the write genuinely won't take, settle at `mismatch` and stop.

Because the verify loop runs on `async_call_later` *between* coordinator ticks, the module pushes
an `on_state_change` callback (`coordinator._on_control_state_change`) after each verify
mutation; the coordinator mirrors the fresh state via `_mirror_control_module_state` and calls
`async_update_listeners`, so the panel's badge and per-register colours track the verify loop in
real time instead of lagging to the next 5-minute cycle.

## Panel readout + force-verify service

The drawer is opened by the header's **Control** button (`#schedule-toggle`) and rendered as a
single **Control** section. Its control block (`_renderControlRow`) is a compact wrapping flex
row of columns: **Override** (the option buttons, stacked vertically) · **Intended** (the
`last_commanded_mode` name + its expected register targets) · **Observed** (the live-decoded
`observed_mode` name + the actual register readback) · **Policy** (the schedule-policy switches) ·
**Tuning** (the numeric knobs). The Policy and Tuning columns are the same `.sched-row` switch /
number rows as before, just nested into the control row as extra columns rather than their own
sections — so `_syncScheduleDrawer`'s row-count check stays valid; they always render even when
the override select entity is missing. Intended/Observed share one aligned register grid
(`_renderModeReadoutInner` → `.mo-table`, columns `label · intended · observed`) so each
register's target sits directly beside its observed value. The Observed column header carries the
verify badge — green "✓" (ok / Engaged), amber "⏳" (pending / Verifying), red "⚠" (mismatch).
The selected Override button mirrors the same state via the `.pending` (pulsing amber outline)
and `.mismatch` (red) classes. `_renderModeReadoutInner` is the single source of truth for the
Intended/Observed half — both `_renderScheduleDrawer` (initial) and `_syncScheduleDrawer`
(in-place) re-render it.

The `sunsale` sentinel option button is annotated with the mode the scheduler is currently
dispatching — rendered **`sunSale[<mode>]`** by `_optionButtonLabel` from the observed sensor's
`last_commanded_mode` — but only while sunSale is the active selection (`mode_override` is `null`
on the sensor exactly then; under a manual override `last_commanded_mode` is that override, not
the scheduled mode). Each Tuning knob carries an `ⓘ` (`.sched-info`) whose `title` / `aria-label`
describes what the knob does, surfaced on hover or keyboard focus; the Policy switch labels carry
the same description in their `title`.

Each observed register cell is coloured independently from its `match` flag plus the verify
phase: **green** when it matches the intended target, **amber** while the verify window is still
open (`verify_state` pending), **red** once the window has closed on a mismatch, and **neutral**
when the intended mode leaves that register at the hardware default (`match` is `None`, no target
to compare). The cells are driven by the `register_panel` attribute — the *always-populated*
canonical comparison (`InverterControlModule._build_register_panel`): unlike `register_status`
(the verify/drift view, which lists only the registers the commanded mode writes and is empty
before any command), `register_panel` always carries the live observed value of every register so
the operator can see what the inverter is actually doing **even when no mode is commanded or the
observed bitmask maps to no named mode** (`observed_mode == "unknown"`). `register_status` is the
subset of `register_panel` rows whose `desired` is not `None`; `_register_row`'s `match` is
tri-state (`True` / `False` / `None`-for-no-target). `observed_mode` is decoded live from the
same readbacks as `register_panel` (independent of the 5-min `state` string) so the observed-mode
name and register values stay consistent during the verify window.

`sun_sale.force_verify_inverter_mode` is a HA service that bypasses the +30 s scheduled verify
and runs one immediately. Useful for confirming engagement right after a manual mode change
without waiting on the next verify-tick. Implementation: `InverterControlModule.force_verify_now`
cancels any pending verify and re-runs the same `_on_verify_tick` body the scheduled callback
would have.
