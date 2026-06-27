"""Tariff formula: convert Nordpool spot prices to effective buy/sell prices.

Distribution fees are time-of-use: a weekday and a weekend schedule of
:class:`TariffBand`s each carry their own buy/sell distribution fee, selected
per slot from the slot's local time-of-day and day-of-week. Tax rates and
markups are global. When a schedule is empty the flat fallback fees on the
:class:`TariffConfig` apply, preserving the pre-TOU behaviour.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from ..contract.models import TariffBand, TariffConfig


def _parse_hhmm(value: Any) -> int | None:
    """Parse an "HH:MM" local time-of-day string to minutes-from-midnight.

    Args:
        value: A string like "06:30"; anything else returns None.

    Returns:
        Minutes from local midnight (0–1439), or None when unparseable / out of range.
    """
    if not isinstance(value, str) or ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        minute = int(hh) * 60 + int(mm)
    except ValueError:
        return None
    return minute if 0 <= minute <= 1439 else None


def bands_from_config(raw_bands: Sequence[Any] | None) -> tuple[TariffBand, ...]:
    """Build a sorted TariffBand tuple from stored config dicts.

    Each entry is a ``{"start": "HH:MM", "buy_fee": float, "sell_fee": float}``
    dict. Entries with a missing/invalid start time are dropped; the result is
    sorted by start time.

    Args:
        raw_bands: Stored band list from the config entry, or None.

    Returns:
        Tuple of TariffBand sorted by start_minute (empty when none are valid).
    """
    bands: list[TariffBand] = []
    for entry in raw_bands or ():
        if not isinstance(entry, dict):
            continue
        start = _parse_hhmm(entry.get("start"))
        if start is None:
            continue
        try:
            buy = float(entry.get("buy_fee", 0.0))
            sell = float(entry.get("sell_fee", 0.0))
        except (TypeError, ValueError):
            continue
        bands.append(TariffBand(start_minute=start, distribution_fee=buy, sell_distribution_fee=sell))
    return tuple(sorted(bands, key=lambda b: b.start_minute))


def _bands_for_day(config: TariffConfig, local_dt: datetime) -> tuple[TariffBand, ...]:
    """Return the band schedule applicable to local_dt's day type.

    Args:
        config: Tariff configuration holding the two schedules.
        local_dt: Slot start projected into the HA local timezone.

    Returns:
        The weekend schedule on Sat/Sun, otherwise the weekday schedule.
    """
    return config.weekend_bands if local_dt.weekday() >= 5 else config.weekday_bands


def select_band(config: TariffConfig, local_dt: datetime) -> TariffBand | None:
    """Pick the active TOU band for a local time, wrapping past midnight.

    The band whose start is the latest at or before ``local_dt``'s minute-of-day
    wins. Before the earliest band's start the previous day's last band still
    applies, so the schedule covers 24h with no gaps regardless of ordering.

    Args:
        config: Tariff configuration holding the band schedules.
        local_dt: Slot start projected into the HA local timezone.

    Returns:
        The applicable TariffBand, or None when the day's schedule is empty.
    """
    bands = _bands_for_day(config, local_dt)
    if not bands:
        return None
    minute = local_dt.hour * 60 + local_dt.minute
    ordered = sorted(bands, key=lambda b: b.start_minute)
    chosen = ordered[-1]  # wrap: before the first start the last band carries over
    for band in ordered:
        if band.start_minute <= minute:
            chosen = band
        else:
            break
    return chosen


def buy_distribution_fee(config: TariffConfig, local_dt: datetime | None) -> float:
    """Resolve the buy-side distribution fee for a slot.

    Args:
        config: Tariff configuration.
        local_dt: Slot start in local time, or None to force the flat fallback.

    Returns:
        The active band's buy distribution fee, or the flat fallback.
    """
    if local_dt is not None:
        band = select_band(config, local_dt)
        if band is not None:
            return band.distribution_fee
    return config.distribution_fee


def sell_distribution_fee(config: TariffConfig, local_dt: datetime | None) -> float:
    """Resolve the sell-side distribution fee for a slot.

    Args:
        config: Tariff configuration.
        local_dt: Slot start in local time, or None to force the flat fallback.

    Returns:
        The active band's sell distribution fee, or the flat fallback.
    """
    if local_dt is not None:
        band = select_band(config, local_dt)
        if band is not None:
            return band.sell_distribution_fee
    return config.sell_distribution_fee


def buy_price(spot: float, config: TariffConfig, local_dt: datetime | None = None) -> float:
    """Compute total cost to buy 1 kWh from the grid.

    Formula: (spot + distribution_fee + markup) * (1 + tax_rate), where the
    distribution fee is the TOU band's fee for ``local_dt`` (flat fallback when
    ``local_dt`` is None or no band matches).

    Args:
        spot: Nordpool spot price in EUR/kWh.
        config: User-configured tariff parameters.
        local_dt: Slot start in local time for band selection; None → flat fee.

    Returns:
        Effective buy price in EUR/kWh.
    """
    dist = buy_distribution_fee(config, local_dt)
    return (spot + dist + config.markup) * (1.0 + config.tax_rate)


def sell_price(
    spot: float,
    config: TariffConfig,
    local_dt: datetime | None = None,
    export: float | None = None,
) -> float:
    """Compute net revenue from selling 1 kWh to the grid.

    Branches on ``config.sell_mode``:

    - ``"spot"`` (default): ``(spot − sell_distribution_fee − sell_markup)·
      (1 − sell_tax_rate)``, the distribution fee resolved from the TOU band for
      ``local_dt`` (flat fallback when ``local_dt`` is None or no band matches).
    - ``"fixed"``: the flat ``fixed_sell_price`` (feed-in tariff).
    - ``"schedule"``: the active TOU band's ``sell_distribution_fee`` reinterpreted
      as an **absolute** sell price (avoided-cost schedule, e.g. NEM 3.0); falls
      back to ``fixed_sell_price`` when no band matches.
    - ``"feed"``: the per-slot ``export`` price from a separate live export feed;
      falls back to ``fixed_sell_price`` when the slot has no export price.

    Args:
        spot: Import/spot price in EUR/kWh.
        config: User-configured tariff parameters.
        local_dt: Slot start in local time for band selection; None → flat fee.
        export: Per-slot live export price (EUR/kWh) for ``feed`` mode; None when
            the source has no separate export feed.

    Returns:
        Effective sell price in EUR/kWh; can be negative.
    """
    mode = config.sell_mode
    if mode == "fixed":
        return config.fixed_sell_price
    if mode == "schedule":
        if local_dt is not None:
            band = select_band(config, local_dt)
            if band is not None:
                return band.sell_distribution_fee
        return config.fixed_sell_price
    if mode == "feed":
        return export if export is not None else config.fixed_sell_price
    dist = sell_distribution_fee(config, local_dt)
    return (spot - dist - config.sell_markup) * (1.0 - config.sell_tax_rate)
