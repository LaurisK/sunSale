"""Price level — class every price slot as cheap / normal / expensive.

sunSale publishes the result to Home Assistant (a *Price level* sensor plus
*Cheap price* / *Expensive price* binary sensors) so automations and other
integrations can act on the current price — sunSale itself controls nothing
with it. Works on any install with prices, with or without an inverter.

Each slot's **effective buy price** is ranked within its **local** day. The
cheapest ``cheap_share`` of the day's slots are cheap, the dearest
``expensive_share`` expensive, the rest normal. Optional absolute limits
override the rank (at or below ``cheap_below`` → always cheap, at or above
``expensive_above`` → always expensive). A slot that falls in both rank shares
is normal: with no spread there is nothing to shift towards.

Step-shaped prices (time-of-use schedules, hourly markets on a 15-min grid)
tie many slots at the share boundary. Equal prices must share one class, so a
tie group joins the class only when at least half of it falls inside the share;
otherwise the threshold stops just short of it. Without this, a 10-hour
shoulder band would be labelled "expensive" because 2 of its hours reach into
the dearest quarter.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from datetime import date, datetime, tzinfo
from typing import Any

from ..contract.const import (
    CONF_PRICE_LEVEL_CHEAP_BELOW,
    CONF_PRICE_LEVEL_CHEAP_SHARE,
    CONF_PRICE_LEVEL_EXPENSIVE_ABOVE,
    CONF_PRICE_LEVEL_EXPENSIVE_SHARE,
    DEFAULT_PRICE_LEVEL_SHARE_PCT,
)
from ..contract.models import (
    PriceLevel,
    PriceLevelConfig,
    PriceLevelDay,
    PriceLevelSeries,
    PriceLevelSlot,
    PriceSeries,
    PriceSlot,
)


def config_from_entry(data: Mapping[str, Any]) -> PriceLevelConfig:
    """Build the price-level config from merged config-entry data + options.

    Args:
        data: Config dict; shares are stored as percentages, limits may be
            absent or ``None``.

    Returns:
        The config, defaulting to the cheapest / dearest 25 % with no limits
        (so entries created before the setting existed need no migration).
    """
    def _share(key: str) -> float:
        """Return a stored percentage as a 0–1 fraction, clamped."""
        pct = data.get(key)
        pct = DEFAULT_PRICE_LEVEL_SHARE_PCT if pct is None else float(pct)
        return min(max(pct, 0.0), 100.0) / 100.0

    def _limit(key: str) -> float | None:
        """Return an optional absolute limit, or None when unset."""
        value = data.get(key)
        return None if value in (None, "") else float(value)

    return PriceLevelConfig(
        cheap_share=_share(CONF_PRICE_LEVEL_CHEAP_SHARE),
        expensive_share=_share(CONF_PRICE_LEVEL_EXPENSIVE_SHARE),
        cheap_below=_limit(CONF_PRICE_LEVEL_CHEAP_BELOW),
        expensive_above=_limit(CONF_PRICE_LEVEL_EXPENSIVE_ABOVE),
    )


def _share_count(n: int, share: float) -> int:
    """Return how many of ``n`` slots a share covers (rounded up, 0 for a 0 share)."""
    if share <= 0:
        return 0
    return min(n, math.ceil(n * share - 1e-9))


def day_thresholds(prices: list[float], config: PriceLevelConfig) -> tuple[float | None, float | None]:
    """Return one day's (cheap, expensive) rank thresholds.

    Args:
        prices: The day's buy prices, **sorted ascending**.
        config: Shares to apply.

    Returns:
        The highest price inside the cheap share and the lowest inside the
        expensive share, after the half-a-tie-group rule (module docstring);
        either is ``None`` when its share covers no whole class of prices.
    """
    n = len(prices)
    cheap_n = _share_count(n, config.cheap_share)
    expensive_n = _share_count(n, config.expensive_share)

    cheap = None
    if cheap_n:
        edge = prices[cheap_n - 1]
        lo, hi = bisect_left(prices, edge), bisect_right(prices, edge)
        # Too little of the boundary tie group is inside: stop below it.
        if (cheap_n - lo) * 2 >= hi - lo:
            cheap = edge
        elif lo > 0:
            cheap = prices[lo - 1]

    expensive = None
    if expensive_n:
        edge = prices[n - expensive_n]
        lo, hi = bisect_left(prices, edge), bisect_right(prices, edge)
        if (hi - (n - expensive_n)) * 2 >= hi - lo:
            expensive = edge
        elif hi < n:
            expensive = prices[hi]
    return cheap, expensive


def level_for(
    buy: float, cheap_threshold: float | None, expensive_threshold: float | None, config: PriceLevelConfig,
) -> PriceLevel:
    """Return the level of one buy price given its day's thresholds.

    Args:
        buy: The slot's effective buy price.
        cheap_threshold: The day's cheap rank threshold (or None).
        expensive_threshold: The day's expensive rank threshold (or None).
        config: Supplies the optional absolute limits.

    Returns:
        The absolute limit's class when exactly one applies; otherwise the rank
        class, with a slot in both rank shares counted as normal.
    """
    abs_cheap = config.cheap_below is not None and buy <= config.cheap_below
    abs_expensive = config.expensive_above is not None and buy >= config.expensive_above
    if abs_cheap != abs_expensive:
        return PriceLevel.CHEAP if abs_cheap else PriceLevel.EXPENSIVE
    rank_cheap = cheap_threshold is not None and buy <= cheap_threshold
    rank_expensive = expensive_threshold is not None and buy >= expensive_threshold
    if rank_cheap and not rank_expensive:
        return PriceLevel.CHEAP
    if rank_expensive and not rank_cheap:
        return PriceLevel.EXPENSIVE
    return PriceLevel.NORMAL


def classify_price_levels(
    series: PriceSeries, config: PriceLevelConfig, local_tz: tzinfo, now: datetime,
) -> PriceLevelSeries:
    """Class every slot of ``series`` as cheap / normal / expensive.

    Args:
        series: The effective price series (buy prices include fees and tax).
        config: Shares and optional absolute limits.
        local_tz: HA's timezone — days are local days, so the ranking matches
            the day a household sees.
        now: Cycle timestamp, stamped as ``computed_at``.

    Returns:
        One ``PriceLevelSlot`` per *priced* price slot (same order) and one
        ``PriceLevelDay`` per local date covered. Slots with no known market
        price are absent — they have no level to rank.
    """
    # Unpriced slots hold a filler zero, which would both take the "cheapest"
    # rank itself and drag every real slot's rank within its day.
    ranked = series.priced_slots
    by_day: dict[date, list[PriceSlot]] = {}
    for slot in ranked:
        by_day.setdefault(slot.start.astimezone(local_tz).date(), []).append(slot)

    days: list[PriceLevelDay] = []
    classed: dict[datetime, PriceLevelSlot] = {}
    for day, slots in sorted(by_day.items()):
        prices = sorted(s.buy_eur_kwh for s in slots)
        cheap, expensive = day_thresholds(prices, config)
        days.append(PriceLevelDay(date=day, slot_count=len(prices), cheap_threshold=cheap,
                                  expensive_threshold=expensive))
        for s in slots:
            # Fraction of the day's other slots that are strictly cheaper.
            rank = bisect_left(prices, s.buy_eur_kwh) / (len(prices) - 1) if len(prices) > 1 else 0.5
            classed[s.start] = PriceLevelSlot(
                start=s.start, end=s.end, day=day, buy_eur_kwh=s.buy_eur_kwh,
                level=level_for(s.buy_eur_kwh, cheap, expensive, config), rank=rank,
            )

    return PriceLevelSeries(
        slots=tuple(classed[s.start] for s in ranked),
        days=tuple(days),
        config=config,
        computed_at=now,
    )


def next_start_of(series: PriceLevelSeries, t: datetime, level: PriceLevel) -> datetime | None:
    """Return when ``level`` next begins after the slot covering ``t``.

    Args:
        series: The classified series.
        t: Timezone-aware lookup time.
        level: The level to look for.

    Returns:
        Start of the first later slot with that level that follows a slot of a
        different level (i.e. the start of its next run), or None.
    """
    current = series.slot_at(t)
    if current is None:
        return None
    previous = current.level
    for slot in series.slots:
        if slot.start < current.end:
            continue
        if slot.level is level and previous is not level:
            return slot.start
        previous = slot.level
    return None


def _round(value: float | None) -> float | None:
    """Round a price to 4 decimals, passing None through."""
    return None if value is None else round(value, 4)


def price_level_status(series: PriceLevelSeries, t: datetime) -> dict[str, Any] | None:
    """Return the current price status as a flat, JSON-safe dict.

    This is what the HA entities publish (state + attributes) and what the debug
    view reports as ``current``, so both always agree.

    Args:
        series: The classified series.
        t: Timezone-aware lookup time (normally now).

    Returns:
        ``level``, ``buy_price``, ``rank_pct`` (0 = day's cheapest, 100 =
        dearest), the slot bounds, the slot's day thresholds, and when the level
        next changes (``next_change`` / ``next_level``, None if it lasts to the
        end of the known prices). None when ``t`` is outside the series.
    """
    slot = series.slot_at(t)
    if slot is None:
        return None
    day = series.day(slot.day)
    nxt = series.next_change(t)
    return {
        "level": slot.level.value,
        "buy_price": _round(slot.buy_eur_kwh),
        "rank_pct": round(slot.rank * 100),
        "slot_start": slot.start.isoformat(),
        "slot_end": slot.end.isoformat(),
        "cheap_threshold": _round(day.cheap_threshold) if day else None,
        "expensive_threshold": _round(day.expensive_threshold) if day else None,
        "next_change": nxt.start.isoformat() if nxt else None,
        "next_level": nxt.level.value if nxt else None,
    }
