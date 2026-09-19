"""Buy / sell price forms: the formula, its grid fees and the tariff schedules.

Both directions use the same three kinds of page, stored in one formula dict
per direction (``CONF_PRICE_BUY`` / ``CONF_PRICE_SELL``, see
``pipeline/tariff.py``):

- **formula** — energy (market or fixed price), markup, VAT, the number of
  grid-fee tariffs (1, 2 or 4) and whether seasons, weekends and holidays get
  their own schedule;
- **grid fees** — one fee per tariff;
- **schedule** — per season / day type, up to ``MAX_TARIFF_SWITCHES``
  "from HH:MM → tariff" rows; each tariff runs until the next row's start,
  wrapping past midnight. Only offered with more than one tariff.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from ..contract.const import (
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
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
    MAX_TARIFF_SWITCHES,
    SEASON_ALL,
    SEASON_SUMMER,
    SEASON_WINTER,
    TARIFF_COUNTS,
)
from ..pipeline.tariff import (
    ALL_SCHEDULE_KEYS,
    format_switches,
    parse_hhmm,
    parse_mmdd,
    schedule_keys,
    switches_from_config,
    tariff_count,
)
from .fields import list_selector, suggest

SIDE_BUY = "buy"
SIDE_SELL = "sell"
SIDES = (SIDE_BUY, SIDE_SELL)
SIDE_CONF = {SIDE_BUY: CONF_PRICE_BUY, SIDE_SELL: CONF_PRICE_SELL}
SIDE_LABELS = {SIDE_BUY: "Buy price", SIDE_SELL: "Sell price"}

_SEASON_NAMES = {SEASON_ALL: "", SEASON_SUMMER: "summer ", SEASON_WINTER: "winter "}
_DAY_NAMES = {DAY_WORKDAY: "workday", DAY_WEEKEND: "weekend", DAY_HOLIDAY: "holiday"}

_ENERGY_SELECTOR = list_selector([
    {"value": ENERGY_DYNAMIC, "label": "Market price from the price source"},
    {"value": ENERGY_FIXED, "label": "Fixed price"},
])
_TARIFFS_SELECTOR = list_selector([
    {"value": "1", "label": "1 — one grid fee at all times"},
    {"value": "2", "label": "2 — e.g. day / night"},
    {"value": "4", "label": "4 — e.g. peak / day / evening / night"},
])


# --- Step ids ------------------------------------------------------------------

def side_menu(side: str) -> str:
    """Return the step id of a direction's menu (``price_buy`` / ``price_sell``)."""
    return f"price_{side}"


def formula_page(side: str) -> str:
    """Return the step id of a direction's formula form."""
    return f"price_{side}_formula"


def fees_page(side: str) -> str:
    """Return the step id of a direction's grid-fee form."""
    return f"price_{side}_fees"


def schedule_page(side: str, key: str) -> str:
    """Return the menu-row step id that opens one schedule, e.g. ``price_buy_all_workday``."""
    return f"price_{side}_{key}"


def schedule_form(side: str) -> str:
    """Return the step id every schedule form of a direction is shown (and submitted) under."""
    return f"price_{side}_schedule"


def schedule_label(key: str) -> str:
    """Return a schedule's display name, e.g. "Workday schedule" or "Summer weekend schedule"."""
    season, _, day = key.partition("_")
    return f"{_SEASON_NAMES[season]}{_DAY_NAMES[day]} schedule".capitalize()


# Every page of both directions → (direction, short label), menu order.
SIDE_PAGES: dict[str, tuple[str, str]] = {}
# Schedule page → (direction, schedule key).
SCHEDULE_PAGES: dict[str, tuple[str, str]] = {}
for _side in SIDES:
    SIDE_PAGES[formula_page(_side)] = (_side, "Price formula")
    SIDE_PAGES[fees_page(_side)] = (_side, "Grid fees")
    for _key in ALL_SCHEDULE_KEYS:
        SIDE_PAGES[schedule_page(_side, _key)] = (_side, schedule_label(_key))
        SCHEDULE_PAGES[schedule_page(_side, _key)] = (_side, _key)


# --- Formula form --------------------------------------------------------------

def formula_schema(d: dict[str, Any]) -> vol.Schema:
    """Build the formula form, pre-filled from a formula dict (or a rejected submit).

    Args:
        d: The direction's formula dict, defaults filled in.

    Returns:
        Schema with energy source, fixed price, markup, VAT, tariff count,
        the weekend / holiday / season toggles and the season start dates.
    """
    return vol.Schema({
        vol.Required(FORMULA_ENERGY, default=d[FORMULA_ENERGY]): _ENERGY_SELECTOR,
        vol.Required(FORMULA_FIXED_PRICE, default=d[FORMULA_FIXED_PRICE]): vol.Coerce(float),
        vol.Required(FORMULA_MARKUP, default=d[FORMULA_MARKUP]): vol.Coerce(float),
        vol.Required(FORMULA_VAT, default=d[FORMULA_VAT]): vol.Coerce(float),
        vol.Required(FORMULA_TARIFFS, default=str(d[FORMULA_TARIFFS])): _TARIFFS_SELECTOR,
        vol.Required(FORMULA_WEEKENDS, default=bool(d[FORMULA_WEEKENDS])): bool,
        vol.Required(FORMULA_HOLIDAYS, default=bool(d[FORMULA_HOLIDAYS])): bool,
        vol.Required(FORMULA_SEASONS, default=bool(d[FORMULA_SEASONS])): bool,
        vol.Required(FORMULA_SUMMER_START, default=d[FORMULA_SUMMER_START]): str,
        vol.Required(FORMULA_WINTER_START, default=d[FORMULA_WINTER_START]): str,
    })


def validate_formula(user_input: dict[str, Any]) -> dict[str, str]:
    """Return field-keyed errors for the formula form.

    Args:
        user_input: Submitted formula values.

    Returns:
        Mapping of field key to error code; empty when the formula is valid.
        Season dates are only checked while seasons are on.
    """
    errors: dict[str, str] = {}
    if not 0.0 <= user_input.get(FORMULA_VAT, 0.0) <= 100.0:
        errors[FORMULA_VAT] = "invalid_vat"
    if user_input.get(FORMULA_SEASONS):
        summer = parse_mmdd(user_input.get(FORMULA_SUMMER_START))
        winter = parse_mmdd(user_input.get(FORMULA_WINTER_START))
        if summer is None:
            errors[FORMULA_SUMMER_START] = "invalid_season_date"
        if winter is None:
            errors[FORMULA_WINTER_START] = "invalid_season_date"
        elif summer == winter:
            errors[FORMULA_WINTER_START] = "seasons_same_start"
    return errors


def _mmdd(value: Any, fallback: str) -> str:
    """Return value as a zero-padded "MM-DD", or fallback when it is not a real day."""
    parsed = parse_mmdd(value)
    return f"{parsed[0]:02d}-{parsed[1]:02d}" if parsed else fallback


def apply_formula(stored: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any]:
    """Return the formula dict with a submitted formula form applied.

    Grid fees are resized to the new tariff count (new tariffs start at the
    last fee). Schedules the new structure no longer offers are dropped, as
    are switch rows naming a tariff that no longer exists — a schedule left
    empty must be filled in again.

    Args:
        stored: The direction's current formula dict, defaults filled in.
        user_input: The validated formula form values.

    Returns:
        A new formula dict; ``stored`` is not modified.
    """
    count = int(user_input.get(FORMULA_TARIFFS, stored[FORMULA_TARIFFS]))
    count = count if count in TARIFF_COUNTS else 1
    fees = [float(fee) for fee in stored.get(FORMULA_GRID_FEES) or []] or [0.0]
    fees = (fees + [fees[-1]] * count)[:count]
    formula = {
        **stored,
        FORMULA_ENERGY: ENERGY_FIXED if user_input.get(FORMULA_ENERGY) == ENERGY_FIXED else ENERGY_DYNAMIC,
        FORMULA_FIXED_PRICE: float(user_input.get(FORMULA_FIXED_PRICE, 0.0)),
        FORMULA_MARKUP: float(user_input.get(FORMULA_MARKUP, 0.0)),
        FORMULA_VAT: float(user_input.get(FORMULA_VAT, 0.0)),
        FORMULA_TARIFFS: count,
        FORMULA_WEEKENDS: bool(user_input.get(FORMULA_WEEKENDS)),
        FORMULA_HOLIDAYS: bool(user_input.get(FORMULA_HOLIDAYS)),
        FORMULA_SEASONS: bool(user_input.get(FORMULA_SEASONS)),
        FORMULA_SUMMER_START: _mmdd(
            user_input.get(FORMULA_SUMMER_START), stored.get(FORMULA_SUMMER_START) or DEFAULT_SUMMER_START,
        ),
        FORMULA_WINTER_START: _mmdd(
            user_input.get(FORMULA_WINTER_START), stored.get(FORMULA_WINTER_START) or DEFAULT_WINTER_START,
        ),
        FORMULA_GRID_FEES: fees,
    }
    schedules = {}
    for key in schedule_keys(formula):
        rows = [row for row in (stored.get(FORMULA_SCHEDULES) or {}).get(key) or [] if row.get("tariff", 0) <= count]
        if rows:
            schedules[key] = rows
    formula[FORMULA_SCHEDULES] = schedules
    return formula


# --- Grid-fee form -------------------------------------------------------------

def fee_key(number: int) -> str:
    """Return the grid-fee form field of tariff ``number`` (1-based)."""
    return f"fee_{number}"


def fees_schema(d: dict[str, Any]) -> vol.Schema:
    """Build the grid-fee form: one fee per tariff, pre-filled from the formula dict."""
    fees = list(d.get(FORMULA_GRID_FEES) or [])
    return vol.Schema({
        vol.Required(fee_key(n), default=fees[n - 1] if n <= len(fees) else 0.0): vol.Coerce(float)
        for n in range(1, tariff_count(d) + 1)
    })


def apply_fees(stored: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any]:
    """Return the formula dict with the submitted grid fees (a new dict)."""
    fees = [float(user_input.get(fee_key(n), 0.0)) for n in range(1, tariff_count(stored) + 1)]
    return {**stored, FORMULA_GRID_FEES: fees}


# --- Schedule form -------------------------------------------------------------

def start_key(row: int) -> str:
    """Return the "from" field of switch row ``row`` (1-based)."""
    return f"from_{row}"


def tariff_key(row: int) -> str:
    """Return the tariff field of switch row ``row`` (1-based)."""
    return f"tariff_{row}"


def schedule_form_values(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a stored schedule as schedule-form field values."""
    values: dict[str, Any] = {}
    for n, row in enumerate(rows[:MAX_TARIFF_SWITCHES], start=1):
        values[start_key(n)] = row.get("start")
        values[tariff_key(n)] = str(row.get("tariff"))
    return values


def schedule_schema(d: dict[str, Any], values: dict[str, Any]) -> vol.Schema:
    """Build the schedule form: ``MAX_TARIFF_SWITCHES`` "from → tariff" rows.

    Args:
        d: The direction's formula dict (for the tariff choices and their fees).
        values: Field values to pre-fill (a stored schedule or a rejected submit).

    Returns:
        Schema of optional row pairs. ``suggested_value`` pre-fills, so a
        cleared row really is dropped.
    """
    fees = list(d.get(FORMULA_GRID_FEES) or [])
    options = [
        {"value": str(n), "label": f"T{n} — grid fee {fees[n - 1]:g}" if n <= len(fees) else f"T{n}"}
        for n in range(1, tariff_count(d) + 1)
    ]
    tariff_selector = SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))
    fields: dict = {}
    for n in range(1, MAX_TARIFF_SWITCHES + 1):
        fields[vol.Optional(start_key(n), description=suggest(values.get(start_key(n))))] = str
        fields[vol.Optional(tariff_key(n), description=suggest(values.get(tariff_key(n))))] = tariff_selector
    return vol.Schema(fields)


def validate_schedule(user_input: dict[str, Any], count: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Parse the submitted switch rows.

    A row with neither field set is ignored; a half-filled row, a bad time or
    a repeated start is an error on that row's field.

    Args:
        user_input: Submitted schedule values.
        count: The formula's tariff count.

    Returns:
        ``(rows, errors)``: the stored switch rows sorted by start, and
        field-keyed error codes (``base`` when no row is filled in).
    """
    errors: dict[str, str] = {}
    switches: dict[int, int] = {}
    for n in range(1, MAX_TARIFF_SWITCHES + 1):
        text = str(user_input.get(start_key(n)) or "").strip()
        tariff = str(user_input.get(tariff_key(n)) or "").strip()
        if not text and not tariff:
            continue
        minute = parse_hhmm(text)
        if not text:
            errors[start_key(n)] = "required"
        elif minute is None:
            errors[start_key(n)] = "invalid_band_time"
        elif minute in switches:
            errors[start_key(n)] = "duplicate_band_start"
        if not tariff.isdigit() or not 1 <= int(tariff) <= count:
            errors[tariff_key(n)] = "required"
        if minute is not None and not errors.get(start_key(n)) and not errors.get(tariff_key(n)):
            switches[minute] = int(tariff)
    if not switches and not errors:
        errors["base"] = "schedule_empty"
    rows = [{"start": f"{m // 60:02d}:{m % 60:02d}", "tariff": switches[m]} for m in sorted(switches)]
    return rows, errors


# --- Status text ---------------------------------------------------------------

def formula_summary(side: str, d: dict[str, Any]) -> str:
    """Return a one-line description of a formula, e.g. "market price, markup 0.005, VAT 21 %, 2 tariffs"."""
    parts = ["market price" if d[FORMULA_ENERGY] == ENERGY_DYNAMIC else f"fixed {d[FORMULA_FIXED_PRICE]:g}"]
    if d[FORMULA_MARKUP]:
        parts.append(f"{'markup' if side == SIDE_BUY else 'deduction'} {d[FORMULA_MARKUP]:g}")
    parts.append(f"{'VAT' if side == SIDE_BUY else 'tax'} {d[FORMULA_VAT]:g} %")
    count = tariff_count(d)
    tariffs = "1 grid-fee tariff" if count == 1 else f"{count} grid-fee tariffs"
    split = [name for name, key in (("seasons", FORMULA_SEASONS), ("weekends", FORMULA_WEEKENDS),
                                    ("holidays", FORMULA_HOLIDAYS)) if d.get(key)]
    if count > 1 and split:
        tariffs += f" ({', '.join(split)})"
    parts.append(tariffs)
    return ", ".join(parts)


def fees_summary(d: dict[str, Any]) -> str:
    """Return the grid fees as one line, e.g. "T1 0.06, T2 0.035"."""
    fees = list(d.get(FORMULA_GRID_FEES) or [])[: tariff_count(d)]
    return ", ".join(f"T{n} {fee:g}" for n, fee in enumerate(fees, start=1))


def schedule_summary(d: dict[str, Any], key: str) -> str:
    """Return one stored schedule as a line, e.g. "07:00 T1, 23:00 T2"."""
    return format_switches(switches_from_config((d.get(FORMULA_SCHEDULES) or {}).get(key), tariff_count(d)))
