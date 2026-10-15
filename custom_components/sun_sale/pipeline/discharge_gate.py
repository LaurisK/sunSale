"""Discharge gate: the running sell-price average the planner sells above.

Pure Python — no Home Assistant imports.

One small piece consumed by :func:`pipeline.schedule.optimize_schedule`:

* **Running sell average.** One persisted figure that moves once per local day
  to ``0.9 × previous + 0.1 × yesterday's realized sell price`` — the kWh-weighted
  mean sell price of the slots in which the battery really discharged to the grid
  (:func:`inbound.observer.battery_trade.realized_trade_prices`; the daily fold
  itself, :func:`fold_daily_value`, is shared with the precharge's buy-price
  average). A day with no such slot leaves it unchanged, and with no history at
  all it starts at the highest known sell price. With the gate on, the
  planner offers Discharge-to-grid only in slots selling at or above it, so
  stored energy waits for the better part of the price curve. The same switch
  makes the planner assume a stronger sun than forecast (the overfill boost) —
  that is a scaling of the DP's solar input in ``schedule.py``, not state kept
  here.
"""
from __future__ import annotations

from datetime import date

from ..contract.models import RunningSellAverage

# Weight of the previous average in the daily update; the day's realized mean gets the rest.
RUNNING_AVERAGE_KEEP = 0.9

# Price comparisons tolerate float noise so a fixed sell price, whose running
# average equals it to within a rounding error, never gates its own slots out.
PRICE_EPS = 1e-9


def fold_daily_value(
    previous: float | None, realized: float | None, seed: float | None = None,
) -> float | None:
    """Return a running price average folded with one day's realized mean.

    Args:
        previous: The average before this fold, or ``None`` on a cold start
            (the realized mean then seeds it).
        realized: The kWh-weighted mean price the battery traded at on the day
            being folded, or ``None`` when it did not trade.
        seed: Used only when there is neither ``previous`` nor ``realized`` —
            the best-known price at the favourable end (lowest buy / highest
            sell), so a fresh install starts from a bar only a good slot clears.

    Returns:
        The folded average; ``previous`` unchanged when there is no realized
        mean; ``seed`` (possibly ``None``) when there is no history at all.
    """
    if previous is None:
        return realized if realized is not None else seed
    if realized is None:
        return previous
    return RUNNING_AVERAGE_KEEP * previous + (1.0 - RUNNING_AVERAGE_KEEP) * realized


def fold_running_average(
    stored: RunningSellAverage | None,
    realized: float | None,
    today: date,
    seed: float | None = None,
) -> RunningSellAverage | None:
    """Return the running sell average as of local ``today``.

    The update happens once per local day: a stored figure already stamped
    ``today`` (or later) is returned as the same object, so the caller can tell
    "unchanged" by identity and persist only when the day moved on. ``today``
    folds in *yesterday's* trading, the last complete day.

    Args:
        stored: The persisted average, or ``None`` on a cold start.
        realized: Yesterday's realized mean sell price, or ``None`` when the
            battery sold nothing to the grid.
        today: Local date the figure is wanted for.
        seed: The highest sell price currently known, used only when nothing is
            stored and nothing was ever sold.

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
    return RunningSellAverage(value_eur_kwh=value, day=today)
