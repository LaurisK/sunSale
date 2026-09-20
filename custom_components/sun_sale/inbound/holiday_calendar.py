"""Public-holiday lookup for the profitability day classes.

``pipeline/profitability.py:classify_day`` buckets weekday public holidays into
their own day class (demand, and so the price peak, looks like a weekend). This
module supplies the ``is_holiday(date) -> bool`` predicate it takes, from the
``holidays`` package and the country Home Assistant is configured for
(``hass.config.country``, carried as ``SunSaleConfig.holiday_country``).

Every failure degrades to "no holidays" — exactly the behaviour before holidays
were wired — rather than failing the cycle: no country configured, the package
missing, or a country the package has no calendar for.

Pure Python: no Home Assistant imports (``holidays`` is a plain library).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from functools import lru_cache

_LOGGER = logging.getLogger(__name__)

HolidayPredicate = Callable[[date], bool]


@lru_cache(maxsize=8)
def holiday_predicate(country: str | None) -> HolidayPredicate | None:
    """Return ``is_holiday`` for a country's national public holidays, or None.

    Cached per country, so the profitability node can ask every cycle; the
    calendar itself fills in further years lazily as dates are checked.

    The first call for a country imports ``holidays``, which reads files from
    disk — blocking work that Home Assistant forbids on the event loop. Warm
    the cache from an executor with :func:`preload_holidays` before any DAG
    cycle can reach it; every later call is a cache hit and is loop-safe.

    Args:
        country: ISO 3166-1 alpha-2 code (e.g. ``"LT"``), or None when Home
            Assistant has no country configured.

    Returns:
        A predicate that is True on a public holiday, or None when no calendar
        is available (the caller then classifies without holidays).
    """
    if not country:
        return None
    try:
        import holidays  # noqa: PLC0415 — optional at import time; absent means "no holidays"
    except ImportError:
        _LOGGER.warning("The 'holidays' package is not installed; holidays are not classified")
        return None
    try:
        calendar = holidays.country_holidays(country.upper())
    except NotImplementedError:
        _LOGGER.info("No public-holiday calendar for country %s; holidays are not classified", country)
        return None
    return calendar.__contains__


def preload_holidays(country: str | None) -> None:
    """Warm the per-country calendar cache; run this in an executor.

    Populates :func:`holiday_predicate`'s cache so the first DAG cycle does not
    pay for the ``holidays`` import — a blocking disk read — on the event loop.
    Failures are already swallowed by ``holiday_predicate`` itself, so this
    never raises.

    Args:
        country: ISO 3166-1 alpha-2 code, or None to do nothing.
    """
    holiday_predicate(country)
