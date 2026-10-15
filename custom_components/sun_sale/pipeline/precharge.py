"""Precharge: hold a target SoC when the sun will not reach it and prices dip.

Pure Python — no Home Assistant imports.

The buy-side counterpart of the discharge gate, built the same way: nothing here
forces a slot. This module only decides whether the precharge is *armed*;
:func:`pipeline.schedule.optimize_schedule` then plans normally, reads the peak
SoC that plan reaches, and if it falls short of the target buys the deficit in
the cheapest slots the plan left unused, as long as they are under the buy
average.

* **Running buy average.** One persisted figure, folded once per local day to
  ``0.9 × previous + 0.1 × yesterday's realized buy price`` — the kWh-weighted
  mean buy price of the slots in which the battery really charged from the grid
  (:func:`inbound.observer.battery_trade.realized_trade_prices`); a day with none
  leaves it unchanged, and with no history at all it starts at the lowest known
  buy price. The same fold as the sell average
  (:func:`pipeline.discharge_gate.fold_daily_value`).
* **Trigger.** Two facts here, both required: the forecast sun alone (see
  :func:`pipeline.schedule.sun_only_peak_soc`) does not lift the battery to the
  target within the look-ahead days, and a *confirmed* price slot still ahead
  buys below the average. An estimated (forecast-filled) price never counts
  as the trigger, so a predicted dip cannot start a purchase. The third fact —
  the plain plan's own peak SoC is under the target — is read in the scheduler,
  because only it has the plan.
"""
from __future__ import annotations

from datetime import date, datetime, tzinfo

from ..contract.models import (
    BaseLoadProfile,
    BatteryConfig,
    BatteryState,
    GenerationSeries,
    Precharge,
    PriceSeries,
    RunningBuyAverage,
)
from .discharge_gate import PRICE_EPS, fold_daily_value
from .schedule import sun_only_peak_soc


def fold_running_buy_average(
    stored: RunningBuyAverage | None,
    realized: float | None,
    today: date,
    seed: float | None = None,
) -> RunningBuyAverage | None:
    """Return the running buy average as of local ``today``.

    Same contract as :func:`pipeline.discharge_gate.fold_running_average`: a
    stored figure already stamped ``today`` (or later) comes back as the same
    object, so the caller persists only when the day moved on.

    Args:
        stored: The persisted average, or ``None`` on a cold start.
        realized: Yesterday's realized mean buy price, or ``None`` when the
            battery bought nothing from the grid.
        today: Local date the figure is wanted for.
        seed: The lowest buy price currently known, used only when nothing is
            stored and nothing was ever bought.

    Returns:
        The average stamped ``today``; the unchanged ``stored`` when it is
        already current, and ``None`` while there is no history and no price.
    """
    if stored is not None and stored.day >= today:
        return stored
    value = fold_daily_value(
        stored.value_eur_kwh if stored is not None else None, realized, seed,
    )
    if value is None:
        return stored
    return RunningBuyAverage(value_eur_kwh=value, day=today)


def plan_precharge(
    average: RunningBuyAverage | None,
    price_series: PriceSeries,
    generation: GenerationSeries,
    base_load_profile: BaseLoadProfile,
    battery_config: BatteryConfig,
    battery_state: BatteryState,
    now: datetime,
    local_tz: tzinfo,
    days: int,
    target_soc: float,
) -> Precharge:
    """Decide whether the planner should hold ``target_soc`` at the horizon end.

    Args:
        average: The running buy-price average, or ``None`` before it is seeded.
        price_series: Prices; only confirmed slots (``priced_slots``) that end
            after ``now`` can trigger.
        generation: Forecast series (slots to tomorrow 24:00, daily totals to d6).
        base_load_profile: Household baseload per local hour.
        battery_config: Battery limits; the target is capped at ``max_soc``.
        battery_state: Current SoC and estimated usable capacity.
        now: Cycle timestamp.
        local_tz: HA local timezone.
        days: Days of forecast solar to look at.
        target_soc: Wanted SoC fraction.

    Returns:
        The decision. ``active`` is False whenever the sun reaches the target
        (or the forecast gives no basis), no confirmed slot buys below the
        average, or there is no average yet; ``average`` is always carried.
    """
    target = min(max(target_soc, 0.0), battery_config.max_soc)
    peak = sun_only_peak_soc(
        generation, base_load_profile, battery_config, battery_state,
        now, local_tz, days,
    )
    if peak is None or peak >= target:
        return Precharge(average=average, target_soc=target, peak_soc=peak)
    shortfall = (target - peak) * battery_state.estimated_capacity_kwh
    cheap_confirmed = average is not None and any(
        s.end > now and s.buy_eur_kwh < average.value_eur_kwh - PRICE_EPS
        for s in price_series.priced_slots
    )
    return Precharge(
        average=average, active=cheap_confirmed, target_soc=target,
        peak_soc=peak, shortfall_kwh=shortfall,
    )
