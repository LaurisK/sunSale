"""Tariff formula: turn a slot's market price into effective buy/sell prices.

Buy and sell are each one :class:`PriceFormula`, configured independently:

    buy  = (energy + markup + grid_fee) × (1 + vat)
    sell = (energy − markup − grid_fee) × (1 − vat)

``energy`` is the slot's market price (dynamic) or a fixed price. The grid fee
has 1, 2 or 4 tariffs; with more than one, a switch-point schedule per season
(optional summer / winter) and day type (workday, optional weekend, optional
public holiday) says which tariff is active at the slot's local time of day.

Also parses the stored formula dicts and converts entries that still hold the
legacy flat-fee / fee-band / sell-mode keys, so no config-entry migration is
needed.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from ..contract.const import (
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
    CONF_PRICE_SOURCE,
    CONF_PRICE_TOU_BANDS,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_FIXED_SELL_PRICE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_MODE,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CONF_TARIFF_WEEKDAY_BANDS,
    CONF_TARIFF_WEEKEND_BANDS,
    DAY_HOLIDAY,
    DAY_WEEKEND,
    DAY_WORKDAY,
    DEFAULT_SUMMER_START,
    DEFAULT_WINTER_START,
    ENERGY_DYNAMIC,
    ENERGY_FIXED,
    FORMULA_ENERGY,
    FORMULA_FIXED_PRICE,
    FORMULA_GRID_FEES,
    FORMULA_HOLIDAYS,
    FORMULA_MARKUP,
    FORMULA_SCHEDULES,
    FORMULA_SEASONS,
    FORMULA_SUMMER_START,
    FORMULA_TARIFFS,
    FORMULA_VAT,
    FORMULA_WEEKENDS,
    FORMULA_WINTER_START,
    PRICE_SOURCE_TOU,
    SEASON_ALL,
    SEASON_SUMMER,
    SEASON_WINTER,
    TARIFF_COUNTS,
)
from ..contract.models import DaySchedule, PriceFormula, TariffConfig, TariffSwitch

_LOGGER = logging.getLogger(__name__)

HolidayPredicate = Callable[[date], bool]

# Its season starts are the fallbacks for unparseable stored dates.
_DEFAULT_FORMULA = PriceFormula()

# Every schedule a formula can hold, in form order (season-major).
ALL_SCHEDULE_KEYS = tuple(
    f"{season}_{day}"
    for season in (SEASON_ALL, SEASON_SUMMER, SEASON_WINTER)
    for day in (DAY_WORKDAY, DAY_WEEKEND, DAY_HOLIDAY)
)

# Keys the legacy tariff stored; dropped from an entry once it holds formulas.
LEGACY_PRICE_KEYS = (
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_TAX_RATE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_MODE,
    CONF_TARIFF_FIXED_SELL_PRICE,
    CONF_TARIFF_WEEKDAY_BANDS,
    CONF_TARIFF_WEEKEND_BANDS,
    CONF_PRICE_TOU_BANDS,
)


# --- Stored-value parsing ------------------------------------------------------

def parse_hhmm(value: Any) -> int | None:
    """Parse an "HH:MM" local time-of-day string to minutes-from-midnight.

    Args:
        value: A string like "06:30"; anything else returns None.

    Returns:
        Minutes from local midnight (0–1439), or None when unparseable / out of range.
    """
    if not isinstance(value, str) or ":" not in value:
        return None
    hh, _, mm = value.strip().partition(":")
    if not (hh.isdigit() and mm.isdigit()) or int(mm) > 59:
        return None
    minute = int(hh) * 60 + int(mm)
    return minute if minute <= 1439 else None


def parse_mmdd(value: Any) -> tuple[int, int] | None:
    """Parse an "MM-DD" calendar day (a season start) to ``(month, day)``.

    Args:
        value: A string like "04-01"; anything else returns None.

    Returns:
        ``(month, day)``, or None when unparseable or not a real day (02-29 is
        accepted — it exists in leap years).
    """
    if not isinstance(value, str) or "-" not in value:
        return None
    mm, _, dd = value.strip().partition("-")
    if not (mm.isdigit() and dd.isdigit()):
        return None
    try:
        date(2024, int(mm), int(dd))
    except ValueError:
        return None
    return int(mm), int(dd)


def _float(value: Any, default: float = 0.0) -> float:
    """Return value as a float, or default when it is missing or not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def tariff_count(raw: Mapping[str, Any]) -> int:
    """Return a stored formula's grid-fee tariff count (1 when missing or invalid)."""
    try:
        count = int(raw.get(FORMULA_TARIFFS, 1))
    except (TypeError, ValueError):
        return 1
    return count if count in TARIFF_COUNTS else 1


def schedule_keys(raw: Mapping[str, Any]) -> list[str]:
    """Return the schedules a stored formula needs, in form order.

    Args:
        raw: A stored formula dict.

    Returns:
        ``"<season>_<day type>"`` keys: none with a single tariff; otherwise
        workday plus the enabled weekend / holiday day types, once per season
        (summer and winter when seasons are on, else ``all``).
    """
    if tariff_count(raw) == 1:
        return []
    seasons = (SEASON_SUMMER, SEASON_WINTER) if raw.get(FORMULA_SEASONS) else (SEASON_ALL,)
    days = [DAY_WORKDAY]
    if raw.get(FORMULA_WEEKENDS):
        days.append(DAY_WEEKEND)
    if raw.get(FORMULA_HOLIDAYS):
        days.append(DAY_HOLIDAY)
    return [f"{season}_{day}" for season in seasons for day in days]


def switches_from_config(rows: Any, count: int) -> tuple[TariffSwitch, ...]:
    """Parse stored switch rows, dropping malformed ones and unknown tariffs.

    Args:
        rows: Stored ``[{"start": "HH:MM", "tariff": n}, …]`` list, or None.
        count: The formula's tariff count; a row naming a higher tariff is dropped.

    Returns:
        TariffSwitch tuple sorted by start minute (empty when none are valid).
    """
    switches: dict[int, TariffSwitch] = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping):
            continue
        start = parse_hhmm(row.get("start"))
        try:
            number = int(row.get("tariff", 0))
        except (TypeError, ValueError):
            continue
        if start is not None and 1 <= number <= count:
            switches[start] = TariffSwitch(start_minute=start, tariff=number)
    return tuple(switches[start] for start in sorted(switches))


def default_formula_config() -> dict[str, Any]:
    """Return the stored-dict shape of a formula nobody has configured yet."""
    return {
        FORMULA_ENERGY: ENERGY_DYNAMIC,
        FORMULA_FIXED_PRICE: 0.0,
        FORMULA_MARKUP: 0.0,
        FORMULA_VAT: 0.0,
        FORMULA_TARIFFS: 1,
        FORMULA_SEASONS: False,
        FORMULA_WEEKENDS: False,
        FORMULA_HOLIDAYS: False,
        FORMULA_SUMMER_START: DEFAULT_SUMMER_START,
        FORMULA_WINTER_START: DEFAULT_WINTER_START,
        FORMULA_GRID_FEES: [0.0],
        FORMULA_SCHEDULES: {},
    }


def formula_from_config(raw: Mapping[str, Any] | None) -> PriceFormula:
    """Build a PriceFormula from its stored dict.

    Args:
        raw: Stored formula dict (see :func:`default_formula_config`), or None.

    Returns:
        The parsed formula. Missing grid fees read 0; schedules are kept only
        for the day types the formula's switches enable.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    count = tariff_count(raw)
    stored_fees = raw.get(FORMULA_GRID_FEES)
    fees = [_float(fee) for fee in stored_fees] if isinstance(stored_fees, list) else []
    fees = (fees + [0.0] * count)[:count]
    stored_schedules = raw.get(FORMULA_SCHEDULES)
    stored_schedules = stored_schedules if isinstance(stored_schedules, Mapping) else {}
    schedules = []
    for key in schedule_keys(raw):
        season, _, day_type = key.partition("_")
        switches = switches_from_config(stored_schedules.get(key), count)
        if switches:
            schedules.append(DaySchedule(season=season, day_type=day_type, switches=switches))
    return PriceFormula(
        energy_mode=ENERGY_FIXED if raw.get(FORMULA_ENERGY) == ENERGY_FIXED else ENERGY_DYNAMIC,
        fixed_price=_float(raw.get(FORMULA_FIXED_PRICE)),
        markup=_float(raw.get(FORMULA_MARKUP)),
        vat=_float(raw.get(FORMULA_VAT)) / 100.0,
        grid_fees=tuple(fees),
        seasons=bool(raw.get(FORMULA_SEASONS)),
        weekends=bool(raw.get(FORMULA_WEEKENDS)),
        holidays=bool(raw.get(FORMULA_HOLIDAYS)),
        summer_start=parse_mmdd(raw.get(FORMULA_SUMMER_START)) or _DEFAULT_FORMULA.summer_start,
        winter_start=parse_mmdd(raw.get(FORMULA_WINTER_START)) or _DEFAULT_FORMULA.winter_start,
        schedules=tuple(schedules),
    )


def price_config(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the stored buy and sell formula dicts of a config entry.

    Args:
        data: Merged config-entry data + options (or a setup flow's accumulator).

    Returns:
        ``(buy, sell)`` formula dicts — the stored ones, or for an entry that
        predates them, the conversion of its legacy tariff keys.
    """
    buy, sell = data.get(CONF_PRICE_BUY), data.get(CONF_PRICE_SELL)
    if isinstance(buy, Mapping) and isinstance(sell, Mapping):
        return dict(buy), dict(sell)
    legacy_buy, legacy_sell = legacy_price_config(data)
    return (
        dict(buy) if isinstance(buy, Mapping) else legacy_buy,
        dict(sell) if isinstance(sell, Mapping) else legacy_sell,
    )


def tariff_from_config(data: Mapping[str, Any]) -> TariffConfig:
    """Build the TariffConfig of a config entry (legacy entries converted)."""
    buy, sell = price_config(data)
    return TariffConfig(buy=formula_from_config(buy), sell=formula_from_config(sell))


def needs_market_price(config: TariffConfig) -> bool:
    """Return True when either price follows the market, so a price feed is read."""
    return ENERGY_DYNAMIC in (config.buy.energy_mode, config.sell.energy_mode)


# --- Legacy conversion ---------------------------------------------------------

def _legacy_rows(raw_rows: Any, value: Callable[[Mapping[str, Any]], float]) -> list[tuple[str, float]]:
    """Return legacy band rows as sorted ``("HH:MM", fee)`` pairs.

    Args:
        raw_rows: A stored legacy band list, or None.
        value: Extracts the row's fee from a band dict.

    Returns:
        Valid rows sorted by start, with zero-padded start times.
    """
    rows: dict[int, float] = {}
    for row in raw_rows if isinstance(raw_rows, list) else ():
        if isinstance(row, Mapping) and (start := parse_hhmm(row.get("start"))) is not None:
            rows[start] = value(row)
    return [(f"{start // 60:02d}:{start % 60:02d}", rows[start]) for start in sorted(rows)]


def _fee_schedule(workday: list[tuple[str, float]], weekend: list[tuple[str, float]]) -> dict[str, Any]:
    """Express two legacy day schedules of absolute fees as tariffs plus switch points.

    Each distinct fee becomes a tariff (first seen, first numbered). More than
    four distinct fees cannot be kept; the extra ones map to the nearest kept
    fee and a warning is logged.

    Args:
        workday: Mon–Fri ``("HH:MM", fee)`` rows (at least one).
        weekend: Sat/Sun rows (at least one).

    Returns:
        The tariff, grid-fee, weekend and schedule fields of a formula dict.
    """
    fees: list[float] = []
    for _, fee in [*workday, *weekend]:
        if fee not in fees:
            fees.append(fee)
    kept = fees[: TARIFF_COUNTS[-1]]
    if len(kept) < len(fees):
        _LOGGER.warning("Legacy tariff has %d distinct fees; keeping %s, the rest map to the nearest", len(fees), kept)
    count = next(c for c in TARIFF_COUNTS if c >= len(kept))
    fields: dict[str, Any] = {
        FORMULA_TARIFFS: count,
        FORMULA_GRID_FEES: kept + [kept[-1]] * (count - len(kept)),
        FORMULA_WEEKENDS: False,
        FORMULA_SCHEDULES: {},
    }
    if count == 1:
        return fields

    def rows(day: list[tuple[str, float]]) -> list[dict[str, Any]]:
        """Return a day's rows as switch dicts pointing at the nearest kept fee."""
        return [
            {"start": start, "tariff": min(range(len(kept)), key=lambda i: abs(kept[i] - fee)) + 1}
            for start, fee in day
        ]

    fields[FORMULA_SCHEDULES] = {f"{SEASON_ALL}_{DAY_WORKDAY}": rows(workday)}
    if rows(weekend) != rows(workday):
        fields[FORMULA_WEEKENDS] = True
        fields[FORMULA_SCHEDULES][f"{SEASON_ALL}_{DAY_WEEKEND}"] = rows(weekend)
    return fields


def _legacy_side(
    data: Mapping[str, Any], *, flat_fee: float, markup: float, vat: float, fee_of: Callable[[Mapping], float],
) -> dict[str, Any]:
    """Convert one direction of a legacy spot tariff (flat fee + weekday/weekend bands).

    Args:
        data: The legacy config entry.
        flat_fee: The fee on days whose band list is empty.
        markup: Per-kWh markup.
        vat: Tax rate in percent.
        fee_of: Extracts this direction's fee from a band dict.

    Returns:
        The equivalent formula dict (dynamic energy).
    """
    workday = _legacy_rows(data.get(CONF_TARIFF_WEEKDAY_BANDS), fee_of)
    weekend = _legacy_rows(data.get(CONF_TARIFF_WEEKEND_BANDS), fee_of)
    if not workday and not weekend:
        schedule = _fee_schedule([("00:00", flat_fee)], [("00:00", flat_fee)])
    else:
        schedule = _fee_schedule(workday or [("00:00", flat_fee)], weekend or [("00:00", flat_fee)])
    return {**default_formula_config(), FORMULA_MARKUP: markup, FORMULA_VAT: vat, **schedule}


def legacy_price_config(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a legacy tariff to buy and sell formula dicts, preserving its prices.

    Legacy shape: a flat distribution fee per direction, optional weekday /
    weekend fee bands, global markups and tax rates, a sell mode (``spot``,
    ``fixed``, ``schedule`` — the band's sell fee is the absolute price — or
    ``feed``, the raw export price) and the synthetic ``tou`` import-price
    source. Distinct band fees become grid-fee tariffs. Absolute prices
    (``schedule``, ``tou``) become a fixed zero energy price with the price
    carried by the grid fee — negated on the sell side, where fees are
    deducted. A ``tou`` source ignores the weekday / weekend fee bands.

    Args:
        data: The legacy config entry (an empty dict gives the defaults).

    Returns:
        ``(buy, sell)`` formula dicts.
    """
    buy_fee = _float(data.get(CONF_TARIFF_DISTRIBUTION_FEE))
    sell_fee = _float(data.get(CONF_TARIFF_SELL_DISTRIBUTION_FEE))
    buy_markup = _float(data.get(CONF_TARIFF_MARKUP))
    sell_markup = _float(data.get(CONF_TARIFF_SELL_MARKUP))
    buy_vat = _float(data.get(CONF_TARIFF_TAX_RATE))
    sell_vat = _float(data.get(CONF_TARIFF_SELL_TAX_RATE))
    fixed_sell = _float(data.get(CONF_TARIFF_FIXED_SELL_PRICE))
    mode = data.get(CONF_TARIFF_SELL_MODE) or "spot"

    tou = _legacy_rows(data.get(CONF_PRICE_TOU_BANDS), lambda row: _float(row.get("price")))
    if data.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_TOU and tou:
        buy_rows = [(start, price + buy_fee) for start, price in tou]
        buy = {
            **default_formula_config(), FORMULA_ENERGY: ENERGY_FIXED,
            FORMULA_MARKUP: buy_markup, FORMULA_VAT: buy_vat, **_fee_schedule(buy_rows, buy_rows),
        }
    else:
        buy = _legacy_side(
            data, flat_fee=buy_fee, markup=buy_markup, vat=buy_vat,
            fee_of=lambda row: _float(row.get("buy_fee")),
        )

    zero_sell = {**default_formula_config(), FORMULA_ENERGY: ENERGY_FIXED}
    if mode == "fixed":
        sell = {**zero_sell, FORMULA_FIXED_PRICE: fixed_sell}
    elif mode == "feed":
        sell = default_formula_config()
    elif mode == "schedule":
        sell = {
            **_legacy_side(data, flat_fee=-fixed_sell, markup=0.0, vat=0.0,
                           fee_of=lambda row: -_float(row.get("sell_fee"))),
            FORMULA_ENERGY: ENERGY_FIXED,
        }
    elif data.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_TOU and tou:
        sell_rows = [(start, sell_fee - price) for start, price in tou]
        sell = {
            **zero_sell, FORMULA_MARKUP: sell_markup, FORMULA_VAT: sell_vat,
            **_fee_schedule(sell_rows, sell_rows),
        }
    else:
        sell = _legacy_side(
            data, flat_fee=sell_fee, markup=sell_markup, vat=sell_vat,
            fee_of=lambda row: _float(row.get("sell_fee")),
        )
    return buy, sell


# --- Per-slot evaluation -------------------------------------------------------

def season_of(formula: PriceFormula, day: date) -> str:
    """Return the season whose schedule applies on a local date.

    Args:
        formula: The price formula.
        day: The slot's local date.

    Returns:
        ``all`` without seasons; otherwise ``summer`` from the summer start up
        to (not including) the winter start, ``winter`` for the rest of the year.
    """
    if not formula.seasons:
        return SEASON_ALL
    today = (day.month, day.day)
    summer, winter = formula.summer_start, formula.winter_start
    if summer <= winter:
        return SEASON_SUMMER if summer <= today < winter else SEASON_WINTER
    return SEASON_WINTER if winter <= today < summer else SEASON_SUMMER


def day_type_of(formula: PriceFormula, local_dt: datetime, is_holiday: HolidayPredicate | None) -> str:
    """Return the day type whose schedule applies at a local time.

    A public holiday wins over a weekend when both have their own schedule;
    a day type the formula does not separate reads as a workday.

    Args:
        formula: The price formula.
        local_dt: Slot start in local time.
        is_holiday: Public-holiday predicate, or None when no calendar is known.

    Returns:
        ``holiday``, ``weekend`` or ``workday``.
    """
    if formula.holidays and is_holiday is not None and is_holiday(local_dt.date()):
        return DAY_HOLIDAY
    if formula.weekends and local_dt.weekday() >= 5:
        return DAY_WEEKEND
    return DAY_WORKDAY


def active_tariff(
    formula: PriceFormula, local_dt: datetime | None, is_holiday: HolidayPredicate | None = None,
) -> int:
    """Return the grid-fee tariff number (1-based) active at a local time.

    The switch whose start is the latest at or before the slot's minute-of-day
    wins; before the day's first switch the last one carries over, so a
    schedule covers 24 h. T1 applies with a single tariff, without a local
    time, or when the season / day type has no schedule.

    Args:
        formula: The price formula.
        local_dt: Slot start in local time, or None.
        is_holiday: Public-holiday predicate, or None.

    Returns:
        The active tariff number.
    """
    if len(formula.grid_fees) == 1 or local_dt is None:
        return 1
    season = season_of(formula, local_dt.date())
    day_type = day_type_of(formula, local_dt, is_holiday)
    schedule = next(
        (s for s in formula.schedules if s.season == season and s.day_type == day_type), None,
    )
    if schedule is None or not schedule.switches:
        return 1
    minute = local_dt.hour * 60 + local_dt.minute
    chosen = schedule.switches[-1]
    for switch in schedule.switches:
        if switch.start_minute > minute:
            break
        chosen = switch
    return chosen.tariff


def grid_fee(formula: PriceFormula, local_dt: datetime | None, is_holiday: HolidayPredicate | None = None) -> float:
    """Return the grid fee (per kWh) active at a local time."""
    return formula.grid_fees[active_tariff(formula, local_dt, is_holiday) - 1]


def buy_price(
    spot: float,
    config: TariffConfig,
    local_dt: datetime | None = None,
    is_holiday: HolidayPredicate | None = None,
) -> float:
    """Compute the total cost of buying 1 kWh from the grid.

    ``(energy + markup + grid_fee) × (1 + vat)`` with the buy formula; energy
    is ``spot`` when dynamic, else the fixed price.

    Args:
        spot: The slot's market (import) price per kWh.
        config: User-configured tariff.
        local_dt: Slot start in local time for tariff selection; None → T1.
        is_holiday: Public-holiday predicate for the holiday schedule.

    Returns:
        Effective buy price per kWh.
    """
    formula = config.buy
    energy = spot if formula.energy_mode == ENERGY_DYNAMIC else formula.fixed_price
    return (energy + formula.markup + grid_fee(formula, local_dt, is_holiday)) * (1.0 + formula.vat)


def sell_price(
    spot: float,
    config: TariffConfig,
    local_dt: datetime | None = None,
    export: float | None = None,
    is_holiday: HolidayPredicate | None = None,
) -> float:
    """Compute the net revenue from selling 1 kWh to the grid.

    ``(energy − markup − grid_fee) × (1 − vat)`` with the sell formula; energy
    is the slot's separate live export price when the feed has one, else
    ``spot``, when dynamic — or the fixed (feed-in) price.

    Args:
        spot: The slot's market (import) price per kWh.
        config: User-configured tariff.
        local_dt: Slot start in local time for tariff selection; None → T1.
        export: The slot's separate live export price, or None.
        is_holiday: Public-holiday predicate for the holiday schedule.

    Returns:
        Effective sell price per kWh; can be negative.
    """
    formula = config.sell
    if formula.energy_mode == ENERGY_DYNAMIC:
        energy = export if export is not None else spot
    else:
        energy = formula.fixed_price
    return (energy - formula.markup - grid_fee(formula, local_dt, is_holiday)) * (1.0 - formula.vat)


def format_switches(switches: Sequence[TariffSwitch]) -> str:
    """Return switch points as a readable line, e.g. "07:00 T1, 23:00 T2"."""
    return ", ".join(f"{s.start_minute // 60:02d}:{s.start_minute % 60:02d} T{s.tariff}" for s in switches)
