"""Shared data structures for sunSale. No logic, no HA imports."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import Enum


@dataclass(frozen=True)
class PriceEntry:
    """Spot/import price for one time slot, from any price source.

    ``price_eur_kwh`` is the per-slot import (buy-side) market price.
    ``export_price_eur_kwh`` is an optional *separate* live export price for
    markets that publish a distinct feed-in stream (UK Octopus Outgoing, AU
    Amber feed-in); it is None for single-feed sources (Nordpool, ENTSO-e) and
    is only consumed when the tariff is in ``sell_mode="feed"``.
    """
    start: datetime
    end: datetime
    price_eur_kwh: float
    export_price_eur_kwh: float | None = None


@dataclass(frozen=True)
class TariffBand:
    """One time-of-use band of a daily distribution-fee schedule.

    A band runs from ``start_minute`` until the next band's start, wrapping
    past midnight; the last band of the day wraps around to the first. Only
    the distribution fees are time-of-use — tax rates and markups are global
    (held on the enclosing :class:`TariffConfig`).
    """
    start_minute: int             # Minutes from local midnight (0–1439)
    distribution_fee: float       # EUR/kWh, buy distribution for this band
    sell_distribution_fee: float  # EUR/kWh, sell distribution for this band


@dataclass(frozen=True)
class TariffConfig:
    """User-configured tariff formula parameters.

    Distribution fees may vary by time-of-use via ``weekday_bands`` /
    ``weekend_bands``; tax rates and markups are global. When a schedule is
    empty the flat ``distribution_fee`` / ``sell_distribution_fee`` are used
    for every slot, preserving the pre-TOU behaviour.
    """
    distribution_fee: float       # EUR/kWh, fallback buy distribution (no bands)
    tax_rate: float               # Fractional, e.g. 0.21 for 21% VAT on buy
    markup: float                 # EUR/kWh, retailer margin when buying
    sell_distribution_fee: float  # EUR/kWh, fallback sell distribution (no bands)
    sell_tax_rate: float          # Fractional tax deducted from sell revenue
    sell_markup: float            # EUR/kWh, retailer margin deducted when selling
    weekday_bands: tuple[TariffBand, ...] = ()  # Mon–Fri TOU distribution schedule
    weekend_bands: tuple[TariffBand, ...] = ()  # Sat/Sun TOU distribution schedule
    # Sell-price model selector (see pipeline/tariff.py:sell_price):
    #   "spot"     — (spot − sell_distribution_fee − sell_markup)·(1 − sell_tax_rate)
    #   "fixed"    — flat ``fixed_sell_price`` (feed-in tariff)
    #   "schedule" — per-TOU-band absolute price (band.sell_distribution_fee
    #                reinterpreted as the absolute sell price; e.g. NEM 3.0
    #                avoided cost), falling back to ``fixed_sell_price``
    #   "feed"     — per-slot live export price from the price feed, falling
    #                back to ``fixed_sell_price``
    sell_mode: str = "spot"
    fixed_sell_price: float = 0.0  # EUR/kWh, used by fixed/schedule/feed fallbacks


@dataclass(frozen=True)
class TariffResult:
    """Computed effective buy/sell prices for one hour."""
    hour: datetime
    spot_price: float
    buy_price: float   # Total cost to buy 1 kWh from grid (EUR/kWh)
    sell_price: float  # Total revenue from selling 1 kWh to grid (EUR/kWh)


@dataclass(frozen=True)
class BatteryConfig:
    """User-configured battery parameters (immutable)."""
    nominal_capacity_kwh: float
    purchase_price_eur: float
    rated_cycle_life: int
    max_charge_power_kw: float
    max_discharge_power_kw: float
    min_soc: float               # Minimum SoC to maintain (0.0–1.0)
    max_soc: float               # Maximum SoC to charge to (0.0–1.0)
    round_trip_efficiency: float  # e.g. 0.90 for 90%
    nominal_voltage_v: float = 48.0  # DC bus voltage; used for kW→A conversion on Solis


@dataclass(frozen=True)
class Limit:
    """One control point's resolved writable bound.

    The platform-neutral answer to "what will the hardware accept here", read
    live from whatever the platform's actuator advertises (on Solis: a
    ``number`` entity's ``min``/``max`` attributes, which upstream resolves from
    BMS mirror registers and may therefore change between cycles).

    ``known=False`` means no bound could be resolved at all — the entity is
    unmapped or missing from the state machine — which is distinct from a
    resolved bound that happens to be open on one side (``low``/``high`` of
    ``None``). Callers must not treat an unresolvable bound as "unconstrained":
    the write would still be rejected or clamped downstream.
    """
    low: float | None
    high: float | None
    known: bool

    def clamp(self, value: float) -> float:
        """Return ``value`` reduced into this bound.

        Args:
            value: Desired target in the control point's own unit.

        Returns:
            ``value`` bounded by ``low``/``high``; unchanged when this limit is
            not ``known`` or advertises no bound on the relevant side.
        """
        if not self.known:
            return value
        clamped = value
        if self.low is not None:
            clamped = max(clamped, self.low)
        if self.high is not None:
            clamped = min(clamped, self.high)
        return clamped

    @property
    def constrains(self) -> bool:
        """Return whether this limit actually bounds anything."""
        return self.known and (self.low is not None or self.high is not None)


UNKNOWN_LIMIT = Limit(low=None, high=None, known=False)


@dataclass(frozen=True)
class InverterCapability:
    """What the inverter will actually accept, resolved live each cycle.

    The platform-neutral projection of the per-control-point :class:`Limit`s
    into the kW vocabulary the planner speaks, so the DP can plan against the
    hardware's real envelope instead of the configured one. Powers are
    magnitudes (always ≥ 0).

    ``None`` on any field means "no bound resolved" — either genuinely
    unconstrained or unreadable; ``degraded`` names the control points whose
    bound could not be read, so the panel and the integration check can tell
    those two apart.
    """
    max_battery_charge_kw: float | None
    max_battery_discharge_kw: float | None
    max_export_kw: float | None
    max_grid_discharge_kw: float | None
    resolved_at: datetime
    degraded: tuple[str, ...] = ()

    @staticmethod
    def reduce(configured: float | None, capable: float | None) -> float | None:
        """Return the binding value of a configured cap and a resolved capability.

        Capability may only *reduce* a configured cap, never raise it: the
        config expresses operator intent (or a deliberately conservative
        limit), while capability expresses a hardware ceiling. ``None`` on
        either side means that side does not constrain.

        Args:
            configured: The configured/planned cap in kW, or ``None``.
            capable: The resolved hardware ceiling in kW, or ``None``.

        Returns:
            The smaller of the two, or whichever one is not ``None``.
        """
        if configured is None:
            return capable
        if capable is None:
            return configured
        return min(configured, capable)


@dataclass
class BatteryState:
    """Current observed battery state."""
    soc: float                       # 0.0–1.0
    estimated_capacity_kwh: float    # Learned usable capacity


@dataclass(frozen=True)
class BatteryStatus:
    """Normalised battery snapshot: configured limits + current telemetry.

    Produced by `inbound/battery.py` from BatteryConfig + BatteryReading.
    Total capacity is the configured nominal value; remaining capacity is
    derived from the observed SoC.
    """
    total_capacity_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    soc: float                       # 0.0–1.0
    remaining_capacity_kwh: float    # soc * total_capacity_kwh


@dataclass(frozen=True)
class SolarForecast:
    """Predicted solar generation for one hour."""
    start: datetime
    end: datetime
    generation_kwh: float


@dataclass(frozen=True)
class ScheduleSlot:
    """One slot of the optimised battery schedule.

    ``mode`` is the Solis StorageMode the inverter should enter for this slot.
    The DP scheduler in ``pipeline/schedule.py`` picks it directly from per-slot
    physics (``pipeline/slot_physics.simulate_slot``).
    """
    start: datetime
    end: datetime
    mode: StorageMode          # forward ref — StorageMode is defined later in this file
    power_kw: float              # Energy exchanged in this slot (kWh at 1h resolution)
    expected_soc_after: float    # Predicted battery SoC at end of slot
    expected_profit_eur: float   # Profit (negative = cost) from this action
    reason: str                  # Human-readable explanation


@dataclass
class Schedule:
    """Complete battery optimization result."""
    slots: list[ScheduleSlot]
    total_expected_profit_eur: float
    degradation_cost_per_kwh: float
    computed_at: datetime


@dataclass(frozen=True)
class SchedulePolicy:
    """User-tunable flags + numeric knobs constraining the DP scheduler.

    Action-set toggles:
        ``use_standby`` — when False, StandBy is removed from the DP action set
            so the planner falls back to SelfUse during no-generation windows
            and the battery stays available to cover load.
        ``allow_grid_charging`` — when False, GridCharge is removed; the planner
            never force-charges the battery from the grid no matter how cheap
            import becomes.
        ``allow_feed_in`` — when False, FeedIn-priority is removed; export still
            happens through SelfUse when there is over-cap solar surplus.
        ``allow_discharge_to_grid`` — when False, the explicit Discharge-to-grid
            mode is removed; battery can still discharge to cover local load.

    Numeric knobs:
        ``mode_change_penalty_eur_per_kwh`` — EUR per storage-side kWh moved by
            the battery whenever the chosen mode differs from the previous
            slot's. Discourages flapping.
        ``profitability_tilt_alpha`` — strength of the profitability-score bias
            on end-SoC valuation; 0 disables the bias.
        ``terminal_value_discount`` — multiplier applied to the in-horizon
            median sell price when valuing end-of-horizon SoC; 0 disables
            terminal valuation entirely.
        ``export_limit_kw`` — deployment-wide export cap threaded to the DP's
            slot physics so the planner reserves battery headroom for over-cap
            solar instead of planning exports the inverter cannot deliver.
            ``None`` = uncapped (legacy behaviour, and the fallback for a bare
            ``SchedulePolicy()`` in ``ScheduleNode``).
        ``forecast_reserve_soc`` — extra SoC fraction the planner may not spend,
            sized from the measured spread of the day-ahead generation forecast.
            The DP sells battery *now* on the strength of solar it expects
            *later*; when that solar does not arrive the battery is empty and
            the shortfall is imported at peak. Holding back roughly one standard
            deviation of day-ahead forecast error bounds that exposure. Raises
            the DP's SoC floor only — never forces a charge. ``None`` = disabled.
        ``max_battery_charge_kw`` / ``max_battery_discharge_kw`` — the battery
            legs' live ceilings from ``InverterCapability``, already reduced
            against ``BatteryConfig``. ``ScheduleNode`` substitutes them into the
            config's power limits so the per-slot energy envelope in
            ``slot_physics`` reflects what the hardware accepts rather than what
            was configured. ``None`` = leave ``BatteryConfig`` untouched.
    """
    use_standby: bool = True
    allow_grid_charging: bool = True
    allow_feed_in: bool = True
    allow_discharge_to_grid: bool = True
    mode_change_penalty_eur_per_kwh: float = 0.005
    profitability_tilt_alpha: float = 0.5
    terminal_value_discount: float = 0.5
    max_discharge_to_grid_kw: float | None = None  # None → hardware max
    export_limit_kw: float | None = None  # None → uncapped
    max_battery_charge_kw: float | None = None     # None → use BatteryConfig
    max_battery_discharge_kw: float | None = None  # None → use BatteryConfig
    forecast_reserve_soc: float | None = None      # None → no reserve held


@dataclass(frozen=True)
class CapacityObservation:
    """One charge/discharge observation for capacity learning."""
    timestamp: datetime
    soc_start: float
    soc_end: float
    energy_kwh: float   # Measured energy throughput
    direction: str      # "charge" or "discharge"


# ---------------------------------------------------------------------------
# Pipeline stage data contracts (Pricing → Forecast → Calculator → Schedule)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceSlot:
    """Effective buy/sell prices for one time slot."""
    start: datetime
    end: datetime
    buy_eur_kwh: float    # effective buy price (spot + fees + tax)
    sell_eur_kwh: float   # effective sell price — can be NEGATIVE
    spot_eur_kwh: float   # raw import/spot price, kept for provenance
    sources: tuple[str, ...]  # (price_source, "tariff") — for diagnostics
    export_eur_kwh: float | None = None  # raw separate export feed (feed mode)


@dataclass(frozen=True)
class PriceSeries:
    """Normalised price stream for all known slots."""
    slots: tuple[PriceSlot, ...]
    resolution: timedelta
    computed_at: datetime

    def slot_at(self, t: datetime) -> PriceSlot | None:
        """Return the slot covering time t, or None if outside the series.

        Args:
            t: Timezone-aware datetime to look up.

        Returns:
            Matching PriceSlot, or None.
        """
        return next((s for s in self.slots if s.start <= t < s.end), None)

    def window(self, t1: datetime, t2: datetime) -> tuple[PriceSlot, ...]:
        """Return all slots that overlap the half-open interval [t1, t2).

        Args:
            t1: Start of the query window (inclusive).
            t2: End of the query window (exclusive).

        Returns:
            Tuple of overlapping PriceSlots, may be empty.
        """
        return tuple(s for s in self.slots if s.end > t1 and s.start < t2)


@dataclass(frozen=True)
class GenerationSlot:
    """One price-grid-aligned forecast slot.

    start/end mirror the pricing grid (1h or 15-min). expected_kwh is overlap-weighted
    when the raw forecast resolution differs from the grid.
    """
    start: datetime
    end: datetime
    expected_kwh: float


@dataclass(frozen=True)
class SolarEntry:
    """Solar generation for one time slot, any source."""
    start: datetime
    end: datetime
    expected_kwh: float
    source: str


@dataclass(frozen=True)
class GenerationSeries:
    """Output of the forecast stage. Deliverables:

    - slots: price-grid-aligned GenerationSlots covering yesterday 00:00 → tomorrow 23:59,
      one per pricing slot. Consumed by calculation (energy_between, per-slot expected solar
      for lockout logic).
    - today_remaining_kwh: sum of expected_kwh for today's slots with start >= now.
    - total_yesterday_kwh, total_today_kwh, total_tomorrow_kwh: full-day sums over the slot
      grid, sensor attributes only.
    - total_d2_kwh … total_d6_kwh: daily totals for days 2–6 ahead, summed directly from raw
      HA entity watts (outside the price grid, no per-slot data). Sensor attributes only.
    """
    slots: tuple[GenerationSlot, ...]
    total_yesterday_kwh: float = 0.0
    total_today_kwh: float = 0.0
    total_tomorrow_kwh: float = 0.0
    today_remaining_kwh: float = 0.0
    total_d2_kwh: float = 0.0
    total_d3_kwh: float = 0.0
    total_d4_kwh: float = 0.0
    total_d5_kwh: float = 0.0
    total_d6_kwh: float = 0.0

    def daily_totals_kwh(self, ndigits: int = 4) -> dict[str, float]:
        """Return the per-day generation totals keyed for serialization.

        Centralises the ``total_<day>_kwh`` dict built by the forecast
        diagnostic sensor and the debug view, so the day set is declared once.

        Args:
            ndigits: Decimal places to round each total to.

        Returns:
            Dict mapping ``total_yesterday_kwh`` / ``total_today_kwh`` /
            ``total_tomorrow_kwh`` / ``total_d2_kwh`` … ``total_d6_kwh`` to the
            rounded daily kWh totals.
        """
        return {
            "total_yesterday_kwh": round(self.total_yesterday_kwh, ndigits),
            "total_today_kwh": round(self.total_today_kwh, ndigits),
            "total_tomorrow_kwh": round(self.total_tomorrow_kwh, ndigits),
            "total_d2_kwh": round(self.total_d2_kwh, ndigits),
            "total_d3_kwh": round(self.total_d3_kwh, ndigits),
            "total_d4_kwh": round(self.total_d4_kwh, ndigits),
            "total_d5_kwh": round(self.total_d5_kwh, ndigits),
            "total_d6_kwh": round(self.total_d6_kwh, ndigits),
        }

    def energy_between(self, t1: datetime, t2: datetime) -> float:
        """Return expected kWh between t1 and t2 via proportional overlap.

        Args:
            t1: Start of query window (tz-aware).
            t2: End of query window (tz-aware).

        Returns:
            Sum of pro-rated expected_kwh across all overlapping slots.
        """
        total = 0.0
        for s in self.slots:
            overlap_start = max(s.start, t1)
            overlap_end = min(s.end, t2)
            if overlap_start >= overlap_end:
                continue
            slot_secs = (s.end - s.start).total_seconds()
            overlap_secs = (overlap_end - overlap_start).total_seconds()
            total += s.expected_kwh * (overlap_secs / slot_secs)
        return total


@dataclass(frozen=True)
class ObservedGenerationSlot:
    """Observed (measured) solar generation for one time slot."""
    start: datetime
    end: datetime
    generated_kwh: float
    source: str           # "inverter"


@dataclass(frozen=True)
class ObservedGenerationSeries:
    """Per-slot observed generation aligned to PriceSeries resolution.

    Covers yesterday 00:00 → now. Slots whose start is in the future of the
    last sample are simply absent (or zero where partial overlap occurs).
    """
    slots: tuple[ObservedGenerationSlot, ...]
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0


@dataclass(frozen=True)
class ForecastErrorSlot:
    """Per-slot forecast vs. observed solar generation delta.

    `error_kwh = observed_kwh - forecast_kwh`. Positive means the forecast
    under-predicted (actual generation exceeded the forecast); negative means
    it over-predicted. `relative_error` is `error_kwh / forecast_kwh` when
    the forecast is non-zero; None when the forecast was zero (relative error
    is undefined and would otherwise blow up the series-level MAPE).

    `censored` flags a slot whose observation is untrustworthy as a measure of
    *potential* generation because a no-export inverter regime was active and
    the slot under-generated — i.e. the panels were almost certainly curtailed
    (clipped) rather than the forecast being wrong. Censored slots keep their
    observed/error values for the chart but are excluded from the series-level
    statistics and from every EMA quality bucket. See `forecast_accuracy.py`.
    """
    start: datetime
    end: datetime
    forecast_kwh: float
    observed_kwh: float
    error_kwh: float
    relative_error: float | None
    censored: bool = False


@dataclass(frozen=True)
class ForecastErrorSeries:
    """Aligned forecast/observed error series over the overlap window.

    ``slots`` spans the whole forecast window, so it includes future slots that
    have no observation yet; those carry the ``-1.0`` sentinel in every
    observed-side field so a chart can draw "no data yet". **The aggregate
    statistics below are computed over the matched slots only.** That makes
    ``len(slots)`` and the statistics describe different populations — summing
    ``observed_kwh`` across ``slots`` yields nonsense (a 72 h window that is
    half in the future sums to a large negative number). ``matched_slot_count``
    is the denominator the statistics actually used; prefer it over
    ``len(slots)`` whenever dividing.
    """
    slots: tuple[ForecastErrorSlot, ...]
    total_forecast_kwh: float
    total_observed_kwh: float
    total_error_kwh: float           # sum of signed errors
    mean_absolute_error_kwh: float    # MAE per slot (mean of |error_kwh|)
    bias_kwh: float                    # mean signed error per slot
    mean_absolute_percentage_error: float | None    # MAPE (forecast-weighted); None when total_forecast_kwh == 0
    computed_at: datetime
    matched_slot_count: int = 0       # slots with a real observation (stats denominator)


@dataclass(frozen=True)
class SlotDecision:
    """Per-slot decision flags produced by the Calculator stage."""
    start: datetime
    end: datetime
    expected_solar_kwh: float       # expected generation this slot
    expected_solar_negative_sale_kwh: float  # production during negative-sale (reported only)
    notes: tuple[str, ...]          # e.g. ("battery_full_during_lockout", "paid_to_charge")


@dataclass(frozen=True)
class CalculationResult:
    """Output of the Calculator stage."""
    slots: tuple[SlotDecision, ...]
    feed_in_lockout_windows: tuple[tuple[datetime, datetime], ...]
    total_negative_sale_kwh: float  # sum across locked-out slots — reported, no decision
    computed_at: datetime


# ---------------------------------------------------------------------------
# Translation-layer primary types (HA state → domain, produced by translators)
# ---------------------------------------------------------------------------

@dataclass
class PriceFeedData:
    """Primary data: market prices for today + tomorrow from any price source.

    Source-agnostic output of every price-feed translator (Nordpool, ENTSO-e,
    Octopus, Amber, synthetic TOU). Entries carry the import price and an
    optional separate export price; resolution is detected from the data (or
    fixed by synthetic sources).
    """
    entries: list[PriceEntry]    # sorted by start; auto-detected resolution
    resolution: timedelta        # 15min or 1h, detected from data


# Back-compat alias: the DAG and persistence historically keyed on NordpoolData.
# Keeping it as the same class object means existing ``consumes`` declarations
# and stored payloads keep resolving with no migration.
NordpoolData = PriceFeedData


@dataclass(frozen=True)
class YesterdayPrices:
    """Primary data: yesterday's Nordpool prices loaded from persistent storage.

    Combined with NordpoolData by inbound/pricing to form the 72h PriceSeries.
    """
    entries: tuple[PriceEntry, ...]  # sorted by start; empty on first cycle


@dataclass
class SolarData:
    """Primary data: unified solar forecast for yesterday + today + tomorrow."""
    entries: list[SolarEntry]        # sorted by start, all panels combined
    total_today_kwh: float           # sum of expected_kwh for today's slots
    today_remaining_kwh: float       # sum of today's slots with start >= now
    primary_source: str              # "open_meteo" | "forecast_solar" | "none"


@dataclass(frozen=True)
class BatteryReading:
    """Primary data: raw inverter telemetry for one cycle.

    The two daily-resetting energy counters are optional: present on inverters
    that expose them (e.g. Solis ``today_battery_charge/discharge_energy``) and
    used by the capacity estimator to integrate cycle energy from the inverter's
    own authoritative totals instead of a noisy power integral. ``None`` when the
    inverter does not publish them — the estimator then simply does not learn and
    falls back to nominal capacity.
    """
    soc: float                  # 0.0–1.0
    power_kw: float             # positive = charging, negative = discharging
    grid_power_kw: float        # positive = importing
    household_load_kw: float    # current load in kW (0.2 kW default if unavailable)
    charge_energy_today_kwh: float | None = None     # daily cumulative AC charge energy
    discharge_energy_today_kwh: float | None = None  # daily cumulative AC discharge energy


@dataclass(frozen=True)
class PvPowerReading:
    """Primary data: one snapshot of instantaneous PV power in watts."""
    power_w: float
    timestamp: datetime


@dataclass(frozen=True)
class PvPowerHistory:
    """Primary data: ordered snapshots of instantaneous PV power.

    Coordinator appends each cycle's `PvPowerReading` and persists the
    rolling list (2 days) so per-slot kWh can be averaged over yesterday
    and today.
    """
    samples: tuple[PvPowerReading, ...]   # sorted by timestamp ascending


@dataclass(frozen=True)
class GenerationReading:
    """Primary data: one snapshot of the inverter's today-total kWh counter.

    The counter is cumulative and resets at local midnight. Used as the
    authoritative total for end-of-day correction of power-averaged slots.
    """
    today_total_kwh: float
    timestamp: datetime


@dataclass(frozen=True)
class GenerationHistory:
    """Primary data: ordered samples of the inverter today-total counter.

    Coordinator appends each cycle's `GenerationReading` and persists the
    rolling list (≥ 2 days) so the inbound module can difference samples
    across yesterday → now (fallback) and apply end-of-day correction.
    """
    samples: tuple[GenerationReading, ...]   # sorted by timestamp ascending


@dataclass(frozen=True)
class EstimatedCapacity:
    """Primary data: current CapacityEstimator result, set by coordinator pre-DAG."""
    value_kwh: float


@dataclass(frozen=True)
class HouseholdConsumptionReading:
    """Primary data: one snapshot of the inverter's household-consumption today-total kWh counter.

    The counter is cumulative and resets at local midnight; this is just the
    most recent observed value, so consumers can show "consumption so far
    today" without re-deriving it from instantaneous load samples.
    """
    today_total_kwh: float
    timestamp: datetime


@dataclass(frozen=True)
class ConsumptionDayRecord:
    """One local-date's hour-bucket consumption totals.

    Built once per local day from the day's ``ObservedConsumptionSeries`` —
    each ``hour_kwh[h]`` is the sum of ``consumed_kwh`` across the price-grid
    slots whose local start-hour equals ``h``. ``hour_completeness[h]`` is
    the fraction of expected price-grid slots in that hour that contributed
    at least one derived sample, so the baseload builder can drop hours
    where the inverter was offline.
    """
    local_date: date
    hour_kwh: tuple[float, ...]                 # length 24, kWh by local hour 0..23
    hour_completeness: tuple[float, ...]        # length 24, fraction in 0..1
    finalised_at: datetime                       # UTC timestamp when the record was written


@dataclass(frozen=True)
class ConsumptionDailyBuckets:
    """Primary data: rolling history of finalised per-day hour-bucket rollups.

    Coordinator appends one record per local date after the day ends and
    trims to ``CONSUMPTION_DAILY_WINDOW_DAYS``. Consumed by
    ``BaseLoadProfileNode`` to compute the per-hour P15 floor.
    """
    records: tuple[ConsumptionDayRecord, ...]   # sorted by local_date ascending


class DayClass(Enum):
    """Bucket for daily peak prices, ordered cheapest → most expensive on average."""
    WEEKEND = "weekend"     # Sat/Sun (and any holiday falling on these)
    HOLIDAY = "holiday"     # weekday public holiday
    WEEKDAY = "weekday"     # normal working day


@dataclass(frozen=True)
class DailyPeak:
    """One day's peak Nordpool spot price + its day-class bucket."""
    day: date
    peak_eur_kwh: float
    day_class: DayClass


@dataclass(frozen=True)
class PriceHistory:
    """Primary data: rolling history of daily peaks (≤ ~90 days)."""
    peaks: tuple[DailyPeak, ...]    # sorted by day ascending


@dataclass(frozen=True)
class ProfitabilityScore:
    """Secondary data: today's profitability relative to recent history.

    `score` is None when history is too sparse to be meaningful
    (< MIN_HISTORY_DAYS days in the rolling window).
    """
    score: float | None             # 0.0–1.0, percentile rank; None when sparse
    today_peak_eur_kwh: float
    today_class: DayClass
    class_medians: dict             # DayClass → median peak in long window
    window_days: int                # days actually used for percentile rank
    computed_at: datetime


# ---------------------------------------------------------------------------
# DAG secondary output wrapper types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DegradationCost:
    """Cost per kWh of cycling the battery (secondary output of DegradationNode)."""
    value_kwh: float


@dataclass(frozen=True)
class BaseLoadSlot:
    """Per-hour baseload floor for one local hour-of-day bucket.

    `is_fallback` is True when the bucket had fewer than MIN_BUCKET_SAMPLES
    observations and the value came from the profile's cross-bucket fallback.
    """
    hour: int                    # 0..23 in local time
    baseload_kw: float
    sample_count: int
    is_fallback: bool


@dataclass(frozen=True)
class BaseLoadProfile:
    """24-hour (local time) profile of estimated minimum household draw.

    Indexed by local hour 0..23. `slots` is always length 24 — sparse buckets
    receive `fallback_kw` so callers never have to handle None. `confidence`
    is None when total history covers fewer than MIN_HISTORY_DAYS local days.
    """
    slots: tuple[BaseLoadSlot, ...]    # length 24, indexed by hour
    fallback_kw: float                  # used for sparse buckets and as a stub
    overall_p10_kw: float               # diagnostic: P10 of all samples
    overall_median_kw: float            # diagnostic
    confidence: float | None            # 0..1, distinct_days / window_days; None when sparse
    sample_count: int                   # total samples in window
    distinct_days: int                  # distinct local-date days in window
    computed_at: datetime

    def at(self, t: datetime, local_tz: tzinfo) -> float:
        """Return the baseload floor kW for the local hour containing t.

        Args:
            t: Any tz-aware datetime.
            local_tz: Timezone used to map t to a local hour-of-day bucket.

        Returns:
            Estimated minimum household draw in kW for that hour.
        """
        return self.slots[t.astimezone(local_tz).hour].baseload_kw


@dataclass(frozen=True)
class BatteryRuntimeEstimate:
    """How long the battery can sustain household baseload before hitting min_soc.

    Forward-simulates net drain (baseload − forecast solar) from `now` over
    `horizon_hours`. Intentionally does NOT account for the schedule node's
    scheduled discharge/charge — the output is a "baseload-only reserve".
    `runtime_minutes` and `until` are None when the battery never drains
    within the horizon (e.g. forecast solar covers baseload).
    """
    remaining_kwh_usable: float         # max(0, (soc − min_soc) * total_capacity)
    avg_drain_kw_next_hour: float       # mean net_drain over the first simulated hour
    runtime_minutes: float | None
    until: datetime | None              # now + runtime_minutes, tz-aware
    horizon_hours: int
    computed_at: datetime


# ---------------------------------------------------------------------------
# Structured integration config (used by DAG engine and nodes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SunSaleConfig:
    """All user configuration, structured for DAG nodes.

    `local_tz` defaults to UTC for tests / installs without HA's timezone
    set; the coordinator populates it from `hass.config.time_zone` at setup.

    `latitude` / `longitude` come from `hass.config` and drive the solar-position
    geometry behind the clear-sky index. They are optional because HA allows an
    unset home location; consumers must degrade gracefully rather than assume a
    site, since a wrong coordinate silently mis-bins every accuracy sample.
    """
    tariff: TariffConfig
    battery: BatteryConfig
    local_tz: tzinfo = field(default=UTC)
    price_source: str = "nordpool"  # provenance tag for PriceSlot.sources
    currency: str = "EUR"           # display-only currency code (UI labels/panel)
    latitude: float | None = None
    longitude: float | None = None
    # Opt-in: hold battery back against day-ahead forecast error. Off by
    # default because it is dispatch-affecting and sized from the install's own
    # measured error, which needs weeks of honest history to be meaningful.
    forecast_reserve_enabled: bool = False


# ---------------------------------------------------------------------------
# Forecast quality — EMA running state and persistent store
# ---------------------------------------------------------------------------


@dataclass
class AccuracyBucketState:
    """Mutable EMA running state for one forecast quality bucket.

    Updated in-place each time a new (forecast, observed) pair falls into
    this bucket. All EMA fields are in kWh units; display conversion happens
    at serialization time (×1000 → Wh).
    """
    ema_error: float = 0.0       # mean signed error — drives Bias metric
    ema_abs_error: float = 0.0   # mean |error| — drives MAE metric
    ema_sq_error: float = 0.0    # mean error² — drives RMSE metric (sqrt at display)
    ema_obs: float = 0.0         # mean observed — needed for R² denominator
    ema_obs_sq: float = 0.0      # mean observed² — needed for R² denominator
    n: int = 0                   # total samples absorbed into EMA

    def metrics(self) -> dict:
        """Compute the quality metrics from current EMA state.

        MAPE is deliberately absent. On PV it is dominated by near-zero slots —
        a 0.36 kWh dawn slot missed by 0.86 kWh reports 239 % — so the aggregate
        tracks how many dim slots the window happened to contain rather than how
        good the forecast is. Bias/MAE/RMSE carry the same information in kWh,
        where an error's size means something.

        Returns:
            Dict with keys n, bias_wh, mae_wh, rmse_wh, r2. Fields other than n
            are None when n == 0. ``r2`` is additionally None when observed
            variance is ~0 (a constant series has no variance to explain) and is
            clamped at -9.99 below, since arbitrarily negative values only say
            "far worse than predicting the mean".
        """
        if self.n == 0:
            return {"n": 0, "bias_wh": None, "mae_wh": None,
                    "rmse_wh": None, "r2": None}
        bias_wh = round(self.ema_error * 1000, 2)
        mae_wh  = round(self.ema_abs_error * 1000, 2)
        rmse_wh = round(math.sqrt(max(0.0, self.ema_sq_error)) * 1000, 2)
        obs_var = self.ema_obs_sq - self.ema_obs ** 2
        if obs_var > 1e-12:
            r2 = round(max(-9.99, min(1.0, 1.0 - self.ema_sq_error / obs_var)), 4)
        else:
            r2 = None
        return {"n": self.n, "bias_wh": bias_wh, "mae_wh": mae_wh,
                "rmse_wh": rmse_wh, "r2": r2}


@dataclass(frozen=True)
class SunTimes:
    """Approximate sunrise and sunset datetimes for today, derived from sun.sun.

    Both fields are UTC-aware. None when the sun.sun entity is unavailable or
    the times cannot be derived from next_rising / next_setting attributes.
    """
    today_sunrise: datetime | None
    today_sunset: datetime | None


@dataclass(frozen=True)
class ArrayCalibration:
    """Effective PV array geometry fitted from measured generation.

    The clear-sky index needs a denominator: what this array *would* produce
    under a cloudless sky. Rather than asking the user for kWp/tilt/azimuth
    (which they usually do not know accurately, and which existing installs
    would have to be re-prompted for), these are fitted from observed output
    against computed solar position.

    ``kwp_eff`` is deliberately an *effective* capacity: it absorbs module
    efficiency, inverter losses, wiring and the clear-sky model's own absolute
    calibration. It is therefore not comparable to a nameplate rating — only to
    itself over time, and to ``implied_forecast_kwp``.

    Attributes:
        kwp_eff: Fitted effective peak capacity in kW.
        tilt_deg: Fitted array tilt from horizontal.
        azimuth_deg: Fitted array facing, clockwise from true north.
        fit_quality: 1 − (residual spread / mean) over the clear slots used;
            1.0 is a perfect fit, ≤ 0 means the geometry explains nothing.
        n_days: Number of distinct clear-ish days contributing to the fit.
        n_slots: Number of slots contributing to the fit.
        implied_forecast_kwp: The capacity implied by the *forecast provider's*
            own peak output over the same geometry. Comparing this against
            ``kwp_eff`` is the whole point: a large gap means the configured
            array in the forecast provider is the wrong size.
        correction_factor: ``kwp_eff / implied_forecast_kwp`` — what the
            forecast would need to be multiplied by. ``None`` when the forecast
            peak is too small to divide by.
        confidence: 0–1 readiness score combining fit quality and sample count.
            Consumers must refuse to act on a low-confidence calibration.
        computed_at: When the fit was last recomputed.
    """
    kwp_eff: float
    tilt_deg: float
    azimuth_deg: float
    fit_quality: float
    n_days: int
    n_slots: int
    implied_forecast_kwp: float | None = None
    correction_factor: float | None = None
    confidence: float = 0.0
    computed_at: datetime | None = None


@dataclass(frozen=True)
class SolarHealth:
    """Verdict on whether the array is physically under-performing its own past.

    Compares the *ceiling* of the clear-sky index over a recent window against
    an older baseline. Cloud only ever pushes the index down, so the ceiling
    estimates what the array can physically do while the mean would mostly
    measure the weather.

    Deliberately independent of the generation forecast: it needs the error
    distribution to be stationary, not centred, so it stays informative even
    while the forecast has no skill.

    Attributes:
        status: ``"ok"``, ``"degraded"``, or ``"unknown"``. ``"unknown"`` means
            not enough clear weather or history to judge — never an all-clear.
        recent_ceiling: Best clear-sky index in the recent window.
        baseline_ceiling: Reference ceiling from the older window.
        ratio: ``recent_ceiling / baseline_ceiling``; below ~0.8 reads as
            soiling, snow, new shading or a dead string.
        recent_days: Days contributing to the recent ceiling.
        baseline_days: Days contributing to the baseline.
        computed_at: When this verdict was formed.
    """
    status: str
    recent_ceiling: float | None
    baseline_ceiling: float | None
    ratio: float | None
    recent_days: int
    baseline_days: int
    computed_at: datetime | None = None


# Bumped when persisted quality state stops being interpretable under the
# current rules. v2 replaced the absolute-intensity and slot-position axes with
# physically normalised ones (clear-sky index, solar elevation, solar azimuth)
# and introduced the per-slot ingestion watermark. Everything below v2 is
# dropped except the horizon buckets (forecast_accuracy.store_from_dict).
FORECAST_QUALITY_STORE_VERSION = 2


@dataclass
class ForecastQualityStore:
    """Persistent quality store — mutable EMA state for every accuracy axis.

    The per-slot axes are keyed on *physical* coordinates rather than on raw
    magnitude or slot index. Binning by absolute forecast kWh conflates "low
    because it is dawn" with "low because it is overcast" — unrelated failure
    modes with unrelated fixes — so each axis below isolates one cause:

    csi_bins: keyed by clear-sky-index band start in whole percent ("0", "10",
            … "100"). The "100" bucket is an **overflow** holding every slot at
            or above 100 %: a well-populated overflow means the array beat the
            clear-sky model built from its own declared capacity, which is the
            signature of a declared capacity that is too small.
    elevation_bins: keyed by solar-elevation band start in whole degrees
            ("0", "5", …). Isolates air-mass and low-sun model error.
    azimuth_bins: keyed by solar-azimuth band start in whole degrees
            ("0", "15", …). A fixed obstruction — tree, chimney, neighbouring
            roof — shows up as a persistent deficit in specific bearings.
    horizon: keyed by day-ahead horizon as str ("0"–"6"). d0/d1 are the only
            horizons the DP can act on; the price series caps planning at ~32 h,
            so d2–d6 are informational.
    horizon_pending: unresolved day-ahead forecast records awaiting actual
            generation data. Each entry is a dict with keys target_date
            (ISO str), horizon (int), forecast_kwh (float).
    last_ingested_slot_utc: ISO timestamp of the newest per-slot sample already
            absorbed. The DAG re-runs every coordinator cycle over the same
            3-day error window, so without this watermark each slot is re-fed
            ~288×/day — which collapses an α=0.1 EMA's effective memory from
            months to minutes. Slots at or before this mark are skipped. The
            horizon buckets need no watermark: they commit once per
            (target_date, horizon) by construction.
    version: store schema version. A freshly constructed store is current by
            definition; only a *persisted* payload below the current version is
            legacy, so the migration check reads the dict, not this default.
    """
    csi_bins: dict[str, AccuracyBucketState] = field(default_factory=dict)
    elevation_bins: dict[str, AccuracyBucketState] = field(default_factory=dict)
    azimuth_bins: dict[str, AccuracyBucketState] = field(default_factory=dict)
    horizon: dict[str, AccuracyBucketState] = field(default_factory=dict)
    horizon_pending: list[dict] = field(default_factory=list)
    last_ingested_slot_utc: str | None = None
    version: int = FORECAST_QUALITY_STORE_VERSION


@dataclass(frozen=True)
class GridImportPowerReading:
    """Primary data: instantaneous grid-import power magnitude (kW, ≥ 0).

    Produced by ``GridImportPowerObserver``. At any real-world moment only
    one of import / export power is non-zero — grid flow is one-way.
    """
    power_kw: float
    timestamp: datetime


@dataclass(frozen=True)
class GridExportPowerReading:
    """Primary data: instantaneous grid-export power magnitude (kW, ≥ 0).

    Produced by ``GridExportPowerObserver``. Mirror of
    ``GridImportPowerReading``.
    """
    power_kw: float
    timestamp: datetime


@dataclass(frozen=True)
class GridImportPowerHistory:
    """Primary data: rolling samples of grid-import power (kW, ≥ 0).

    Coordinator appends each cycle's ``GridImportPowerReading`` and persists
    the rolling list (2 days). Feeds the ``grid_import`` side of the
    observed-series engine directly — no sign-split required.
    """
    samples: tuple[GridImportPowerReading, ...]


@dataclass(frozen=True)
class GridExportPowerHistory:
    """Primary data: rolling samples of grid-export power (kW, ≥ 0).

    Mirror of ``GridImportPowerHistory`` for the export direction.
    """
    samples: tuple[GridExportPowerReading, ...]


@dataclass(frozen=True)
class GridImportTodayReading:
    """Primary data: one snapshot of the inverter's today-total imported-kWh counter.

    Cumulative kWh imported from the grid since local midnight; resets at
    local midnight. Used as the authoritative daily total for end-of-day
    correction of power-derived import slots.
    """
    today_total_kwh: float
    timestamp: datetime


@dataclass(frozen=True)
class GridImportTodayHistory:
    """Primary data: rolling samples of the today-total imported-kWh counter."""
    samples: tuple[GridImportTodayReading, ...]   # sorted by timestamp ascending


@dataclass(frozen=True)
class GridExportTodayReading:
    """Primary data: one snapshot of the inverter's today-total exported-kWh counter.

    Cumulative kWh exported to the grid since local midnight; resets at
    local midnight. Used as the authoritative daily total for end-of-day
    correction of power-derived export slots.
    """
    today_total_kwh: float
    timestamp: datetime


@dataclass(frozen=True)
class GridExportTodayHistory:
    """Primary data: rolling samples of the today-total exported-kWh counter."""
    samples: tuple[GridExportTodayReading, ...]   # sorted by timestamp ascending


@dataclass(frozen=True)
class ObservedGridSlot:
    """Observed (measured) grid flow for one price-grid-aligned time slot.

    `imported_kwh` and `exported_kwh` are gross flows: positive grid-power
    samples accumulate into `imported_kwh`, negative samples into
    `exported_kwh`. Both are non-negative; a slot that imported 30 min and
    exported 30 min reports non-zero values on both sides.
    """
    start: datetime
    end: datetime
    imported_kwh: float
    exported_kwh: float
    source: str           # "inverter"


@dataclass(frozen=True)
class ObservedGridSeries:
    """Per-slot observed grid import/export aligned to PriceSeries resolution.

    Covers two LOCAL days back through now (day-before-yesterday 00:00 →
    now). The extra day beyond "yesterday" is required by `MonthlyBillNode`
    so its day-rollover bake-in window stays inside the series. Today's
    slots are scaled at end-of-day so their sums match the inverter's
    today-total import/export counters (when present). Yesterday and the
    older day are left unscaled; `total_yesterday_*` and `total_today_*`
    aggregate those two days specifically — older slots are present in
    `slots` but not summed.
    """
    slots: tuple[ObservedGridSlot, ...]
    computed_at: datetime
    total_yesterday_imported_kwh: float = 0.0
    total_yesterday_exported_kwh: float = 0.0
    total_today_imported_kwh: float = 0.0
    total_today_exported_kwh: float = 0.0


# ---------------------------------------------------------------------------
# Observed-series engine contracts (bake-in + snapshot persistence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotKwh:
    """Single side's non-negative kWh for one price-grid slot.

    Used as both the raw per-cycle engine output (today) and the persisted
    baked output (yesterday and older). The wrapping record carries the side
    identity and provenance; this dataclass is identity-free per slot.
    """
    start: datetime
    end: datetime
    kwh: float


@dataclass(frozen=True)
class BakedDayRecord:
    """Per-day, per-side outcome of the once-per-day bake-in operation.

    Captures both the inputs and outputs of the bake so integration checks can
    surface divergence: `counter_total_used` is the inverter-side authoritative
    total we baked against, `baked_sum` is the sum of `baked_slots`. When
    `source_kind == "failed_no_source"` the slots equal the raw averaged
    values and `counter_total_used` is 0.0 (no comparison is possible).
    """
    date_str: str               # local ISO date "YYYY-MM-DD" the slots cover
    side_id: str                # matches Side.id in the engine
    counter_total_used: float    # authoritative daily total used for bake
    source_kind: str            # "dedicated_sensor" | "snapshot" | "failed_no_source"
    baked_slots: tuple[SlotKwh, ...]
    baked_sum: float            # sum of baked_slots kwh, after rounding
    baked_at: datetime          # UTC timestamp when this record was written


@dataclass(frozen=True)
class BakedObservedHistory:
    """All baked day records across all sides, sorted by (date_str, side_id).

    Coordinator persists the whole tuple under a single storage key and trims
    older records on each save according to retention policy.
    """
    records: tuple[BakedDayRecord, ...]


@dataclass(frozen=True)
class CounterSnapshotRecord:
    """One pre-rollover snapshot of a single side's today-total counter.

    Captured within the rollover window (configurable, typically last 30 min
    of the local day) so the next-day bake-in has an authoritative value when
    no dedicated yesterday-total sensor is configured for the side.
    """
    side_id: str
    captured_at: datetime       # UTC timestamp of capture
    today_total_kwh: float


@dataclass(frozen=True)
class CounterSnapshotHistory:
    """Rolling history of pre-rollover counter snapshots across all sides.

    Coordinator appends within the rollover window each cycle and trims older
    snapshots on save.
    """
    records: tuple[CounterSnapshotRecord, ...]


# ---------------------------------------------------------------------------
# Derived-power observers — consumption (home load) and inverter losses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcPortPowerReading:
    """Primary data: instantaneous AC grid-port power (kW, signed).

    Sign convention: **positive = inverter→grid** (matches the raw Solis
    ``ac_grid_port_power`` register). Distinct from the sunSale-convention
    signed grid_power_net (positive = import) — these two should be roughly
    mirror images when no battery activity is happening.
    """
    power_kw: float          # signed; positive = inverter→grid
    timestamp: datetime


@dataclass(frozen=True)
class BackupPowerReading:
    """Primary data: instantaneous backup-port output power (kW, ≥ 0).

    Always non-negative — backup port only sources power (it cannot sink).
    Reads ~0 whenever the grid is up; non-zero only when the inverter is
    bridging backup-protected loads during an outage.
    """
    power_kw: float          # ≥ 0
    timestamp: datetime


@dataclass(frozen=True)
class DerivedPowerSample:
    """One synchronised cross-stream snapshot feeding the derived observers.

    Captured each coordinator cycle by composing the cycle's individual
    primary readings into a single tuple-typed sample. Persisted as a rolling
    history so the engine can average the derived-power formulas per slot
    independently of when individual primaries become available later.

    All fields are in kW. Sign conventions follow the codebase:

      * ``ac_port_kw_signed`` — positive = inverter→grid (raw Solis convention).
      * ``backup_kw`` — magnitude ≥ 0.
      * ``grid_net_kw_signed`` — positive = import (sunSale convention).
      * ``solar_kw`` — magnitude ≥ 0.
      * ``battery_kw_signed`` — positive = charging (sunSale convention).

    The derived formulas (clamped ≥ 0) are:

      * consumption_kw = backup + ac_port_signed + grid_net_signed
      * losses_kw      = solar  − battery_signed − ac_port_signed − backup
    """
    timestamp: datetime
    ac_port_kw_signed: float
    backup_kw: float
    grid_net_kw_signed: float
    solar_kw: float
    battery_kw_signed: float


@dataclass(frozen=True)
class DerivedPowerHistory:
    """Rolling history of cross-stream derived-power samples.

    Coordinator appends each cycle (only when *all* component readings are
    present this cycle — partial samples are skipped to avoid biasing the
    slot mean with incomplete data) and persists ~2 days.
    """
    samples: tuple[DerivedPowerSample, ...]   # sorted by timestamp ascending


@dataclass(frozen=True)
class ObservedConsumptionSlot:
    """Observed (derived) household-consumption for one price-grid slot."""
    start: datetime
    end: datetime
    consumed_kwh: float
    source: str           # "inverter_derived"


@dataclass(frozen=True)
class ObservedConsumptionSeries:
    """Per-slot derived household-consumption aligned to PriceSeries resolution.

    Covers yesterday 00:00 local → now. Today's slots are raw averages of the
    cycle-derived consumption formula; yesterday's slots come from
    ``BakedObservedHistory`` when a record exists (Phase-3 bake-in not yet
    wired for this side — yesterday currently stays raw too).
    """
    slots: tuple[ObservedConsumptionSlot, ...]
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0


@dataclass(frozen=True)
class ObservedLossesSlot:
    """Observed (derived) inverter conversion losses for one price-grid slot."""
    start: datetime
    end: datetime
    losses_kwh: float
    source: str           # "inverter_derived"


@dataclass(frozen=True)
class ObservedLossesSeries:
    """Per-slot derived inverter losses aligned to PriceSeries resolution.

    Covers yesterday 00:00 local → now. No bake-in source exists for losses
    (the inverter doesn't expose a "losses today total" counter), so slots
    stay raw indefinitely — comparison against an authoritative total is not
    possible.
    """
    slots: tuple[ObservedLossesSlot, ...]
    computed_at: datetime
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0


@dataclass(frozen=True)
class InverterTimeReading:
    """One paired snapshot of the inverter clock and HA's UTC clock.

    The inverter clock is reported as a local-time datetime — the translator
    attaches HA's local timezone and converts to UTC before constructing the
    reading. ``skew = inverter_now - ha_now`` (positive means inverter is
    ahead of HA).
    """
    ha_now: datetime          # UTC
    inverter_now: datetime    # UTC, normalised from the inverter's local-time entity


@dataclass(frozen=True)
class MonthlyBillState:
    """Persistent per-day finalized-cost ledger for the monthly electricity bill.

    ``finalized_days`` maps a completed local date ("YYYY-MM-DD") to that day's
    net electricity cost in EUR. A day's entry is (re)computed every cycle while
    the day is still inside the live ``PriceSeries`` window — i.e. while it is
    "yesterday" — so the value converges onto the bake-in-corrected total before
    being frozen. Once the price window rolls past the day, the entry is no
    longer recomputed and stays frozen at its last value.

    The carry (month-start → live-window start) and the previous-month total are
    *derived* from this ledger each cycle rather than accumulated once, which
    makes both self-healing across restarts and transient empty cycles. Entries
    older than the previous calendar month are pruned.

    Stored as a tuple of ``(date_str, cost)`` pairs to keep the state hashable
    and immutable, matching the frozen-dataclass convention used elsewhere.
    """
    finalized_days: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class BillSlot:
    """Per-pricing-slot electricity cost breakdown.

    net_cost_eur = imported_kwh * buy_eur_kwh - exported_kwh * sell_eur_kwh.
    No floor is applied to sell_eur_kwh — negative sell prices charge the
    exporter, matching real-market behaviour. Positive net_cost_eur means
    net cost; negative means net revenue.
    """
    start: datetime
    end: datetime
    imported_kwh: float    # net energy bought from grid this slot
    exported_kwh: float    # net energy sold to grid this slot
    buy_eur_kwh: float     # effective buy price
    sell_eur_kwh: float    # effective sell price (may be negative)
    net_cost_eur: float    # cost to the household this slot


@dataclass(frozen=True)
class MonthlyBillResult:
    """Net electricity bill accumulated for the current calendar month.

    Covers from month_start to now, split into a carry (the completed days from
    month_start up to the live-window start, summed from the per-day ledger in
    MonthlyBillState) and a live portion derived from grid power history + price
    series. Slots span the live window only — they never include data attributed
    to a previous month. previous_month_eur is the sum of the previous calendar
    month's ledger entries so the UI can keep showing it after rollover.

    today_cost_eur and yesterday_cost_eur are convenience daily breakdowns
    computed directly over [today_start, now) and [yday_start, today_start)
    respectively — independent of the month-carry split, so they stay correct
    across a month boundary (e.g. on the 1st, yesterday_cost_eur still reports
    the previous month's last day). They drive the daily-cost sensors.
    """
    slots: tuple[BillSlot, ...]      # live-window slots (yday_start or month_start → now)
    carry_eur: float                  # bill from month_start to start of yesterday
    yday_to_now_eur: float           # sum of slot net_cost_eur (live-window total)
    total_month_eur: float           # carry_eur + yday_to_now_eur
    month_str: str                    # current month "YYYY-MM"
    previous_month_str: str           # finalized prior month, "" before first rollover
    previous_month_eur: float         # bill total for previous_month_str
    updated_state: MonthlyBillState  # coordinator persists this after each cycle
    computed_at: datetime
    today_cost_eur: float = 0.0       # net cost for today so far (today_start → now)
    yesterday_cost_eur: float = 0.0   # net cost for the full previous local day


@dataclass
class ForecastAccuracyResult:
    """Single output of ForecastAccuracyNode: per-cycle slot errors + persistent EMA quality.

    error_series carries the aligned forecast/observed deltas for this cycle.
    quality carries the updated persistent EMA buckets across all three groups
    and is saved to storage by the coordinator after each cycle.
    """
    error_series: ForecastErrorSeries
    quality: ForecastQualityStore


# ---------------------------------------------------------------------------
# Inverter mode control — Solis storage-mode state machine
# (target taxonomy defined in docs/solis_control.md §3)
# ---------------------------------------------------------------------------


class StorageMode(Enum):
    """Semantic operating states the scheduler and operator reason about.

    Platform-neutral: each mode is materialised into concrete hardware targets
    by the inverter driver (Solis: ``outbound/storage_mode_specs.py``), not here.
    UNKNOWN is reserved for observed states that map to no planned mode. The
    inline ``43110=…`` notes below are the Solis encoding, for reference only.
    """
    FeedIn     = "feed_in"      # 43110=64 — surplus above export cap → charge
    SelfUse    = "self_use"     # 43110=1  — charge first, capped export of surplus
    NoExport   = "no_export"    # 43110=1  (SelfUse) — export prohibited
    Discharge  = "discharge"    # 43110=64 (FeedIn) — force discharge, export at deployment cap
    GridCharge = "grid_charge"  # 43110=33 (SelfUse|GridCharge) — force grid charge
    StandBy    = "stand_by"     # 43110=1  (SelfUse) — no battery flow, no grid exchange
    TRACK      = "track"        # real-time VPP setpoint follower (future hook)
    UNKNOWN    = "unknown"      # observed bitmask does not map to any named mode


# The canonical set of modes the system actively dispatches, in a fixed order.
# This is the single source of truth shared by the DP planner's action set
# (``pipeline/schedule.py``) and the operator override options
# (``select.py``) — keeping them in lockstep so a new mode can't be added to
# one surface while the other silently diverges.
#
# Excluded from the set, by design:
#   - UNKNOWN : observed-only label, never a target.
#   - TRACK   : needs per-cycle setpoint plumbing not yet exposed.
DISPATCHABLE_MODES: tuple[StorageMode, ...] = (
    StorageMode.SelfUse,
    StorageMode.NoExport,
    StorageMode.StandBy,
    StorageMode.GridCharge,
    StorageMode.Discharge,
    StorageMode.FeedIn,
)


# NOTE: the concrete per-mode register targets (formerly ``StorageModeSpec``)
# live in ``outbound/storage_mode_specs.py`` — Solis hardware detail kept out of
# this platform-neutral contract. The scheduler reasons in ``StorageMode``; only
# the inverter driver materialises a spec.


@dataclass(frozen=True)
class InverterModeReading:
    """Primary data: one-cycle snapshot of observed inverter state.

    All hardware-derived fields are nullable so the driver's ``observe`` stays
    resilient to entity-unavailability. ``mode`` is decoded by the inverter
    driver; it falls back to ``StorageMode.UNKNOWN`` when the raw state cannot
    be read or maps to no known mode.

    ``raw_state`` is a platform-defined integer code for the observed raw mode
    state — the value the driver decodes ``mode`` from (Solis: the register
    43110 bitmask). It is surfaced for diagnostics and used as the "raw state is
    readable" sentinel (``None`` ⇒ unavailable, history is not appended). Its
    encoding is the driver's concern, not the consumer's.

    ``backflow_power_w`` is the configured export-power cap. It is the
    discriminator between SelfUse (cap > 0) and NoExport (cap = 0) under the
    shared SelfUse state — Solis Cloud EMS suppresses export by toggling this
    single value, and sunSale's ``apply_mode`` writes it the same way, so it is
    the authoritative observable signal.

    ``rc_engaged`` is whether the RC active-power selector is engaged at the AC
    grid port. It separates Discharge from FeedIn under the shared FeedIn state:
    the RC setpoint register holds its last value in RAM after the function
    expires, so the selector — not the stale setpoint — is the authoritative
    "is RC actually acting" signal. ``None`` when unmapped or unreadable.
    """
    timestamp: datetime
    raw_state: int | None
    mode: StorageMode
    charge_a: float | None
    discharge_a: float | None
    rc_setpoint_w: int | None
    backflow_power_w: int | None = None
    rc_engaged: bool | None = None


@dataclass(frozen=True)
class InverterModeChange:
    """One persisted entry in the inverter-mode history.

    Appended only when the decoded mode differs from the previous entry; the
    timestamp is the moment the change was first observed by the coordinator.
    ``raw_state`` is the platform-defined raw mode-state code at that instant
    (see :class:`InverterModeReading`).
    """
    timestamp: datetime
    mode: StorageMode
    raw_state: int


@dataclass(frozen=True)
class InverterModeHistory:
    """Primary data: rolling history of mode-change events.

    Samples cover ``local_midnight(yesterday) → now``; older entries are
    pruned at each cycle. Strictly mode-change events — no consecutive entries
    share the same ``mode``.
    """
    samples: tuple[InverterModeChange, ...]   # sorted ascending by timestamp
