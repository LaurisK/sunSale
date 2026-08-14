# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Any behavior-affecting change bumps the `version` in
`custom_components/sun_sale/manifest.json` and adds an entry here — see
[`docs/RELEASING.md`](docs/RELEASING.md).

## [Unreleased]

## [0.4.4] — 2026-08-13

### Fixed
- **A commanded discharge could start a full cycle (5 min) late, ~40 % of the
  time.** `solis_dispatch` writes the Remote Dispatch block as two register
  writes — `44100/44101` (enable + failsafe), then `44105/44106` (control mode +
  power target) — but writing the enable is what puts the inverter *into* the
  dispatch state, and entering that state initialises mode/target to its own
  defaults (`1`/`0`). Whether the second write survives is therefore a race
  against the firmware's initialisation: when it loses, dispatch reads back
  **running with a zero power target** and the inverter exports nothing until
  the next holding tick re-pushes the same block 5 minutes later. On the
  reference install 12 of 30 discharge starts between 2026-08-07 and 08-13 were
  delayed by 305–350 s (the rest engaged in 6–52 s), costing a third of each
  affected 15-minute slot at exactly the priced peaks; the loss was caught
  directly on 2026-08-12 05:03, where a poll landed between the two writes and
  read `active=1, mode=1, target=0`. A **commanded** dispatch (mode change,
  reconcile, verify retry — not a routine hold, which has no enable edge under
  it) is now re-sent once, unconditionally, 10 s later; a second write with
  dispatch already running has no edge to race and has always stuck. Worst-case
  lag drops from ~5 min to ~10 s.

  The verify loop could not have caught this and was not extended to try: the
  `sensor.*_dispatch_*` readbacks are `solis_modbus`'s **write-through cache**,
  so they echo the commanded value within a second of the write and the real
  register value only surfaces on the ~10-minute poll. Verify read its own
  optimistic echo and reported `ok` while the inverter delivered 0 kW — which is
  why the repair is a blind re-write rather than a comparison.

## [0.4.3] — 2026-08-05

### Fixed
- **Slot activation lagged the slot boundary by a constant 0–5 minutes.** The
  coordinator polled on `DataUpdateCoordinator`'s `update_interval`, which
  re-arms relative to the *end of the previous refresh*, so its phase against
  the wall clock is set by whenever Home Assistant last started and never
  self-corrects. Because the 15-minute slot grid is a multiple of the 5-minute
  interval, that phase offset applies identically to **every** boundary: the
  reference install was cycling at `hh:mm:48` with `mm ≡ 2 (mod 5)`, so each
  new slot's `StorageMode` reached the inverter 2 min 48 s into a 15-minute
  slot — 19 % of every slot spent executing the previous slot's decision, at
  exactly the moments (price steps) the schedule was built around. Cycles are
  now driven by a UTC wall-clock-aligned tick on the `:00/:05/:10/…` grid, which
  by construction includes every slot boundary, so a slot's mode is dispatched
  at the instant it activates. The tick is skipped (with a warning) if the
  previous cycle is still running, and refreshes directly rather than through
  the debouncer, whose cooldown would give part of the lag back.

## [0.4.2] — 2026-08-05

### Fixed
- **Remote Dispatch exported past the configured export cap.** The dispatch
  power target was composed from the *declared* spec — the inverter rating —
  to escape the RC `number` entity's declared ±10 kW, which bounds a register
  the dispatch path never writes. But that also escaped the **export cap**,
  which is a real configured limit, and the cap does not save the write
  inverter-side either: `solis_dispatch` writes `0xFFFF` ("no system caps") to
  the dispatch block's own import/export limit registers (44103/44104), so a PCC
  power target *overrides* register 43074 rather than being clipped by it. The
  reference install exported **13.7 kW against a configured 10 kW cap** while
  every control row — including `export_limit_w` reading 10000 — reported
  `match`. The magnitude is now `min(inverter rating, configured export cap)`,
  applied only when discharging: the cap governs grid export, so it says nothing
  about GridCharge's import, whose magnitude is already the battery's own charge
  limit. The battery discharge leg is deliberately still not applied —
  commanding above what the battery can deliver is harmless, exceeding an export
  cap is a compliance question.

## [0.4.1] — 2026-08-05

### Fixed
- **A dispatch-capable install could be pinned to the RC path by a startup
  race.** `SolisDispatchDriver` support was decided once, at driver
  construction — but both halves of the gate depend on `solis_modbus` being
  further along than sunSale, and Home Assistant sets custom integrations up
  concurrently: the `solis_dispatch` service is registered during *its* setup,
  and the capability sensor sits on a SLOW poll group. On the reference install
  register 34502 read `0xAA55` throughout and every dispatch role resolved, yet
  `control_path` still came up `rc` and would have stayed there until the next
  restart. Support is now resolved lazily and a negative answer is never cached
  (it may be "not yet"), while a positive one is latched. The wrapper is always
  built for Solis; `capability()` — called every cycle — is what picks the
  change up, and it keeps the RC leg until dispatch is confirmed so the planner
  never budgets export the fallback cannot deliver.

## [0.4.0] — 2026-08-05

### Added
- **Solis Remote Dispatch driver** for the two forced modes. Where the install
  supports it — `solis_modbus` exposes `solis_dispatch` *and* the inverter
  advertises the capability on input register 34502 (`0xAA55`) — Discharge and
  GridCharge are commanded through the Remote Dispatch block (44100–44112)
  instead of the Remote-Control registers, as `grid_export` / `grid_import`
  meter-side power targets. Three things this fixes that the RC path could not:
  the deadman is **sunSale's to size** (`failsafe_minutes`, refreshed by the
  ordinary holding tick, instead of racing a ~5-minute firmware expiry that
  43282 cannot extend without register 43135); the writes are **raw registers**,
  so neither the `number` entity's same-value short-circuit nor its declared
  ±10 kW ceiling applies; and mode + power + failsafe land as **two atomic block
  writes** rather than a sequence any one of which can be dropped. Because the
  ±10 kW is an entity bound rather than a physical one, `capability()` drops the
  RC leg on this path — **planned peak export rises accordingly**, bounded now by
  battery current and the export cap alone.
  Passive modes keep the register path unchanged, and the driver wraps rather
  than replaces `SolisDriver`, so every read delegates. Selection is gated, so an
  older `solis_modbus` or a non-dispatch inverter silently keeps the RC path;
  `sensor.sunsale_observed_inverter_mode` reports which is live as `control_path`.
  **The 44100 block is a single-writer resource** — SolisCloud's EMS drives the
  same registers. sunSale releases dispatch when a passive mode is commanded, and
  a foreign write during a held forced mode surfaces as drift and is
  re-commanded.
- **Upstream contract guard** (`tests/test_solis_modbus_contract.py`) plus a CI
  job running it against `solis_modbus` at both its latest release and `master`.
  sunSale calls another integration's services and resolves its entities by
  unique-id slug; neither is a versioned API, and a rename there fails *silently*
  at runtime because the write path logs and swallows a rejected call. The guard
  parses upstream's own `services.yaml` and `const.py` and asserts every service
  name, field, dispatch mode name, capability register and readback slug sunSale
  depends on still exists — including the `number` same-value short-circuit that
  `set_number(renew=True)` exists to defeat. It skips without a checkout, so a
  fresh clone still has a green suite.
- `control_path` on `sensor.sunsale_observed_inverter_mode` and in the debug API
  payload, reporting which write path the driver picked (`dispatch` / `rc`).
  Selection happens once at setup, so without it a support capture gives no way
  to tell which control path produced it.

## [0.3.0] — 2026-08-05

### Changed
- **Liveness is now a driver concern.** `needs_keepalive` and `refresh_rc` are
  gone from the `InverterDriver` / `InverterControlDriver` protocols — both were
  Solis vocabulary (`refresh_rc` is named after a Solis register group) leaking
  into the platform-neutral seam, and they forced `InverterControlModule` to own
  a keep-alive timer whose correct cadence only the platform knows. The protocol
  now says `hold(mode, spec)` — "this is still the target" — plus `shutdown()`,
  and each driver decides what holding costs. `SolisDriver` owns its 3-minute RC
  heartbeat, phased to each `apply_mode` and stopped when a passive mode is
  commanded; `EntityControlDriver` owns the equivalent for `keepalive` plans.
  The shared timer plumbing is `outbound/heartbeat.py`, deliberately separate
  from `outbound/driver.py` so the contract module stays free of Home Assistant
  imports. No behaviour change on any platform — the same writes happen at the
  same cadence, from a different owner. Groundwork for a Remote Dispatch driver
  whose inverter-side failsafe needs no heartbeat at all.

## [0.2.2] — 2026-08-04

### Fixed
- **Every RC deadman re-arm was a silent no-op.** `refresh_rc` re-wrote the RC
  timeout (43282) and setpoint (43128) with `force=True`, but `force` only
  bypasses *sunSale's* own readback comparison — one layer down,
  solis_modbus's `SolisNumberEntity.set_native_value` returns early when the
  value equals what the entity already holds, and "value unchanged" is exactly
  the steady state a keep-alive exists to hold. Only the 43132 selector write
  (whose `on_value` branch writes unconditionally) ever reached the wire. Number
  writes now take a `renew` flag that first writes an in-range neighbouring
  value, so the target write is a genuine change the integration forwards.
  This also re-reads the 2026-06-15 capture that concluded "setpoint rewrites
  don't re-arm the function": those rewrites had never been sent.
  A 2026-08-04 capture measured a held Discharge collapsing at exactly 5 min 08 s
  while 43110 / 43128 / 43132 all still read "engaged".
- **A command issued during an integration restart was never retried.** When the
  verify window closed with *every* targeted control point unreadable, the loop
  settled on `unknown` and stopped. But `EntityActuator` logs and swallows a
  service call against an unavailable entity, so a mode commanded seconds after
  an HA restart vanishes silently — and a total readback blackout is exactly the
  case where "unreadable" also implies "the write was lost". The verify loop now
  issues its one retry on a full blackout as well as on a mismatch. A partial
  outage (some rows matching) still does not retry: the write demonstrably
  reached the inverter.

## [0.2.1] — 2026-08-04

### Fixed
- **A commanded Discharge / GridCharge fell back to the base mode after a few
  minutes.** The RC deadman keep-alive (both the 3-min heartbeat and the
  per-tick refresh) only ran while `verify_state == "ok"`, i.e. only when
  *every* control point the mode targets read back matching. Any unrelated row
  that was stuck or unreadable therefore disabled the keep-alive entirely, and
  the inverter expired the RAM-only RC function within minutes while registers
  43110 / 43128 / 43132 all still read "engaged" — so the panel showed the mode
  engaged with nothing happening. Seen live in both shapes: pre-0.2.0 the
  battery-current rows were a permanent `mismatch` (the 200 A clamp), and from
  2026-08-03 a solis_modbus register-group outage left those same entities
  unavailable, pinning `verify_state` at `unknown`. The keep-alive is now
  conditioned only on the commanded mode needing one — `verify_state` says
  nothing about whether the volatile function is about to expire, and a
  mismatch is often the RC selector itself having reverted.
- **One unreadable control point disabled drift reconciliation.** The slow
  drift path required `verify_state == "ok"`, which is unreachable while any
  targeted entity is unavailable, so a genuine drift on a *readable* register
  (e.g. the RC selector reverting) was never re-commanded. Reconciliation now
  also runs from `unknown`; `pending` and the terminal `mismatch` stay excluded.

## [0.2.0] — 2026-08-03

### Fixed
- **Control loop could never verify a clamped write.** `apply_mode` wrote each
  number target clamped into the entity's advertised `min`/`max`, while
  `control_surface` compared the readback against the *unclamped* composed
  `StorageModeSpec`. Any target above an upstream bound therefore reported a
  permanent mismatch: two force-writes and an `ERROR` per slot boundary, then
  `verify_state: "mismatch"` forever. Live since solis_modbus v4.2.0 began
  honouring the declared 0–200 A ceiling on registers 43117/43118 against a
  292.97 A composed target. Drivers now return an **effective** spec
  (`effective_spec`), so the value written and the value verified are the same
  number; the unreduced composition stays available as `declared_spec`.
- **An unreadable control point was treated as register drift.** `ControlRow`
  collapsed "readback disagrees" and "no readback" into `match=False`, which
  pinned `_registers_drifted()` True for as long as an entity stayed
  unavailable — starving the stale-verdict self-heal. Rows now carry a
  four-state `status` (`match` / `mismatch` / `unknown` / `no_target`); only
  `mismatch` counts as drift. `match` is retained as a derived property so the
  panel, sensor attributes and debug API keep their shape.
- **`decode_mode` reported a confident StandBy for an unreadable inverter.**
  Under register 43110 = 1, `discharge_a` is the sole discriminator between
  StandBy and the SelfUse variants, and it was defaulted to 0 when `None`. An
  inverter whose current registers stopped publishing therefore read as StandBy
  while it may have been actively discharging. It now decodes to `UNKNOWN`.

### Added
- **Capability seam** (`docs/capability_seam.md`). `EntityActuator.limit_for()`
  is the single place sunSale reads an upstream entity's advertised bounds,
  resolved live each cycle because solis_modbus v4.2.2 derives them from BMS
  mirror registers after entity construction. It feeds the write clamp, the
  drivers' `effective_spec`, and a new `InverterCapability` projection.
- Verify state `unknown`, reported when every disagreeing control point is
  merely unreadable. No retry is issued — re-writing an invisible register
  proves nothing — and the panel renders those rows grey rather than red.

### Changed
- **The DP now plans against the hardware's live envelope.** `capability()`
  projects the control-point bounds into kW and the coordinator reduces the
  configured export / discharge-to-grid caps by it before the planner runs, so
  the schedule stops budgeting transfer rates the dispatcher would clamp.
  Discharge-to-grid takes the binding one of its three legs — battery current at
  the configured bus voltage, the RC active-power setpoint, and the export cap.
  On the reference install those are 10.24 kW, 10.0 kW and 20.0 kW, so planned
  peak export drops from the configured 15 kW to 10 kW. **This changes generated
  schedules.** `number.sunsale_max_discharge_to_grid` can now only ever *reduce*
  the planned rate.
- The battery legs are reduced too: `ScheduleNode` substitutes the resolved
  ceilings into `BatteryConfig`'s power limits, so the DP's per-slot energy
  envelope in `slot_physics` follows the hardware. Previously it stayed at the
  configured value — on the reference install budgeting 3.75 kWh of charge per
  15-min slot against an achievable 2.56 kWh. Grid-charge and solar-absorption
  planning shift as a result.
- Entity-driven platforms (Huawei / SolaX / Sungrow / GoodWe / Deye) get the
  same per-write reduction, so their verify loop is honest too. Their
  `capability()` reports no resolved bounds — there is no uniform role
  vocabulary across them to map legs from, and inventing one for drivers that
  have never been hardware-verified would feed the planner unchecked numbers.
  They therefore keep planning on configured caps, exactly as before.
- New `capability` integration check validating that the planner's caps equal
  `min(configured, capable)` per leg, wired into the TUI and exposed under
  `pipeline.schedule_policy.effective` in the debug view.

## [0.1.0] — 2026-08-03

First tagged release. Alpha: only the Solis platform (`solis_modbus`) is verified
against real hardware — see the status note in [`README.md`](README.md).

### Added
- CI pipeline: pytest with coverage, ruff, mypy, an import smoke test against
  real Home Assistant, plus hassfest and HACS validation.
- Packaged releases — pushing a `v*` tag builds `sun_sale.zip` (integration files
  at the archive root) and attaches it to a GitHub release, which HACS consumes
  via `zip_release`. Procedure in [`docs/RELEASING.md`](docs/RELEASING.md).
- Export-cap-aware scheduling: the DP planner now receives the configured
  inverter export limit (`SchedulePolicy.export_limit_kw`) and reserves battery
  headroom for over-cap PV, charging energy that would otherwise be curtailed.
  The effective cap is exposed in the debug view under `pipeline.schedule_policy`
  and validated by the schedule integration check.

### Changed
- Discharge-mode slot physics now respect the deployment export cap (matching the
  backflow limit the mode spec writes to the inverter): battery discharge
  throttles to what load plus the export budget can absorb, and over-cap solar
  curtails instead of exporting.
- The coordinator refresh is decomposed into ordered cycle steps
  (`orchestration/cycle_steps.py`), and the persistent-store codecs moved into a
  declarative registry (`orchestration/store_codecs.py`) covered by round-trip
  fixtures.

### Fixed
- The manifest declares `recorder` under `after_dependencies`, so Home Assistant
  starts the recorder before sunSale (the observer resampler replays recorder
  history) and hassfest validation passes.
- `LICENSE` carries the verbatim AGPL-3.0 text so GitHub and HACS can identify
  the license; the project-specific copyright notice moved to `NOTICE`.
- Mode dispatch no longer aborts when one register write is rejected. Number
  targets are clamped into the target entity's advertised `min`/`max` before the
  readback comparison, and a failed service call is logged instead of raised, so
  the writes that follow it — the Solis RC engage/release block in particular —
  always run. This unblocks installs on solis_modbus ≥ 4.2.0, where registers
  43117/43118 began enforcing their declared 0–200 A ceiling and a battery
  configured above that range (e.g. 15 kW at 51.2 V ⇒ 293 A) made every
  `apply_mode` fail before it reached the export limit and RC setpoint. A clamp
  logs a warning naming the entity and its bounds: the hardware will not deliver
  the throughput the planner assumed, so the configured battery limits need to
  match what the integration actually accepts.
- The dashboard battery card reads state of charge live from the mapped SoC
  sensor — resolved from the detected platform role rather than raw config, which
  auto-detected installs never write back — and shows the plan's projected peak
  for today.
- Forecast-accuracy statistics exclude curtailment-suspect slots: a slot that
  under-generated while a no-export regime was active is flagged `censored`, kept
  in the chart, and left out of the series-level statistics and every EMA quality
  bucket.

[Unreleased]: https://github.com/LaurisK/sunSale/compare/v0.4.3...HEAD
[0.4.3]: https://github.com/LaurisK/sunSale/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/LaurisK/sunSale/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/LaurisK/sunSale/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/LaurisK/sunSale/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/LaurisK/sunSale/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/LaurisK/sunSale/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/LaurisK/sunSale/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/LaurisK/sunSale/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LaurisK/sunSale/releases/tag/v0.1.0
