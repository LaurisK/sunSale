"""Discharge gate: the running sell-price average the planner sells above.

Pure Python — no Home Assistant imports.

One small piece consumed by :func:`pipeline.schedule.optimize_schedule`:

* **Running sell average.** One persisted figure that moves once per local day
  to ``0.9 × previous + 0.1 × today's mean sell price`` (the daily fold itself,
  :func:`fold_daily_value`, is shared with the precharge's buy-price average). With the gate on, the
  planner offers Discharge-to-grid only in slots selling at or above it, so
  stored energy waits for the better part of the price curve. The same switch
  makes the planner assume a stronger sun than forecast (the overfill boost) —
  that is a scaling of the DP's solar input in ``schedule.py``, not state kept
  here.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, tzinfo

from ..contract.models import PriceSeries, PriceSlot, RunningSellAverage

# Weight of the previous average in the daily update; today's mean gets the rest.
RUNNING_AVERAGE_KEEP = 0.9

# Price comparisons tolerate float noise so a fixed sell price, whose running
# average equals it to within a rounding error, never gates its own slots out.
PRICE_EPS = 1e-9


def fold_daily_value(
    previous: float | None,
    price_series: PriceSeries,
    today: date,
    local_tz: tzinfo,
    price_of: Callable[[PriceSlot], float],
) -> float | None:
    """Return a running price average folded with local ``today``'s mean.

    Args:
        previous: The average before today, or ``None`` on a cold start (today's
            mean then seeds it).
        price_series: Prices for the planning window; only today's priced slots
            are read, so a forecast-filled placeholder never moves the average.
        today: Local date the figure is wanted for.
        local_tz: Timezone that decides which slots belong to ``today``.
        price_of: Which price of a slot is averaged (sell or buy).

    Returns:
        The folded average, or ``None`` when today has no priced slot yet.
    """
    prices = [
        price_of(s) for s in price_series.priced_slots
        if s.start.astimezone(local_tz).date() == today
    ]
    if not prices:
        return None
    mean = sum(prices) / len(prices)
    if previous is None:
        return mean
    return RUNNING_AVERAGE_KEEP * previous + (1.0 - RUNNING_AVERAGE_KEEP) * mean


def fold_running_average(
    stored: RunningSellAverage | None,
    price_series: PriceSeries,
    today: date,
    local_tz: tzinfo,
) -> RunningSellAverage | None:
    """Return the running sell average as of local ``today``.

    The update happens once per local day: a stored figure already stamped
    ``today`` (or later) is returned as the same object, so the caller can tell
    "unchanged" by identity and persist only when the day moved on. With no
    stored figure, today's mean seeds it.

    Args:
        stored: The persisted average, or ``None`` on a cold start.
        price_series: Prices for the planning window; only today's priced slots
            are read, so a forecast-filled placeholder never moves the average.
        today: Local date the figure is wanted for.
        local_tz: Timezone that decides which slots belong to ``today``.

    Returns:
        The average for ``today``; the unchanged ``stored`` when it is current or
        when today has no priced slot yet (``None`` if there was nothing stored).
    """
    if stored is not None and stored.day >= today:
        return stored
    value = fold_daily_value(
        stored.value_eur_kwh if stored is not None else None,
        price_series, today, local_tz, lambda s: s.sell_eur_kwh,
    )
    if value is None:
        return stored
    return RunningSellAverage(value_eur_kwh=value, day=today)
