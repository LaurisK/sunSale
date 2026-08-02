# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Any behavior-affecting change bumps the `version` in
`custom_components/sun_sale/manifest.json` and adds an entry here — see
[`docs/RELEASING.md`](docs/RELEASING.md).

## [Unreleased]

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
