"""Schedule deep check, SoC sparkline, and schedule TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Sparkline, Static

from .snapshot import Snapshot


@dataclass
class ScheduleCheckResult:
    """Result of the schedule deep-check: slot ordering and profit consistency."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_expected_profit_eur: float = 0.0
    computed_profit_sum: float = 0.0
    export_limit_kw: float | None = None
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True
    terminal: TerminalCheck | None = None


# Tolerances for the terminal recompute. Prices reach the debug view rounded to
# 4 decimals, so a median can drift by ~1e-4; the reserve sums 72 hours of
# baseload rounded to 4 decimals.
_EUR_TOL = 1e-3
_KWH_TOL = 0.05

# Mirrors pipeline.schedule.LOAD_RESERVE_DAYS — used when the snapshot's
# policy block predates the load_reserve_days knob.
_LOAD_RESERVE_DAYS = 3


@dataclass
class TerminalCheck:
    """Declared vs recomputed end-of-horizon valuation (docs/load_reserve_plan.md)."""

    enabled: bool = False
    declared_flat_eur_kwh: float = 0.0
    computed_flat_eur_kwh: float | None = None
    declared_reserve_eur_kwh: float = 0.0
    computed_reserve_eur_kwh: float | None = None
    declared_reserve_kwh: float = 0.0
    computed_reserve_kwh: float | None = None
    skip_reason: str = ""
    ok: bool = True


def check_schedule(snap: Snapshot) -> ScheduleCheckResult:
    """Validate schedule slot ordering and summed profit matches the declared total.

    Args:
        snap: Coordinator snapshot containing outputs.schedule.

    Returns:
        ScheduleCheckResult with per-slot data and overall pass/fail.
    """
    result = ScheduleCheckResult()

    schedule = snap.outputs.get("schedule")
    if not schedule:
        result.skipped = True
        result.skip_reason = "outputs.schedule is null"
        return result

    slots = schedule.get("slots") or []
    result.slot_count = len(slots)
    result.total_expected_profit_eur = schedule.get("total_expected_profit_eur") or 0.0

    policy = snap.pipeline.get("schedule_policy") or {}
    result.export_limit_kw = policy.get("export_limit_kw")
    if result.export_limit_kw is not None and result.export_limit_kw < 0:
        result.mismatches.append("export_limit_kw")
        result.overall_ok = False

    now = datetime.now(UTC)
    last_dt: datetime | None = None
    computed_profit = 0.0

    for s in slots:
        start_str = s.get("start", "")
        end_str = s.get("end", start_str)

        try:
            start_dt: datetime | None = datetime.fromisoformat(start_str).astimezone(UTC)
        except (ValueError, AttributeError):
            start_dt = None

        try:
            end_dt: datetime | None = datetime.fromisoformat(end_str).astimezone(UTC)
        except (ValueError, AttributeError):
            end_dt = None

        is_current = (
            start_dt is not None and end_dt is not None and start_dt <= now < end_dt
        )
        ok = last_dt is None or start_dt is None or start_dt >= last_dt
        if not ok:
            result.mismatches.append(start_str)
            result.overall_ok = False

        profit = s.get("expected_profit_eur") or 0.0
        computed_profit += profit

        raw_soc = s.get("expected_soc_after")
        soc_pct = round(raw_soc * 100.0, 1) if raw_soc is not None else None
        result.slot_rows.append({
            "start": start_str,
            "end": end_str,
            "mode": s.get("mode", ""),
            "power_kw": s.get("power_kw") or 0.0,
            "soc_pct": soc_pct,
            "profit_eur": profit,
            "reason": s.get("reason", "") or "",
            "is_current": is_current,
            "ok": ok,
        })

        if start_dt is not None:
            last_dt = start_dt

    result.computed_profit_sum = computed_profit
    if result.slot_count > 0 and abs(computed_profit - result.total_expected_profit_eur) > 1e-3:
        result.mismatches.append("profit_sum")
        result.overall_ok = False

    terminal = schedule.get("terminal")
    if terminal is not None:
        result.terminal = _check_terminal(snap, terminal, slots, policy)
        if not result.terminal.ok:
            result.mismatches.append("terminal")
            result.overall_ok = False

    return result


def _check_terminal(
    snap: Snapshot, terminal: dict, slots: list[dict], policy: dict,
) -> TerminalCheck:
    """Recompute the schedule's two-tier terminal valuation from the snapshot.

    Mirrors ``pipeline.schedule`` independently: the flat value is the median
    positive plannable sell × efficiency × profitability tilt × discount; the
    reserve price is efficiency × median plannable buy, capped at cheapest buy
    + wear when grid charging is allowed, never below the flat value; the
    reserve size is the next ``load_reserve_days`` days' baseload not covered by
    same-hour forecast solar, plus the extra kWh, divided by efficiency.

    Args:
        snap: Coordinator snapshot.
        terminal: ``outputs.schedule.terminal`` block.
        slots: ``outputs.schedule.slots`` rows (horizon end = last slot end).
        policy: ``pipeline.schedule_policy`` block.

    Returns:
        TerminalCheck with declared and recomputed figures.
    """
    tc = TerminalCheck(
        enabled=bool(policy.get("load_reserve_enabled")),
        declared_flat_eur_kwh=terminal.get("flat_eur_kwh") or 0.0,
        declared_reserve_eur_kwh=terminal.get("reserve_eur_kwh") or 0.0,
        declared_reserve_kwh=terminal.get("reserve_kwh") or 0.0,
    )
    if not tc.enabled and tc.declared_reserve_kwh > _KWH_TOL:
        tc.ok = False  # switch off yet a reserve was held
        return tc

    eff = snap.config.get("round_trip_efficiency")
    pricing = snap.pipeline.get("pricing") or {}
    rows = [r for r in pricing.get("slots") or [] if r.get("priced") or r.get("forecast")]
    if eff is None or not rows:
        tc.skip_reason = "no efficiency or plannable prices in snapshot"
        return tc

    discount = policy.get("terminal_value_discount") or 0.0
    alpha = policy.get("profitability_tilt_alpha") or 0.0
    score = (snap.pipeline.get("profitability_score") or {}).get("score")
    score = 0.5 if score is None else score
    sells = [r["sell"] for r in rows if r["sell"] > 0]
    flat = 0.0
    if discount > 0 and sells:
        flat = median(sells) * eff * (1.0 + alpha * (1.0 - score)) * discount
    tc.computed_flat_eur_kwh = flat

    buys = [r["buy"] for r in rows]
    reserve_price = eff * median(buys)
    if policy.get("allow_grid_charging"):
        deg = snap.pipeline.get("degradation_cost_per_kwh") or 0.0
        reserve_price = min(reserve_price, min(buys) + deg)
    tc.computed_reserve_eur_kwh = max(reserve_price, flat)

    tc.ok = (
        abs(tc.computed_flat_eur_kwh - tc.declared_flat_eur_kwh) <= _EUR_TOL
        and abs(tc.computed_reserve_eur_kwh - tc.declared_reserve_eur_kwh) <= _EUR_TOL
    )
    if tc.enabled:
        tc.computed_reserve_kwh = _recompute_reserve_kwh(
            snap, slots, eff,
            days=int(policy.get("load_reserve_days") or _LOAD_RESERVE_DAYS),
            extra_kwh=policy.get("load_reserve_extra_kwh") or 0.0,
        )
        if tc.computed_reserve_kwh is None:
            tc.skip_reason = "reserve size not recomputable (forecast/baseload missing)"
        elif abs(tc.computed_reserve_kwh - tc.declared_reserve_kwh) > _KWH_TOL:
            tc.ok = False
    return tc


def _recompute_reserve_kwh(
    snap: Snapshot, slots: list[dict], eff: float, *, days: int, extra_kwh: float,
) -> float | None:
    """Recompute the load-reserve size from the snapshot's forecast and baseload.

    Args:
        snap: Coordinator snapshot.
        slots: Schedule slot rows; the last one's end is the horizon end.
        eff: Round-trip efficiency.
        days: Days after the horizon end covered (policy knob).
        extra_kwh: Extra AC kWh reserved on top of the house load.

    Returns:
        Storage-side kWh, or None when an input is missing.
    """
    forecast = snap.pipeline.get("forecast")
    base_load = snap.pipeline.get("base_load_profile")
    computed_at = (snap.outputs.get("schedule") or {}).get("computed_at")
    if not forecast or not base_load or not slots or not computed_at or eff <= 0:
        return None
    tz = ZoneInfo(snap.config.get("time_zone") or "UTC")
    load_by_hour = {s["hour"]: s["baseload_kw"] for s in base_load.get("slots") or []}

    today = datetime.fromisoformat(computed_at).astimezone(tz).date()
    keys = ("total_today_kwh", "total_tomorrow_kwh", "total_d2_kwh", "total_d3_kwh",
            "total_d4_kwh", "total_d5_kwh", "total_d6_kwh")
    totals = {today + timedelta(days=i): forecast.get(k) or 0.0 for i, k in enumerate(keys)}

    shape = [0.0] * 24
    for day in (today + timedelta(days=1), today):
        hourly = [0.0] * 24
        for g in forecast.get("slots") or []:
            local = datetime.fromisoformat(g["start"]).astimezone(tz)
            if local.date() == day:
                hourly[local.hour] += g["expected_kwh"]
        if sum(hourly) > 0:
            shape = [h / sum(hourly) for h in hourly]
            break

    horizon_end = datetime.fromisoformat(slots[-1]["end"])
    shortfall = 0.0
    for i in range(days * 24):
        local = (horizon_end + timedelta(hours=i)).astimezone(tz)
        total = totals.get(local.date(), 0.0)
        if total <= 0:
            continue  # no forecast for this day — skipped, as in the pipeline
        solar = shape[local.hour] * total
        shortfall += max(0.0, load_by_hour.get(local.hour, 0.0) - solar)
    return (shortfall + max(0.0, extra_kwh)) / eff


class ScheduleSlotsTable(Static):
    """DataTable: Time | Mode | SoC (%) | Power (kW) | Profit (€) | Reason."""

    DEFAULT_CSS = """
    ScheduleSlotsTable { height: auto; }
    ScheduleSlotsTable DataTable { height: 18; }
    """

    def __init__(self, sc: ScheduleCheckResult) -> None:
        """Initialise with the schedule check result.

        Args:
            sc: Result of check_schedule() containing per-slot mode, SoC, and profit data.
        """
        super().__init__()
        self._sc = sc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per schedule slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Mode", "SoC (%)", "Power (kW)", "Profit (€)", "Reason", "")
        dim = "dim"
        prev_date = None

        for row in self._sc.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(UTC)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 7)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 6)
                prev_date = cur_date

            is_current = row["is_current"]
            ok = row["ok"]
            profit = row["profit_eur"]
            profit_style = "green" if profit > 0 else ("red" if profit < 0 else dim)
            time_style = "bold cyan" if is_current else "cyan"
            prefix = "▶ " if is_current else "  "
            soc = row.get("soc_pct")
            soc_str = f"{soc:.1f}" if soc is not None else "—"

            table.add_row(
                Text(prefix + time_str, style=time_style),
                Text(row["mode"], style="bold" if is_current else ""),
                Text(soc_str, style=dim),
                Text(f"{row['power_kw']:.2f}", style=dim if row["power_kw"] == 0 else ""),
                Text(f"{profit:+.4f}", style=profit_style),
                Text(row["reason"][:40] if row["reason"] else "—", style=dim),
                Text("✗" if not ok else "", style="red"),
            )


class _SocSparkline(Static):
    """Sparkline showing projected battery SoC across all schedule slots."""

    DEFAULT_CSS = """
    _SocSparkline { height: auto; }
    _SocSparkline Sparkline { height: 5; }
    """

    def __init__(self, soc_values: list[float]) -> None:
        """Initialise with a list of per-slot SoC values (0.0–1.0).

        Args:
            soc_values: Projected SoC at end of each schedule slot.
        """
        super().__init__()
        self._soc_values = soc_values

    def compose(self) -> ComposeResult:
        """Yield a labelled Sparkline for the SoC series."""
        if not self._soc_values:
            yield Static("  (no SoC data)")
            return
        low = min(self._soc_values)
        high = max(self._soc_values)
        yield Static(
            f"  SoC trajectory  low={low*100:.1f}%  high={high*100:.1f}%",
            markup=False,
        )
        yield Sparkline(
            self._soc_values,
            summary_function=max,
            min_color="red",
            max_color="green",
        )


class ScheduleCheckWidget(Static):
    """Collapsible schedule deep-check: SoC trajectory chart, per-slot actions, power, and profit."""

    DEFAULT_CSS = "ScheduleCheckWidget { height: auto; }"

    def __init__(self, sc: ScheduleCheckResult) -> None:
        """Initialise with the pre-computed schedule check result.

        Args:
            sc: Result of check_schedule() for one coordinator.
        """
        super().__init__()
        self._sc = sc

    def compose(self) -> ComposeResult:
        """Render SoC chart and Slots sub-Collapsible with schedule data."""
        sc = self._sc

        if sc.skipped:
            yield Static(f"  ⚠  schedule_check   SKIP   {sc.skip_reason}")
            return

        color = "green" if sc.overall_ok else "red"
        mark = "✓" if sc.overall_ok else "✗"
        status = "PASS" if sc.overall_ok else "FAIL"
        profit_str = f"{sc.total_expected_profit_eur:+.4f}"
        cap_str = (
            f"  cap={sc.export_limit_kw:g}kW"
            if sc.export_limit_kw is not None else ""
        )
        title = (
            f"[{color}]{mark}[/{color}]  schedule_check   [{color}]{status}[/{color}]"
            f"   {sc.slot_count} slots  profit={profit_str}€{cap_str}"
        )

        with Collapsible(title=title, collapsed=True):
            profit_match = "profit_sum" not in sc.mismatches
            profit_style = "green" if profit_match else "red"
            yield Static(
                f"  profit: computed [{profit_style}]{sc.computed_profit_sum:+.4f}[/{profit_style}]€"
                f"  declared {sc.total_expected_profit_eur:+.4f}€",
                markup=True,
            )
            soc_values = [
                r["soc_pct"] / 100.0
                for r in sc.slot_rows
                if r.get("soc_pct") is not None
            ]
            tc = sc.terminal
            if tc is not None:
                t_style = "green" if tc.ok else "red"
                reserve = (
                    f"  reserve {tc.declared_reserve_kwh:.2f} kWh"
                    f" @ {tc.declared_reserve_eur_kwh:.4f}€"
                    if tc.enabled else "  load reserve off"
                )
                yield Static(
                    f"  terminal: [{t_style}]flat {tc.declared_flat_eur_kwh:.4f}€"
                    f"{reserve}[/{t_style}]"
                    + (f"  ({tc.skip_reason})" if tc.skip_reason else ""),
                    markup=True,
                )
            yield _SocSparkline(soc_values)
            with Collapsible(title="Slots", collapsed=False):
                yield ScheduleSlotsTable(sc)
