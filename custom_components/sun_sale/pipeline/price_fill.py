"""Forecast fill for the slots the market has not priced, and its settlement.

The day-ahead auction publishes tomorrow in the early afternoon, so for most of
each day the back half of the 72h slot grid carries no market price at all — and
a dead price feed can leave *today* in the same state (live, 2026-09-20 and
again 2026-09-23). Those slots have always existed so the generation and
observed series have a grid to land on, but they held a fabricated ``0.0`` spot,
which is not merely unknown: run through the tariff formula it becomes a
plausible-looking cheap buy and a negative sell, and the panel drew it as a flat
real price line.

This module replaces that zero with the price-forecast engine's own prediction
for the day and marks the slot ``forecast=True`` — still ``priced=False``, so
nothing that books a fact can touch it, but good enough for the planner to
optimise over. The mark is the whole point: it travels with the slot to the
sensor and the panel, which draws a forecast-filled stretch dashed.

The fill is *not* a decision that sticks. When the real prices for the day
arrive they overwrite it wholesale — there is nothing to merge, the market is
simply right — and the same moment banks the hourly forecast-vs-actual error
into :attr:`PriceCurveHistory.errors`. That is the feedback the engine's
confidence is scaled by and the panel draws as the price forecast error, and
banking it on override rather than at the next local midnight is what makes it
appear the afternoon the auction publishes instead of a day late.

Pure Python — no Home Assistant imports, no third-party deps.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from ..contract.models import (
    PriceCurveHistory,
    PriceErrorPoint,
    PriceSeries,
    PriceSlot,
    TariffConfig,
)
from . import online_shape
from . import tariff as tariff_module

# Provenance tag added to a slot the engine filled, so the sources tuple says
# where the number came from without anyone having to read the flags.
SOURCE_FORECAST = "forecast"


def fill_from_forecast(
    series: PriceSeries,
    curves: Mapping[date, Sequence[float]],
    tariff: TariffConfig,
    local_tz: tzinfo | None = None,
    is_holiday: Callable[[date], bool] | None = None,
) -> PriceSeries:
    """Fill the unpriced slots from the engine's predicted curve for their day.

    Only slots that carry no market price are touched, and only for days the
    engine has a full 24-hour curve for. A real price is never overwritten, and
    a day with no prediction keeps its bare placeholders — an honest zero that
    nothing may read beats a guess nobody can attribute.

    The predicted spot goes through the same tariff formula a real slot does,
    so the filled buy and sell obey the site's own grid-fee bands, season and
    holiday rules and are directly comparable with the priced slots beside
    them.

    Args:
        series: The assembled 72h series, with placeholders where the feed had
            nothing.
        curves: Predicted hourly spot prices per local day — 24 values each,
            in clock order. Days with any other length are ignored.
        tariff: User tariff, applied to the predicted spot.
        local_tz: HA local timezone; UTC when None.
        is_holiday: Public-holiday predicate for the tariff's holiday bands.

    Returns:
        A series with the fillable placeholders replaced, or ``series``
        unchanged when there was nothing to fill.
    """
    usable = {
        day: curve for day, curve in curves.items()
        if len(curve) == online_shape.HOURS
    }
    if not usable:
        return series

    tz = local_tz or UTC
    slots: list[PriceSlot] = []
    changed = False
    for slot in series.slots:
        if slot.priced or slot.forecast:
            slots.append(slot)
            continue
        local_start = slot.start.astimezone(tz)
        curve = usable.get(local_start.date())
        if curve is None:
            slots.append(slot)
            continue
        spot = curve[local_start.hour]
        slots.append(replace(
            slot,
            spot_eur_kwh=spot,
            buy_eur_kwh=tariff_module.buy_price(spot, tariff, local_start, is_holiday),
            sell_eur_kwh=tariff_module.sell_price(
                spot, tariff, local_start, is_holiday=is_holiday,
            ),
            sources=(*slot.sources, SOURCE_FORECAST),
            forecast=True,
        ))
        changed = True

    if not changed:
        return series
    return replace(series, slots=tuple(slots))


def bank_fill_errors(
    history: PriceCurveHistory,
    price_series: PriceSeries | None,
    today: date,
    local_tz: tzinfo | None,
    retention_days: int,
) -> PriceCurveHistory | None:
    """Bank the hourly error for every predicted day the market has now priced.

    A day qualifies once it has a frozen prediction *and* a full 24 hours of
    real prices — which for tomorrow is the afternoon its auction publishes,
    hours before it would settle into the history at midnight. Each day is
    banked once; re-running the cycle changes nothing.

    The frozen prediction is what the engine said at issue time, so the error
    banked here is the honest out-of-sample one, not a hindsight re-run. The
    prediction itself is left in place — ``price_forecast.settle_days`` still
    consumes it into the settled record's ``predicted_curve``.

    Args:
        history: Current history.
        price_series: This cycle's prices, or None.
        today: Local date of the cycle.
        local_tz: Local timezone.
        retention_days: Days of banked error to keep, counted back from today.

    Returns:
        Updated history, or None when nothing changed.
    """
    if price_series is None:
        return None

    tz = local_tz or UTC
    cutoff = today - timedelta(days=retention_days)
    covered = history.error_days()
    priced = price_series.priced_slots

    added: list[PriceErrorPoint] = []
    for day, prediction in sorted(history.prediction_by_day().items()):
        if day < cutoff or day in covered:
            continue
        if len(prediction.curve) != online_shape.HOURS:
            continue
        actual = online_shape.curve_from_slots(
            list(_slots_for_day(priced, day, tz)), day, tz,
        )
        if actual is None:
            continue
        for hour, (real, forecast) in enumerate(zip(actual, prediction.curve)):
            added.append(PriceErrorPoint(
                start=datetime.combine(day, time(hour=hour), tzinfo=tz),
                forecast_eur_kwh=forecast,
                actual_eur_kwh=real,
                error_eur_kwh=real - forecast,
            ))

    kept = tuple(e for e in history.errors if e.start.date() >= cutoff)
    if not added and len(kept) == len(history.errors):
        return None
    return replace(
        history,
        errors=tuple(sorted([*kept, *added], key=lambda e: e.start)),
    )


def _slots_for_day(
    slots: Sequence[PriceSlot], day: date, local_tz: tzinfo,
) -> tuple[PriceSlot, ...]:
    """Return the slots whose local start falls on ``day``.

    Args:
        slots: Slots to filter.
        day: Local date wanted.
        local_tz: Local timezone.

    Returns:
        The matching slots, in input order.
    """
    return tuple(s for s in slots if s.start.astimezone(local_tz).date() == day)
