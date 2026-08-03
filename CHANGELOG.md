# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Any behavior-affecting change bumps the `version` in
`custom_components/sun_sale/manifest.json` and adds an entry here — see
[`docs/RELEASING.md`](docs/RELEASING.md).

## [Unreleased]

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

[Unreleased]: https://github.com/LaurisK/sunSale/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/LaurisK/sunSale/releases/tag/v0.1.0
