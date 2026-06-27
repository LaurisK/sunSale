"""Schedule deep check, SoC sparkline, and schedule TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

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
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


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

    now = datetime.now(timezone.utc)
    last_dt: datetime | None = None
    computed_profit = 0.0

    for s in slots:
        start_str = s.get("start", "")
        end_str = s.get("end", start_str)

        try:
            start_dt: datetime | None = datetime.fromisoformat(start_str).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            start_dt = None

        try:
            end_dt: datetime | None = datetime.fromisoformat(end_str).astimezone(timezone.utc)
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

    return result


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
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
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
        title = (
            f"[{color}]{mark}[/{color}]  schedule_check   [{color}]{status}[/{color}]"
            f"   {sc.slot_count} slots  profit={profit_str}€"
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
            yield _SocSparkline(soc_values)
            with Collapsible(title="Slots", collapsed=False):
                yield ScheduleSlotsTable(sc)
