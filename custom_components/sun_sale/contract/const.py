"""Constants for the sunSale integration."""

DOMAIN = "sun_sale"

# Config entry keys — tariff
CONF_TARIFF_DISTRIBUTION_FEE = "distribution_fee"
CONF_TARIFF_TAX_RATE = "tax_rate"
CONF_TARIFF_MARKUP = "markup"
CONF_TARIFF_SELL_DISTRIBUTION_FEE = "sell_distribution_fee"
CONF_TARIFF_SELL_TAX_RATE = "sell_tax_rate"
CONF_TARIFF_SELL_MARKUP = "sell_markup"

# Display currency code (ISO 4217, e.g. EUR/SEK/NOK/DKK/GBP/USD). Affects only
# the unit labels / icons on sensors and the panel — never the optimisation math,
# which is currency-neutral (per-kWh in whatever unit the price source supplies).
CONF_CURRENCY = "currency"
DEFAULT_CURRENCY = "EUR"

# Sell-price model (see pipeline/tariff.py:sell_price and TariffConfig.sell_mode).
CONF_TARIFF_SELL_MODE = "sell_mode"
CONF_TARIFF_FIXED_SELL_PRICE = "fixed_sell_price"

SELL_MODE_SPOT = "spot"          # spot-derived (default; Nordpool/ENTSO-e)
SELL_MODE_FIXED = "fixed"        # flat feed-in tariff (e.g. German EEG)
SELL_MODE_SCHEDULE = "schedule"  # per-TOU-band absolute price (e.g. NEM 3.0)
SELL_MODE_FEED = "feed"          # separate live export feed (Octopus/Amber)
SELL_MODES = (SELL_MODE_SPOT, SELL_MODE_FIXED, SELL_MODE_SCHEDULE, SELL_MODE_FEED)
DEFAULT_SELL_MODE = SELL_MODE_SPOT

# Config entry keys — time-of-use distribution-fee bands. Each holds a list of
# {"start": "HH:MM", "buy_fee": float, "sell_fee": float} dicts (local time).
# Empty/absent → the flat distribution fees above apply to every slot.
CONF_TARIFF_WEEKDAY_BANDS = "tariff_weekday_bands"
CONF_TARIFF_WEEKEND_BANDS = "tariff_weekend_bands"

# Number of TOU band rows offered per schedule in the config/options flow.
MAX_TARIFF_BANDS = 4

# Config entry keys — battery
CONF_BATTERY_NOMINAL_CAPACITY = "nominal_capacity_kwh"
CONF_BATTERY_PURCHASE_PRICE = "purchase_price_eur"
CONF_BATTERY_RATED_CYCLE_LIFE = "rated_cycle_life"
CONF_BATTERY_MAX_CHARGE_POWER = "max_charge_power_kw"
CONF_BATTERY_MAX_DISCHARGE_POWER = "max_discharge_power_kw"
CONF_BATTERY_MIN_SOC = "min_soc"
CONF_BATTERY_MAX_SOC = "max_soc"
CONF_BATTERY_ROUND_TRIP_EFFICIENCY = "round_trip_efficiency"
CONF_BATTERY_NOMINAL_VOLTAGE = "nominal_voltage_v"

# Config entry keys — inverter
CONF_INVERTER_PLATFORM = "inverter_platform"
# Per-deployment inverter power ratings (config flow). Stored in kW for UX
# parity with the battery power fields; the coordinator converts to W. Absent
# on configs created before these fields existed — the coordinator falls back
# to DEFAULT_INVERTER_*_KW so existing installs keep the historical 10 kW.
CONF_INVERTER_MAX_POWER_KW = "inverter_max_power_kw"
CONF_INVERTER_EXPORT_LIMIT_KW = "inverter_export_limit_kw"
CONF_INVERTER_ENTITY_BATTERY_SOC = "inverter_entity_battery_soc"
CONF_INVERTER_ENTITY_GRID_POWER = "inverter_entity_grid_power"
CONF_INVERTER_ENTITY_BATTERY_POWER = "inverter_entity_battery_power"
CONF_INVERTER_ENTITY_CHARGE_CONTROL = "inverter_entity_charge_control"

# Config entry keys — Solis-specific inverter entities (state-machine model).
# Auto-detection via inbound/solis_entity_resolver.py is the preferred path;
# these CONF_* keys back the manual-mapping fallback form in config_flow.py.
CONF_SOLIS_CONFIG_ENTRY_ID = "solis_config_entry_id"
CONF_INVERTER_SOLIS_STORAGE_CONTROL_READBACK = "inverter_solis_storage_control_readback"
CONF_INVERTER_SOLIS_BATTERY_MAX_CHARGE_CURRENT = "inverter_solis_battery_max_charge_current"
CONF_INVERTER_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT = "inverter_solis_battery_max_discharge_current"
CONF_INVERTER_SOLIS_RC_SETPOINT = "inverter_solis_rc_setpoint"
CONF_INVERTER_SOLIS_RC_GRID_ADJUSTMENT_SELECT = "inverter_solis_rc_grid_adjustment_select"
CONF_INVERTER_SOLIS_RC_TIMEOUT = "inverter_solis_rc_timeout"
CONF_INVERTER_SOLIS_BACKFLOW_POWER = "inverter_solis_backflow_power"
CONF_INVERTER_SOLIS_PEAK_MAX_USABLE_GRID_POWER = "inverter_solis_peak_max_usable_grid_power"
CONF_INVERTER_SOLIS_SELF_USE_SWITCH = "inverter_solis_self_use_switch"
CONF_INVERTER_SOLIS_TOU_MODE_SWITCH = "inverter_solis_tou_mode_switch"
CONF_INVERTER_SOLIS_ALLOW_GRID_CHARGE_SWITCH = "inverter_solis_allow_grid_charge_switch"
CONF_INVERTER_SOLIS_FEED_IN_PRIORITY_SWITCH = "inverter_solis_feed_in_priority_switch"
CONF_INVERTER_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH = (
    "inverter_solis_allow_export_under_self_use_switch"
)
CONF_INVERTER_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH = (
    "inverter_solis_grid_feed_in_power_limit_switch"
)

# Config entry keys — data sources
CONF_NORDPOOL_ENTITY = "nordpool_entity"  # generic price sensor (all sources)
CONF_NORDPOOL_RESOLUTION = "nordpool_resolution"

# Price source selector. Picks which price-feed translator reads the price
# sensor (see inbound/pricing.py:build_price_translator). Default keeps the
# historical Nordpool behaviour so existing configs need no migration.
CONF_PRICE_SOURCE = "price_source"
CONF_PRICE_EXPORT_ENTITY = "price_export_entity"  # optional separate export feed
# Absolute import-price TOU schedule for the synthetic ``tou`` source: a list of
# {"start": "HH:MM", "price": float} dicts (local time), same row shape as the
# distribution-fee bands.
CONF_PRICE_TOU_BANDS = "price_tou_bands"

PRICE_SOURCE_NORDPOOL = "nordpool"
PRICE_SOURCE_ENTSOE = "entsoe"
PRICE_SOURCE_OCTOPUS = "octopus"
PRICE_SOURCE_AMBER = "amber"
PRICE_SOURCE_TOU = "tou"
PRICE_SOURCES = (
    PRICE_SOURCE_NORDPOOL,
    PRICE_SOURCE_ENTSOE,
    PRICE_SOURCE_OCTOPUS,
    PRICE_SOURCE_AMBER,
    PRICE_SOURCE_TOU,
)
DEFAULT_PRICE_SOURCE = PRICE_SOURCE_NORDPOOL
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
CONF_SOLAR_FORECAST_ENTITY_2 = "solar_forecast_entity_2"
# Device-based forecast selection: a list of forecast-integration config-entry
# IDs. When set, ``inbound/forecast_resolver.py`` resolves each device's base
# "today" forecast sensor automatically. The two legacy entity keys above remain
# as the manual fallback (used when no forecast devices are discovered).
CONF_SOLAR_FORECAST_DEVICE_IDS = "solar_forecast_device_ids"
CONF_INVERTER_ENTITY_HOUSEHOLD_CONSUMPTION_ENERGY = (
    "inverter_entity_household_consumption_energy"
)
CONF_INVERTER_ENTITY_SOLAR_ENERGY = "inverter_entity_solar_energy"
CONF_INVERTER_ENTITY_PV_POWER = "inverter_entity_pv_power"
# Optional dedicated battery-BMS (JK-BMS or similar) telemetry. When the SoC
# entity is mapped, a ChainedBatterySource reads SoC / pack power from the BMS
# in preference to the inverter, falling back to the inverter per field. Pack
# power is signed positive = charging (sunSale convention).
CONF_BMS_BATTERY_SOC = "bms_battery_soc"
CONF_BMS_BATTERY_POWER = "bms_battery_power"
# Optional custom sidebar-panel title. Blank → the default "sunSale" for every
# entry; set per entry to name each instance when more than one exists.
CONF_PANEL_TITLE = "panel_title"
CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY = "inverter_entity_grid_import_energy"
CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY = "inverter_entity_grid_export_energy"
# Daily-resetting battery charge/discharge energy counters (kWh). Feed the
# capacity estimator's energy integral. Optional — absent on inverters that
# don't publish them, in which case the estimator does not learn (uses nominal).
CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY = "inverter_entity_battery_charge_energy"
CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY = "inverter_entity_battery_discharge_energy"
# Per-direction instantaneous grid-power entities (preferred). When both are
# configured, the coordinator uses these in place of the legacy signed
# ``CONF_INVERTER_ENTITY_GRID_POWER``. Each entity must report a
# non-negative magnitude in W or kW for its direction only — the other
# direction reads ~0 at any moment because grid flow is one-way.
CONF_INVERTER_ENTITY_GRID_IMPORT_POWER = "inverter_entity_grid_import_power"
CONF_INVERTER_ENTITY_GRID_EXPORT_POWER = "inverter_entity_grid_export_power"
# AC grid-port power (signed; convention: positive = inverter→grid) feeding the
# derived consumption + losses observers. The Solis auto-detect path resolves
# this to ``ac_grid_port_power``; non-Solis installs map it manually.
CONF_INVERTER_ENTITY_AC_PORT_POWER = "inverter_entity_ac_port_power"
# Backup-port output power (magnitude, ≥ 0). Non-zero only when the inverter
# is bridging backup-protected loads with grid down; otherwise ~0. Solis
# resolves to ``backup_load_power``.
CONF_INVERTER_ENTITY_BACKUP_POWER = "inverter_entity_backup_power"
# Optional: HA entity exposing the inverter's own clock (local-time
# datetime). When set, the inverter_time module tracks HA↔inverter skew so
# the pre-rollover snapshot fires relative to the inverter's idea of midnight
# instead of HA's. Leaving this empty disables skew correction.
CONF_INVERTER_ENTITY_INVERTER_CLOCK = "inverter_entity_inverter_clock"

# Persistent storage
STORAGE_KEY_CAPACITY = f"{DOMAIN}_capacity"
STORAGE_KEY_YESTERDAY = f"{DOMAIN}_yesterday"
STORAGE_KEY_GENERATION = f"{DOMAIN}_generation"
STORAGE_KEY_PV_POWER = f"{DOMAIN}_pv_power"
STORAGE_KEY_CONSUMPTION_DAILY = f"{DOMAIN}_consumption_daily"
STORAGE_KEY_PRICE_HISTORY = f"{DOMAIN}_price_history"
STORAGE_KEY_FORECAST_QUALITY = f"{DOMAIN}_forecast_quality"
STORAGE_KEY_GRID_IMPORT_POWER = f"{DOMAIN}_grid_import_power"
STORAGE_KEY_GRID_EXPORT_POWER = f"{DOMAIN}_grid_export_power"
STORAGE_KEY_GRID_IMPORT_TOTAL = f"{DOMAIN}_grid_import_total"
STORAGE_KEY_GRID_EXPORT_TOTAL = f"{DOMAIN}_grid_export_total"
STORAGE_KEY_DERIVED_POWER = f"{DOMAIN}_derived_power"
STORAGE_KEY_MONTHLY_BILL = f"{DOMAIN}_monthly_bill"
STORAGE_KEY_MODE_HISTORY = f"{DOMAIN}_mode_history"
STORAGE_VERSION = 1

# Debounce window (seconds) for PersistentStore writes. A single coordinator
# tick fans out ~10–14 logical saves (the rolling sample histories plus the
# forecast-quality / monthly-bill / price-history stores), and off-cycle
# refreshes (mode-override presses, force_recalculate, startup) burst these
# further — each previously a full-JSON Store.async_save rewrite, ~3–4k
# writes/day. Routing them through Store.async_delay_save coalesces the
# per-tick fan-out and any burst into one debounced background write per store,
# off the synchronous update path — meaningful flash wear relief on SD-card
# installs. Kept below UPDATE_INTERVAL_MINUTES so steady-cadence writes still
# land each cycle; HA flushes pending writes on clean shutdown via its
# final-write listener, so only an unclean crash within the window can drop the
# most recent (reconstructible) sample.
STORE_SAVE_DELAY_SECONDS = 30

# Rolling generation-sample retention (days). Anything older than this is
# trimmed before persistence each cycle.
GENERATION_HISTORY_RETENTION_DAYS = 2

# Rolling PV-power-sample retention (days). Covers yesterday + today slots.
PV_POWER_HISTORY_RETENTION_DAYS = 2

# Rolling per-direction grid-power-sample retention (days). One value applies
# to both the import and export history stores. Covers yesterday + today for
# billing.
GRID_POWER_HISTORY_RETENTION_DAYS = 2

# Rolling import/export today-total counter retention (days). Same window as
# grid power so end-of-day correction always has yesterday's final value
# available alongside today's samples.
GRID_IMPORT_TOTAL_HISTORY_RETENTION_DAYS = 2
GRID_EXPORT_TOTAL_HISTORY_RETENTION_DAYS = 2

# Rolling derived-power-sample retention (days). Mirrors GRID_POWER history;
# enough to cover yesterday + today for the bake-in window.
DERIVED_POWER_HISTORY_RETENTION_DAYS = 2

# Baked observed history retention (days). Keeps enough history for the
# integration check's rollup window (per-side faults over the last month).
BAKED_OBSERVED_HISTORY_RETENTION_DAYS = 35

# Pre-rollover counter snapshot retention (days). Snapshots only need to
# survive long enough for the next-day bake-in to consume them.
COUNTER_SNAPSHOT_HISTORY_RETENTION_DAYS = 2

# Persistent storage keys for the new observed-series stores.
STORAGE_KEY_BAKED_OBSERVED = f"{DOMAIN}_baked_observed"
STORAGE_KEY_COUNTER_SNAPSHOT = f"{DOMAIN}_counter_snapshot"

# Bake-in source-kind discriminator values stored in BakedDayRecord.source_kind.
SOURCE_KIND_DEDICATED_SENSOR = "dedicated_sensor"
SOURCE_KIND_SNAPSHOT = "snapshot"
SOURCE_KIND_FAILED_NO_SOURCE = "failed_no_source"

# Bake-in hard cutoff — local time after which a bake-in attempt records
# failed_no_source if no source value has materialised. Expressed as
# (hour, minute) tuple in the local timezone.
BAKE_IN_HARD_CUTOFF_LOCAL = (6, 0)

# Pre-rollover snapshot window — local time range during which the snapshot
# module captures the current today-total counter values. Expressed as
# ((start_hour, start_minute), (end_hour, end_minute)) in local time.
SNAPSHOT_WINDOW_LOCAL = ((23, 30), (23, 59))

# Inverter clock-skew tracker: minimum samples in the rolling window before
# ``current_skew_seconds`` returns a value. Until then, the snapshot module
# uses HA local time directly (no shift).
INVERTER_TIME_MIN_SAMPLES = 5

# Inverter clock-skew tracker: maximum samples retained in the rolling
# window. Older samples are trimmed each cycle. Sized so a 5 min coordinator
# cadence yields roughly four hours of history — enough for the median to
# track gradual drift but small enough that a one-shot bad reading washes
# out quickly.
INVERTER_TIME_MAX_SAMPLES = 50

# Rolling per-day consumption-bucket retention (days). One ConsumptionDayRecord
# per local date, each holding 24 hour-bucket sums in kWh. Sized to give a
# full 30 finalised days of input to the per-hour P15 baseload profile.
CONSUMPTION_DAILY_WINDOW_DAYS = 30

# Per-day per-hour completeness gate. A day's hour bucket only feeds the P15
# profile when at least this fraction of the price-grid slots in that hour
# had a derived sample — drops days where the inverter was offline for part
# of the hour and the sum would otherwise underestimate the floor.
CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS = 0.8

# Rolling price-history retention (days) for profitability scoring.
PRICE_HISTORY_RETENTION_DAYS = 90

# Update interval (minutes)
UPDATE_INTERVAL_MINUTES = 5

# Capacity estimator: discard observations with SoC delta below this threshold
# (the estimator's own per-sample reliability floor).
CAPACITY_OBS_MIN_SOC_DELTA = 0.05

# Capacity estimator — anchor accumulator (see coordinator._build_capacity_observation).
# Real SoC moves < ~3 % per 5-min cycle, so a single-cycle delta is both too small
# to clear CAPACITY_OBS_MIN_SOC_DELTA and dominated by SoC quantisation. Instead an
# anchor is held fixed across cycles and an observation is emitted once SoC has
# swung at least CAPACITY_OBS_EMIT_SOC_DELTA in one direction, with energy taken
# from the inverter's own charge/discharge counters over the same span.
CAPACITY_OBS_EMIT_SOC_DELTA = 0.10
# Abandon (re-anchor) a window older than this — guards against an anchor left
# stale by a long telemetry dropout. Windows never span a counter midnight reset
# (that re-anchors immediately), so this only bounds slow-trickle accumulation.
CAPACITY_OBS_MAX_WINDOW_S = 12 * 60 * 60
# A window is "clean" only when the off-direction counter advanced by less than
# this fraction of the on-direction energy; above it the battery oscillated both
# ways and the energy↔SoC mapping is ambiguous, so the window is discarded.
CAPACITY_OBS_PURITY_FRACTION = 0.15
# A counter delta below this (kWh) is treated as a midnight reset / noise floor.
CAPACITY_OBS_COUNTER_RESET_EPS_KWH = 0.05

# Defaults
DEFAULT_NORDPOOL_RESOLUTION = "15min"

DEFAULT_BATTERY_MIN_SOC = 10
DEFAULT_BATTERY_MAX_SOC = 95
DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY = 90
DEFAULT_BATTERY_RATED_CYCLE_LIFE = 6000
DEFAULT_BATTERY_NOMINAL_VOLTAGE = 48.0

# Default Solis entity IDs (canonical names from the solis_modbus integration).
# Only used as placeholders in the manual-mapping config-flow form when
# auto-detection via the entity registry fails.
DEFAULT_SOLIS_STORAGE_CONTROL_READBACK = "sensor.solis_storage_control_switch_value"
DEFAULT_SOLIS_BATTERY_MAX_CHARGE_CURRENT = "number.solis_battery_max_charge_current"
DEFAULT_SOLIS_BATTERY_MAX_DISCHARGE_CURRENT = "number.solis_battery_max_discharge_current"
DEFAULT_SOLIS_RC_SETPOINT = "number.solis_rc_inverter_ac_grid_active_power"
DEFAULT_SOLIS_RC_GRID_ADJUSTMENT_SELECT = "select.solis_rc_grid_adjustment"
DEFAULT_SOLIS_RC_TIMEOUT = "number.solis_rc_timeout"
DEFAULT_SOLIS_BACKFLOW_POWER = "number.solis_backflow_power"
DEFAULT_SOLIS_PEAK_MAX_USABLE_GRID_POWER = "number.solis_peak_max_usable_grid_power"
DEFAULT_SOLIS_SELF_USE_SWITCH = "switch.solis_self_use_mode"
DEFAULT_SOLIS_TOU_MODE_SWITCH = "switch.solis_time_of_use_mode"
DEFAULT_SOLIS_ALLOW_GRID_CHARGE_SWITCH = "switch.solis_allow_grid_to_charge_the_battery"
DEFAULT_SOLIS_FEED_IN_PRIORITY_SWITCH = "switch.solis_feed_in_priority_mode"
DEFAULT_SOLIS_ALLOW_EXPORT_UNDER_SELF_USE_SWITCH = (
    "switch.solis_allow_export_switch_under_self_generation_and_self_use"
)
DEFAULT_SOLIS_GRID_FEED_IN_POWER_LIMIT_SWITCH = "switch.solis_grid_feed_in_power_limit_switch"

# Default inverter / export limits used by storage_mode_specs.build_specs()
# until a per-deployment value is wired through the config flow.
DEFAULT_INVERTER_MAX_POWER_W = 10_000
DEFAULT_EXPORT_LIMIT_W = 10_000
# kW-denominated config-flow defaults / fallbacks for the keys above.
DEFAULT_INVERTER_MAX_POWER_KW = DEFAULT_INVERTER_MAX_POWER_W / 1000.0
DEFAULT_INVERTER_EXPORT_LIMIT_KW = DEFAULT_EXPORT_LIMIT_W / 1000.0

# Mode-history retention: prune samples older than the start of yesterday
# (computed in local time by the control module).
MODE_HISTORY_RETENTION_DAYS = 2

# Schedule policy switches — user-toggleable flags that constrain the DP
# scheduler's action set. Defaults preserve the historical "all modes
# available" behaviour so existing installs see no change after upgrade.
DEFAULT_SCHEDULE_USE_STANDBY = True
DEFAULT_SCHEDULE_ALLOW_GRID_CHARGING = True
DEFAULT_SCHEDULE_ALLOW_FEED_IN = True
DEFAULT_SCHEDULE_ALLOW_DISCHARGE_TO_GRID = True

# Numeric schedule-policy knobs. Values mirror the in-module DEFAULT_* used by
# pipeline/schedule.py so that the user-facing entities start at the same
# operating point the planner used before they were exposed.
DEFAULT_SCHEDULE_MODE_CHANGE_PENALTY_EUR_PER_KWH = 0.005
DEFAULT_SCHEDULE_PROFITABILITY_TILT_ALPHA = 0.5
DEFAULT_SCHEDULE_TERMINAL_VALUE_DISCOUNT = 0.5
# Default planner cap on Discharge-to-grid power. Pinned to the RC active-power
# setpoint the dispatcher actually writes for Discharge
# (DEFAULT_INVERTER_MAX_POWER_W) so the schedule's projected discharge rate
# equals what the inverter is commanded to do, not the battery's higher raw
# discharge limit (max_discharge_power_kw). The inverter's force-discharge has
# been observed reverting to ~10 kW, so 10 kW is the safe default to plan
# against even if the hardware setting resets again. Setting the
# "Max Discharge to Grid" number entity to its maximum restores "use hardware
# max" (None); any value below caps the DP's peak grid-export rate.
DEFAULT_SCHEDULE_MAX_DISCHARGE_TO_GRID_KW: float | None = (
    DEFAULT_INVERTER_MAX_POWER_W / 1000.0
)

# Bounds enforced by the Number entities and clamped by the coordinator before
# the policy reaches the DP. Mode-change penalty is bounded above by 0.10
# EUR/kWh — much higher and the DP would never change modes; profitability
# tilt and terminal discount are dimensionless and live in [0, 1].
SCHEDULE_MODE_CHANGE_PENALTY_MIN = 0.0
SCHEDULE_MODE_CHANGE_PENALTY_MAX = 0.10
SCHEDULE_PROFITABILITY_TILT_ALPHA_MIN = 0.0
SCHEDULE_PROFITABILITY_TILT_ALPHA_MAX = 1.0
SCHEDULE_TERMINAL_VALUE_DISCOUNT_MIN = 0.0
SCHEDULE_TERMINAL_VALUE_DISCOUNT_MAX = 1.0
SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MIN = 0.5
SCHEDULE_MAX_DISCHARGE_TO_GRID_KW_MAX = 30.0
