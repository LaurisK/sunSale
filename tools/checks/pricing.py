"""Pricing deep check and its TUI widgets."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


def _resolve_tz(name: str) -> tzinfo:
    """Return the tzinfo for an IANA name, falling back to UTC.

    Args:
        name: IANA timezone name from the debug payload (e.g. "Europe/Riga").

    Returns:
        The resolved tzinfo, or UTC when the name is empty/unknown.
    """
    try:
        return ZoneInfo(name) if name else UTC
    except Exception:  # noqa: BLE001 - unknown tz name → safe UTC fallback
        return UTC


def _holiday_calendar(country: str | None) -> Callable[[date], bool] | None:
    """Return the public-holiday predicate for a country, or None without a calendar.

    Args:
        country: The payload's ``holiday_country`` (None → no holidays).

    Returns:
        ``date -> bool``, or None when no country is set, the ``holidays``
        package is missing, or it has no calendar for the country.
    """
    if not country:
        return None
    try:
        import holidays  # noqa: PLC0415 — optional; the check degrades without it

        return holidays.country_holidays(country.upper()).__contains__
    except (ImportError, NotImplementedError):
        return None


def _grid_fee(formula: dict, local_dt: datetime | None, is_holiday: Callable[[date], bool] | None) -> float:
    """Mirror pipeline/tariff.py:grid_fee for one serialised formula and local slot start.

    Picks the season (summer from ``summer_start`` up to ``winter_start``,
    wrapping the new year), the day type (holiday before weekend, each only
    when the formula separates it), then the switch whose start is the latest
    at or before the slot's minute-of-day (the day's last switch before the
    first). T1 applies with one tariff, no local time or no matching schedule.

    Args:
        formula: Serialised ``PriceFormula`` dict (buy or sell).
        local_dt: Slot start in the installation's local timezone, or None.
        is_holiday: Public-holiday predicate, or None.

    Returns:
        The grid fee per kWh.
    """
    fees = formula.get("grid_fees") or [0.0]
    if len(fees) == 1 or local_dt is None:
        return fees[0]
    season = "all"
    if formula.get("seasons"):
        today = (local_dt.month, local_dt.day)
        summer = tuple(formula.get("summer_start") or (4, 1))
        winter = tuple(formula.get("winter_start") or (11, 1))
        in_summer = summer <= today < winter if summer <= winter else not winter <= today < summer
        season = "summer" if in_summer else "winter"
    if formula.get("holidays") and is_holiday is not None and is_holiday(local_dt.date()):
        day_type = "holiday"
    elif formula.get("weekends") and local_dt.weekday() >= 5:
        day_type = "weekend"
    else:
        day_type = "workday"
    switches = next(
        (s.get("switches") or [] for s in formula.get("schedules") or []
         if s.get("season") == season and s.get("day_type") == day_type),
        [],
    )
    if not switches:
        return fees[0]
    minute = local_dt.hour * 60 + local_dt.minute
    ordered = sorted(switches, key=lambda s: s.get("start_minute", 0))
    tariff = ordered[-1].get("tariff", 1)
    for switch in ordered:
        if switch.get("start_minute", 0) > minute:
            break
        tariff = switch.get("tariff", 1)
    return fees[tariff - 1]


def _energy(formula: dict, market: float) -> float:
    """Return a formula's energy price: the market price when dynamic, else its fixed price."""
    return market if formula.get("energy_mode", "dynamic") == "dynamic" else formula.get("fixed_price", 0.0)


@dataclass
class PricingCheckResult:
    """Result of the pricing deep-check: tariff formula vs module output."""

    skipped: bool = False
    skip_reason: str = ""
    module_slot_count: int = 0
    resolution_s: int = 0
    computed_at: str = ""
    negative_sell_count: int = 0
    tariff_config: dict = field(default_factory=dict)
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_pricing(snap: Snapshot) -> PricingCheckResult:
    """Verify every pricing slot against the tariff formula.

    Args:
        snap: Coordinator snapshot containing pipeline.pricing and inputs.tariff_config.

    Returns:
        PricingCheckResult with per-slot formula comparisons and overall pass/fail.
    """
    result = PricingCheckResult()

    pricing = snap.pipeline.get("pricing")
    if not pricing:
        result.skipped = True
        result.skip_reason = "pipeline.pricing is null"
        return result

    tariff = snap.inputs.get("tariff_config")
    if not tariff:
        result.skipped = True
        result.skip_reason = "inputs.tariff_config is null"
        return result

    result.module_slot_count = pricing.get("slot_count", 0)
    result.resolution_s = pricing.get("resolution_s", 0)
    result.computed_at = pricing.get("computed_at", "")
    result.negative_sell_count = pricing.get("negative_sell_count", 0)
    result.tariff_config = dict(tariff)

    buy = tariff.get("buy") or {}
    sell = tariff.get("sell") or {}
    local_tz = _resolve_tz(snap.config.get("time_zone", ""))
    country = snap.config.get("holiday_country")
    is_holiday = _holiday_calendar(country)
    if country and is_holiday is None and (buy.get("holidays") or sell.get("holidays")):
        result.skipped = True
        result.skip_reason = f"a holiday schedule is in use but this machine has no holiday calendar for {country}"
        return result
    TOL = 1e-3

    for s in pricing.get("slots") or []:
        spot = s.get("spot", 0.0)
        act_buy = s.get("buy", 0.0)
        act_sell = s.get("sell", 0.0)
        export = s.get("export")
        # Resolve the per-slot grid-fee tariff the same way PricingNode does,
        # projecting the UTC slot start into local time first.
        try:
            local_start = datetime.fromisoformat(s.get("start", "")).astimezone(local_tz)
        except (ValueError, AttributeError):
            local_start = None
        exp_buy = (
            (_energy(buy, spot) + buy.get("markup", 0.0) + _grid_fee(buy, local_start, is_holiday))
            * (1 + buy.get("vat", 0.0))
        )
        # A dynamic sell price follows the separate export feed where the slot has one.
        sell_market = export if export is not None else spot
        exp_sell = (
            (_energy(sell, sell_market) - sell.get("markup", 0.0) - _grid_fee(sell, local_start, is_holiday))
            * (1 - sell.get("vat", 0.0))
        )
        ok = abs(act_buy - exp_buy) <= TOL and abs(act_sell - exp_sell) <= TOL
        if not ok:
            result.mismatches.append(s.get("start", ""))
            result.overall_ok = False
        result.slot_rows.append({
            "start": s.get("start", ""),
            "spot": spot,
            "exp_buy": exp_buy,
            "act_buy": act_buy,
            "exp_sell": exp_sell,
            "act_sell": act_sell,
            "ok": ok,
        })

    return result


class PricingSlotsTable(Static):
    """DataTable: Time | Spot | Exp Buy | Act Buy | Exp Sell | Act Sell | ✓/✗."""

    DEFAULT_CSS = """
    PricingSlotsTable { height: auto; }
    PricingSlotsTable DataTable { height: 22; }
    """

    def __init__(self, pc: PricingCheckResult) -> None:
        """Initialise with the pricing check result.

        Args:
            pc: Result of check_pricing() containing per-slot formula comparisons.
        """
        super().__init__()
        self._pc = pc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per pricing slot."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Spot", "Exp Buy", "Act Buy", "Exp Sell", "Act Sell", "")
        dim = "dim"

        for row in self._pc.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(UTC)
                time_str = dt.strftime("%m-%d %H:%M")
            except (ValueError, AttributeError):
                time_str = row["start"]

            ok = row["ok"]
            bad_style = "red" if not ok else ""
            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{row['spot']:.4f}"),
                Text(f"{row['exp_buy']:.4f}", style=dim),
                Text(f"{row['act_buy']:.4f}", style=bad_style),
                Text(f"{row['exp_sell']:.4f}", style=dim),
                Text(f"{row['act_sell']:.4f}", style=bad_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class PricingCheckWidget(Static):
    """Collapsible pricing deep-check: formula verification per slot + tariff config."""

    DEFAULT_CSS = "PricingCheckWidget { height: auto; }"

    def __init__(self, pc: PricingCheckResult) -> None:
        """Initialise with the pre-computed pricing check result.

        Args:
            pc: Result of check_pricing() for one coordinator.
        """
        super().__init__()
        self._pc = pc

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and optional Tariff Config sub-Collapsible."""
        pc = self._pc

        if pc.skipped:
            yield Static(f"  ⚠  pricing_check   SKIP   {pc.skip_reason}")
            return

        color = "green" if pc.overall_ok else "red"
        mark = "✓" if pc.overall_ok else "✗"
        status = "PASS" if pc.overall_ok else "FAIL"
        neg = f"  {pc.negative_sell_count} negative-sell" if pc.negative_sell_count else ""
        res_min = pc.resolution_s // 60
        title = (
            f"[{color}]{mark}[/{color}]  pricing_check   [{color}]{status}[/{color}]"
            f"   {pc.module_slot_count} slots  res={res_min}min{neg}"
        )

        with Collapsible(title=title, collapsed=True):
            with Collapsible(title="Slots", collapsed=False):
                yield PricingSlotsTable(pc)
            with Collapsible(title="Tariff Config", collapsed=True):
                lines = "\n".join(f"  {k}: {v}" for k, v in pc.tariff_config.items())
                yield Static(lines)
