# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Any behavior-affecting change bumps the `version` in
`custom_components/sun_sale/manifest.json` and adds an entry here — see
[`docs/RELEASING.md`](docs/RELEASING.md).

## [Unreleased]

### Fixed
- **A control register's read-back is no longer pre-selected as a grid meter.**
  `solis_modbus` mirrors its writable dispatch import / export limits back as
  `power` sensors with state class `measurement`, and their names — *Dispatch
  Import Limit* / *Dispatch Export Limit* — match the grid-import / grid-export
  name hints exactly, so `inbound/inverter_sources.py` offered them *first* and
  pre-filled them on the "Inverter power sensors" page. Confirming that page
  bound the per-direction grid-power observers to a register sentinel
  (6 553 500 W = 6553.5 kW, identical on both directions), which put 1638.375
  kWh into every 15-minute slot of both directions and 11 696 € into one day of
  the monthly bill. A candidate whose name says *limit*, *setpoint*, *target*,
  *dispatch* or *threshold* now sorts last, is never pre-selected, and is
  labelled *a limit or setpoint, not a meter* — still offered, because the
  detection ranks and never filters.
  - Existing entries are not migrated: an install that already stored one keeps
    it (a visit to the page must never silently change a reading), so clear the
    row to fall back to the signed `grid_power_net` projection.
- **A dead price feed no longer takes the whole dashboard down with it.** The
  `PriceSeries` slot grid is what the generation and every observed series are
  resampled onto, so when the price sensor produced nothing the grid shrank to
  whatever days were left — on a permanently unavailable sensor, the persisted
  *yesterday* alone. Everything else followed it down: no forecast for today or
  tomorrow, an empty schedule, no price level, and charts showing yesterday and
  nothing since. The grid is now always padded out to the full local
  yesterday→tomorrow window, with the slots the feed had no price for marked
  `priced=False`.
  Found after a host restart with no working DNS left the Nordpool integration's
  sensor permanently `unavailable` (its entity raises on its first fetch inside
  `async_added_to_hass`, so it is never added and never retries — reloading the
  entry brings it back).
- **Nothing trades or bills on a price that was never published.** `priced=False`
  slots are skipped by `calculation` — the single choke point in front of the
  optimiser — and by the monthly bill, price level, profitability and price
  forecast. This also covers tomorrow before the day-ahead auction publishes:
  `_zero_fill_tomorrow`'s placeholders used to be priced as a real 0.00 €/kWh
  spot, which on a 15-min grid put 96 fabricated slots — two thirds of the
  horizon — into the optimiser's plan.
- **A 4× energy overstatement whenever the feed was empty.** `PriceSeries.resolution`
  was taken from the price feed even when that feed had no entries, so it
  reported the translator's unused 1 h default against a 15-min grid. The
  schedule and the price forecast both read it as `slot_hours`. It is now
  re-derived from the slots that exist. `tools/checks/pricing.py` gained a
  declared-resolution-vs-slot-spacing check so this cannot slip through again.
- **sunSale no longer holds up Home Assistant's startup.** `async_setup_entry`
  awaited a full coordinator cycle — Modbus reads plus a recorder replay —
  before returning, which put `sun_sale` in core's "blocking … wrap-up" warning
  and, with a slow inverter link, stretched startup badly. While HA is still
  starting the blocking first refresh is skipped; the existing
  `EVENT_HOMEASSISTANT_STARTED` hook runs the first cycle, and every entity
  already renders a `None` `coordinator.data`. A later reload keeps the first
  refresh and its `ConfigEntryNotReady` retry.
- **The `holidays` import no longer blocks the event loop.** The first lookup
  imported the package — a disk read — from inside a DAG cycle, tripping HA's
  blocking-call detector. The per-country cache is now warmed from an executor
  during coordinator setup.
- **The recorder stops rejecting the panel sensors.** `sensor.sunsale_dashboard`
  (~40 kB) and `sunsale_monthly_bill` (~18 kB) carry a whole pipeline bundle in
  their attributes, past the recorder's 16 kB ceiling — logged and dropped every
  cycle. The bundle sensors now declare `_unrecorded_attributes`, keeping their
  *state* recorded (so long-term statistics still work) without the churn.

## [0.6.0] — 2026-09-20

### Added
- **Forecast sensors are detected, and what each integration resolved to is
  shown.** *Solar forecast* now has three disjoint pickers: the integrations
  (unchanged), a list of forecast sensors **no integration covers** — the long
  tail of an unrecognised integration or a hand-built sensor — and the by-hand
  picker for the rest. All three merge into one de-duplicated list, so an array
  can never count twice. `detect_forecast_sensors` recognises a sensor by the
  per-slot data `SolarTranslator` actually parses (a `watts` dict or a
  `forecast` list — *not* `wh_period`, which it cannot read) and only offers
  "today" sensors, since the other days are reached by substituting that word.
  The form also names each chosen integration's resolved sensor, and says so
  when one resolved to nothing — previously a silent log line, so an array
  could contribute no forecast at all without anyone noticing.
- **The inverter's optional sources are detected.** The old *Energy counters and
  BMS* page asked for each sensor out of every sensor in Home Assistant. It is
  now two pages — *Power sensors and BMS* and *Energy counters* — whose every
  row lists the sensors found **on the inverter's own device**, pre-selecting
  what the platform already resolved, else what the user stored, else the best
  name match. `inbound/inverter_sources.py` locates the device from the
  platform's resolved role map (the way the metered-device picker locates a
  meter) and classifies the rest by device class, counter state class and a
  today/yesterday split. Every same-kind sensor is still offered — the hints
  only rank — so an unusually named sensor stays reachable, and **Other** opens
  a by-hand picker over the whole system for any row that needs it. With
  nothing detected a row keeps its plain entity picker.
- **The price forecast's weather entity is now visible and selectable.** New
  optional *Price forecast weather* page in the **prices** section (not the solar
  forecast, which is hidden without an inverter — weather feeds only the
  week-ahead *price* model, and a prices-only install is exactly where that
  matters). `inbound/weather.py:detect_weather_entities` lists every weather
  entity and marks the auto-detected one **in use now**; *None* turns the
  weather correction off and, unlike before, stays off. `CONF_WEATHER_ENTITY`
  was previously read at runtime with no form field anywhere, so which entity
  fed a dispatch-affecting forecast could be neither seen nor changed.
- **A dedicated battery BMS is picked as a device.** New optional *Dedicated
  battery BMS* page: choose the battery device and sunSale resolves its state of
  charge and pack power (`detect_bms_sources` over `BMS_SPECS`), then asks you to
  confirm them — the same two-step shape as adding a metered device, instead of
  hunting two loose sensors on the old *Energy counters and BMS* page. A device
  publishing no state of charge is refused with a reason, because SoC is what
  puts the BMS in front of the inverter at all; confirming with no device removes
  a BMS again. The stored keys are unchanged, so `inbound/battery_source.py` and
  the coordinator are untouched. That page is now *Inverter power sensors*.
- **Price sensors are detected.** *Price source and sensor* now lists the
  sensors on the system that a price translator can read — recognised by the
  attributes each translator parses (Nord Pool `raw_today` / `today` +
  `tomorrow_valid`, ENTSO-e `prices_today` / `prices`, Octopus `rates` /
  `all_rates`, Amber `forecasts`) — and pre-selects the stored one, or the first
  found. Picking one sets both the source and the sensor. **Other** opens a
  follow-up form to pick the source and sensor by hand (also the way to leave
  the sensor blank with both prices fixed); a stored sensor that isn't detected
  opens on Other. With nothing detected the form is unchanged. Detection of the
  non-Nord Pool sources follows their documented schemas and is untested.
- **Export-price feeds are detected too.** Octopus export-rate sensors
  (`is_export`) and Amber feed-in forecasts (`channel_type: feedIn`; the entity
  id as fallback) are kept out of the price-sensor list and offered in their own
  *Export-price feed* list (plus *None*), pre-selecting the feed from the chosen
  price sensor's integration. A feed from another integration is refused
  (`export_feed_mismatch`), since the translator only reads its own. The
  by-hand export sensor field can now be cleared (it used to snap back to its
  stored value), and the page summary shows the export sensor.
- **Price level published to Home Assistant.** Every price slot's buy price is
  ranked within its local day and classed cheap / normal / expensive — the
  cheapest `price_level_cheap_share` / dearest `price_level_expensive_share`
  (defaults 25 % / 25 %), with optional absolute per-kWh limits overriding the
  rank, and a boundary tie group joining a class only when at least half of it
  is inside the share. The level is exposed as
  `sensor.sunsale_price_level`, the `binary_sensor.sunsale_cheap_price` /
  `sunsale_expensive_price` pair and the debug view's `pipeline.price_level`,
  re-evaluated on every quarter-hour boundary. sunSale controls nothing with
  it — automations and other integrations consume it — so it works on any
  install with prices, including a monitoring-only one. The new
  `check_price_level` integration check recomputes the classes from
  `pipeline.pricing`.
- **Prices are optional.** Leaving *Electricity prices* out of the installation
  makes sunSale a monitoring-only install: the coordinator builds no price
  translator, so pricing and everything downstream skip. Placeholder zero
  tariff values are written so setup stays constructible.
- `CONF_INSTALL_CAPABILITIES` and `contract/install_capabilities.py`: what the
  installation has (prices, inverter, solar forecast), derived at Finish from
  the completed setup sections. Entries created before have no stored
  capabilities and read as having everything, so existing installs behave
  exactly as before — no migration.
- **Seven config keys that no form could reach.** These were read at runtime but
  written by no setup page, so unless the Solis resolver happened to auto-fill
  them the features behind them silently did nothing:
  `inverter_entity_ac_port_power` + `..._backup_power` (without both, the
  observed **consumption and losses** series never run),
  `inverter_entity_battery_charge_energy` + `..._discharge_energy` (the
  **battery-capacity estimate** had no input and stayed at nominal),
  `inverter_entity_grid_import_power` + `..._grid_export_power`, and the three
  `inverter_entity_*_yesterday` totals (the **end-of-day bake-in** fell back to
  the pre-rollover snapshot). All are now rows on the new pages, so these work
  on every platform rather than Solis alone.
- **Price estimates in the panel's generation row.** Each upcoming day's pill
  now shows the week-ahead price forecast next to its generation estimate —
  the cheapest–dearest 1 h and 4 h spot price (`≈ 1h 0.021–0.190 · 4h
  0.035–0.160 €`), with the forecast source in the tooltip. The 4 h pair is
  left out while that band has too little history. It shows only while that day's auction is unsettled, so tomorrow's
  estimate disappears once its actual prices are published. Carried in the
  dashboard sensor's new `forecast_daily_price` attribute.

### Removed
- **The HA↔inverter clock-skew tracker.** `inbound/inverter_time.py`,
  `InverterTimeTranslator`, `InverterTimeReading`, `InverterTimeStep`,
  `CONF_INVERTER_ENTITY_INVERTER_CLOCK`, the `INVERTER_TIME_*` constants and
  `maybe_capture_snapshots`' `clock_skew_seconds` argument are gone. **No
  behaviour change**: the key was writable by no form, so every install was
  already on HA-local timing.
  It could not have worked on Solis anyway — solis_modbus publishes the clock as
  six independently-updating integer sensors, not the single datetime the
  translator required, and their update stamps were measured up to 20 minutes
  apart. solis_modbus also syncs the inverter clock itself, and the observed
  ~4-6 min drift is far inside the 29-minute snapshot window, so the capture
  always precedes the counters' reset. `tests/test_pre_rollover_snapshot.py`
  now pins that margin instead of the shift.
- **`CycleScratch`.** The clock skew was its only occupant, so the scratchpad
  went with it: `CycleStep.seed` / `persist` are now `(primary, now)`. Steps
  communicate through `primary` under the real contract types the DAG consumes;
  a step wanting an earlier step's value in a shape the DAG does not model takes
  a constructor-injected collaborator instead of a side channel.

### Changed

- **Setup is one menu tree instead of a fixed step ladder.** Home Assistant's
  setup dialog has one button per form and none on a menu, so navigation lives
  in menu rows. The first screen lists every section — *Electricity prices →*,
  *Inverter and battery →*, *Solar forecast →* (once an inverter is set up),
  *Installation name →* — with its ✓ / · / ○ state, plus an always-present
  **✓ Finish configuration**. When something is missing, Finish stays on the
  menu and says what. Confirming a page of a section adds it to the
  installation, and **✕ Remove** inside the section takes it out again (its
  values are kept).
- Each section is a menu of one-module pages. Prices: price source and sensor,
  buy price, sell price, price level, price-forecast weather. Inverter:
  platform and power ratings, battery, entity mapping, dedicated BMS, power
  sensors, energy counters. Every form's button reads **Confirm** and returns
  to the menu it was opened from; every menu has a **← Back** row.
- **Every form has a way back** — its last field is a *Confirm dropdown*
  (HA allows a form only one button): **✓ save and go back** (the default) or
  **✕ discard and go back** to the menu it came from, with nothing on that page
  saved, instead of cancelling the whole setup. So that discarding is never
  blocked by HA's "fill in all required fields" check, fields with no
  pre-filled value (battery capacity / price, manual entity mapping) are
  checked by sunSale itself and marked "Fill in this field."
- Only pages without a working default are required: the price source (while a
  price follows the market), each direction's formula, its grid fees and every
  schedule its structure needs, and the inverter's platform + battery +
  mapping. Price level, the price-forecast weather, the BMS, both source pages
  and the solar forecast are optional and never block Finish.
- A single auto-detected solis_modbus / vendor inverter is mapped as soon as
  the platform is confirmed, and the *Entity mapping* row is hidden.
- **Config flow and options flow share one implementation**, replacing the two
  near-duplicate step ladders. The options flow is the same menu tree opened on
  the entry: existing entries open in **Configure** with their sections already
  ✓ and can be saved straight away, and editing one page keeps every other
  stored value (the flow is seeded from the entry's data + options). The stored
  config is unchanged; no migration.
- **Solar forecast takes any number of arrays.** One form with a multi-select
  of forecast devices *and* a multi-select of forecast sensors, replacing the
  device picker / two-sensor fallback split. All selected forecasts are summed.
  Once saved, the sensor list (`solar_forecast_entities`) supersedes the two
  legacy keys; entries saved before keep their exact previous behaviour.
- **Setup errors are specific and land on the offending field.** Separate
  messages for each battery field (cycle life, charge / discharge power,
  voltage, min / max SoC, inverted SoC range) and for the currency code; the
  price-sensor error names the chosen source, and the unsupported-platform
  error names the platform.
- Internal: the setup menus live in a `setup_flow/` package — per-section form
  modules, per-section step mixins and a small hub. `config_flow.py` only binds
  them to Home Assistant.
- **Electricity prices: one simple formula per direction.** The tariff is now two
  independently configured price formulas — buy `(energy + markup + grid fee) × (1 + VAT)`
  and sell `(energy − deduction − grid fee) × (1 − tax)`. The energy is the market price
  from the chosen source (Nord Pool, ENTSO-e, Octopus, Amber) or a fixed price; with both
  prices fixed no price sensor is read at all. The grid fee has 1, 2 or 4 tariffs; with
  more than one, a switch-point schedule ("from HH:MM → tariff", up to six rows, wrapping
  past midnight) says which applies when — for workdays, plus optional weekend and
  public-holiday schedules (the `holidays` calendar for Home Assistant's country) — and
  optionally separately for summer and winter (configurable start dates). A dynamic sell
  price uses the separate live export feed (Octopus Outgoing, Amber feed-in) where the slot
  has one. Setup: the prices section is *Price source and sensor* (now also the currency),
  *Buy price*, *Sell price* and *Price level*; each price menu holds *Price formula*,
  *Grid fees* and the schedules its structure needs.
- **Removed from the price setup:** the synthetic time-of-use price source, the `schedule`
  and `feed` sell modes and the weekday / weekend fee-band lists — all expressible in the
  new model. Existing entries need no migration: flat fees, fee bands, sell mode and TOU
  source are converted on read into formulas with the same per-slot prices (distinct band
  fees become tariffs; absolute TOU / avoided-cost prices become a fixed zero energy price
  carried by the grid fee), and saving the options once stores the formulas and drops the
  legacy keys. Debug view: `inputs.tariff_config` is now the `buy` / `sell` formulas; the
  pricing deep-check mirrors the new formula, holidays and seasons included. The
  week-ahead forecast's export break-even uses the cheapest sell grid-fee tariff, and no
  break-even at all for a fixed sell price.

## [0.5.1] — 2026-09-11

Fixes for the 2026-09-09/10 over-discharge incident (analysis:
[`docs/battery_export_guard.md`](docs/battery_export_guard.md)).

### Fixed
- **Planner no longer hides a battery below `min_soc`.** The schedule's forward
  roll started from `max(min_soc, soc)` and every slot clamped its end SoC up
  to `min_soc`, so at a real 3 % the planner planned from 6 % and could command
  Discharge (a 10 kW export target) into an over-discharged battery. It now
  starts from the measured SoC and only lifts it by energy actually charged.
- **No export modes below `min_soc`.** While the battery sits under the floor,
  the forward roll replaces Discharge / Feed-in with Self-use (No-export at a
  negative sell price) so solar refills the battery.
- **Observed mode during Remote Dispatch.** A dispatch-driven Discharge /
  GridCharge was decoded from register 43110 alone and recorded as `feed_in`;
  it is now reported as the forced mode while the dispatch block holds a power
  target.

### Changed
- **Discharge cycle-cost gate.** Discharge is never scheduled in a slot whose
  sell price is below the battery cycle cost `2 × degradation / efficiency`
  (≈ 2 c/kWh on the reference install), independent of any forecast refill.
  Feed-in is unaffected.
- **Remote Dispatch carries a SoC floor.** Every `solis_dispatch` command sends
  `soc_min` = `min_soc` (rounded up), so the inverter itself stops a forced
  discharge at the planner's floor instead of the service default 0 %.
- **Remote Dispatch honours "Allow grid charging".** Every non-GridCharge
  dispatch command sends `allow_grid_charge` mirroring the switch (read at write
  time); GridCharge always permits it. Previously the firmware default
  (grid charge allowed) applied.

### Added
- **Battery-export guard (monitor only).** Trips when grid export exceeds PV
  by ≥ 1 kW for 120 s while a passive mode is commanded — battery energy
  leaving without being asked for. On a trip it captures the inverter's raw
  state (incl. the undocumented 33122 working mode), logs a warning and raises
  a persistent notification; nothing is written. A companion SoC-floor watch
  flags discharge at/below `min_soc` in passive modes. State is exposed as
  `export_guard` / `soc_floor_guard` on the observed-inverter-mode sensor and
  in the debug view, and validated by the new `check_export_guard` deep-check.
  Solis roles `operating_mode` (33122) and `dispatch_running_status` (34504)
  are resolved for the capture.

## [0.5.0] — 2026-08-27

### Fixed
- **Forecast-quality EMA buckets measured the last ~30 minutes, not months.**
  `_update_quality` looped over every matched slot on *every* coordinator cycle
  with no already-ingested guard, so each slot was absorbed ~288×/day. On the
  reference install this was exact: `group2["1"].n = 30526` ÷ 2 per cycle
  = 15,263 cycles ÷ 288/day = **52.997 days**, matching the horizon buckets'
  `n = 53`. With α=0.1 (memory ≈10 updates) the effective window had collapsed
  to **0.6 min** for the busiest bucket — the module docstring's claim that
  quality "accumulates over months" was false. Symptoms visible in the stored
  state: `bias_wh == mae_wh` exactly in 17 buckets (EMA collapsed onto a single
  sample) and R² pinned at the −9.99 clamp in 11. `ForecastQualityStore` now
  carries `last_ingested_slot_utc`; slots at or before it are skipped.
  Pre-existing per-slot buckets are unrecoverable and are dropped on load.

### Added
- **Solar geometry** (`pipeline/solar_geometry.py`) — NOAA solar position from
  `hass.config` latitude/longitude. Verified against an independent
  implementation of the Astronomical Almanac formulae: max deviation 0.008°
  across 2108 daylight instants.
- **Clear-sky reference and index** (`pipeline/clear_sky.py`) — measured output
  as a fraction of cloudless potential, which divides season and time-of-day out
  of the accuracy metrics.
- **Array calibration** (`pipeline/array_calibration.py`) — fits effective kWp,
  tilt and azimuth from baked generation history, then runs the *forecast*
  through the same geometry to derive the capacity the forecast provider appears
  to be configured with. Their ratio (`correction_factor`) answers "is my
  forecast's array the right size?" — a one-time configuration fix worth more
  than any runtime bias correction. Exposed under `pipeline.array_calibration`;
  advisory only, gated behind `CONF_APPLY_FORECAST_CORRECTION` (default off).
- **Array health** (`pipeline/solar_health.py`) — detects soiling, snow, new
  shading and dead strings by comparing the array against *its own past self*:
  the recent 7-day ceiling of the clear-sky index against a 90th-percentile
  baseline. Uses ceilings rather than averages because cloud can only push the
  index down, so the ceiling tracks hardware while the mean tracks weather. Works
  even though the forecast has no skill — it needs the error distribution to be
  stationary, not centred. Exposed under `pipeline.solar_health`. Known
  limitation: a week with no clear spell at all reads as `degraded`.
- **Forecast reserve** (`CONF_FORECAST_RESERVE_ENABLED`, default **off**) — sizes
  a battery reserve from the measured d0/d1 day-ahead RMSE and raises the DP's
  SoC floor by it, so the planner cannot sell down on the strength of solar that
  may not arrive. A *planning* floor only: it never forces a charge, and is
  clamped to the current SoC so a battery already below it is not stranded.
  Capped at 25 % of usable capacity — uncapped, the reference install's ~25 kWh
  d0 RMSE against a ~26 kWh pack would reserve the whole battery.
- `matched_slot_count` on `ForecastErrorSeries`. `slot_count` counts the whole
  forecast window including future slots carrying the `-1.0` "no data" sentinel,
  while the aggregate statistics are computed over matched slots only; summing
  `observed_kwh` across `slots` previously yielded a large negative number for a
  window half in the future.
- `check_array_calibration` deep-check + TUI widget, covering the fitted geometry
  and the health verdict, and verifying `correction_factor` really equals the
  ratio of the two capacities it is derived from.
- [`docs/forecast_calibration.md`](docs/forecast_calibration.md) documenting the
  whole seam.
- **Week-ahead price forecast** (`pipeline/price_forecast.py`) — daily price
  statistics for the next seven days, published on the same day axis as the
  existing week-ahead generation forecast so the two read together. Per local
  day: the mean of the highest and lowest 1 h and 3 h (`peak_1h/3h`,
  `trough_1h/3h`), the 3 h spread, the hours at or below the export
  break-even, and the PV energy expected to land in those hours. Days the
  day-ahead auction already covers are reported verbatim (`source="actual"`);
  the rest are predicted.

  `negative_generation_kwh` splits into `absorbable_kwh` and `surplus_kwh`.
  Headroom is deliberately the best case — the whole battery plus the load
  running through those hours — so surplus is the PV that will be produced into
  loss-making prices *even if the battery is empty by then*. That makes it a
  lower bound on unavoidable spill, and a forward-looking reason to sell
  earlier: if D+2 will spill 40 kWh no matter what, arriving at it full is a
  loss, and today's mediocre price is the best exit available. The split is
  reported as a pair or not at all — an install whose battery size is unknown
  gets neither half rather than a guessed one.

  The trough bands are not decoration. Measured on 20 months of Lithuanian
  prices, the winter change is **not** a collapse in volatility — January's
  3 h spread (0.158) was *higher* than July's (0.135) — it is a 4–5× rise in
  the **trough**: 0.083–0.087 in Jan/Feb against 0.017–0.032 in summer. What
  makes winter different is that refilling gets expensive, not that arbitrage
  stops working.

- **Skill-weighted model with a climatology floor.** The prediction is a
  trailing day-class median; a weather-anomaly ridge regression (wind through a
  turbine power curve, temperature, the install's own PV forecast, plus a
  weekend interaction) contributes only in proportion to its own measured
  out-of-sample skill against that baseline.

  This is a correctness requirement for a multi-region integration, not a
  refinement. Running one model, one specification, across eight European
  bidding zones: it is worth +31 % in DE-LU, +27 % in BE and +21 % in LT, but
  **−16 % in NO2 and −2 % in FR** — where hydro reservoir water value and
  nuclear availability set the price, weather explains nothing and adding it
  actively degrades the forecast. Skill correlates +0.75 with a zone's
  wind+solar share. Rather than configure per region, each install measures its
  own skill and the blend weight collapses to zero when it earns none.

  The skill floor (`PRICE_FORECAST_MIN_SKILL`) is a deliberate dead zone:
  across 30 synthetic histories with *no* signal whatsoever, 12 still scored
  positive skill by chance (max +1.9 %). Trusting any positive number would be
  selecting on noise.

- **Feature vintages** (`DayFeatureVintage`). Each day's model features are
  frozen two days ahead — the shortest horizon the auction does not already
  cover — and the settled record is written from that frozen row, not from the
  day's realised weather. Training on realised weather would flatter the fit
  offline and degrade in production, since forecast error at the serving
  horizon is a large part of the real uncertainty.

- **Weather translator** (`inbound/weather.py`) — reads any Home Assistant
  `weather` entity's daily forecast (auto-detected, or set via
  `CONF_WEATHER_ENTITY`). No API key and no new cloud dependency, so the
  integration keeps its `local_polling` character and works wherever the user's
  existing weather integration does. Weather is optional throughout: without it
  the forecast publishes its climatology baseline.

- **Price-curve history store** (`sun_sale_price_curve_history`) — rolling 400
  days of settled daily statistics plus their frozen feature vintages. The
  existing `price_history` store keeps only daily peaks, which is enough for
  the profitability percentile and nothing else.

- **`sunSale Price Forecast` sensor** — state is tomorrow's expected 3 h peak;
  attributes carry every day plus `model_skill` / `model_weight`, so how much
  the forecast is trusting its weather model is visible rather than implicit.
  The same per-day block is appended to the existing forecast sensor's
  attributes and to `pipeline.price_forecast` in the debug API.

- **`check_price_forecast`** integration check — recomputes settled days from
  `pipeline.pricing`, validates band ordering and spread consistency, bounds
  negative-price generation by each day's forecast total, verifies the
  absorbable/surplus split partitions it exactly, and asserts the skill guard
  holds (no blend weight without positive skill). The TUI surfaces the week's
  total surplus in the check's title line.

### Changed
- **Accuracy buckets are keyed on physical axes.** The old axes binned by
  absolute forecast kWh and by slot-index within the solar day, which conflate
  "low because it is dawn" with "low because it is overcast" — unrelated failure
  modes with unrelated fixes. Replaced by clear-sky index (10 % bins plus a
  ≥100 % overflow, whose population is the signature of a mis-declared array),
  solar elevation (5° bands, isolating air-mass error) and solar azimuth
  (15° bands, where fixed obstructions appear as bearing-specific deficits).
  `group3` is renamed `horizon` and its data is preserved across the migration.
- **MAPE is no longer tracked.** On PV it is dominated by near-zero slots — a
  0.36 kWh dawn slot missed by 0.86 kWh reports 239 % — so the aggregate
  measured how many dim slots a window held rather than forecast quality.
- The forecast-horizon chart now marks d2–d6 as informational: the DP plans only
  ~32 h ahead (bounded by the price series), so only d0/d1 can affect dispatch.
- `tools/checks/forecast.py` fails the forecast-quality check when any per-slot
  bucket's `n` exceeds a plausible per-day ingestion rate, so a re-ingestion
  regression cannot return silently.

### Notes
- Analysis and the reproducible multi-region study behind these numbers:
  [`docs/price_forecast_week_ahead.md`](docs/price_forecast_week_ahead.md) and
  `tools/research/price_forecast_study.py`.
- Nothing in this release affects dispatch. The forecast is published and
  measured only; wiring it into the schedule's terminal value (in particular a
  refill-cost term from `trough_3h`) is deliberately left for a later release,
  once it has a track record on real installs.

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

[Unreleased]: https://github.com/LaurisK/sunSale/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/LaurisK/sunSale/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/LaurisK/sunSale/compare/v0.4.3...v0.5.0
[0.4.3]: https://github.com/LaurisK/sunSale/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/LaurisK/sunSale/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/LaurisK/sunSale/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/LaurisK/sunSale/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/LaurisK/sunSale/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/LaurisK/sunSale/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/LaurisK/sunSale/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/LaurisK/sunSale/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LaurisK/sunSale/releases/tag/v0.1.0
