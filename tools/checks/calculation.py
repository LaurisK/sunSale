"""Calculation (solar attribution) deep check and its TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


@dataclass
class CalculationCheckResult:
    """Result of the calculation deep-check: per-slot solar attribution vs pricing."""

    skipped: bool = False
    skip_reason: str = ""
    module_slot_count: int = 0
    total_negative_sale_kwh: float = 0.0
    computed_neg_sale_kwh: float = 0.0
    computed_at: str = ""
    lockout_windows: list[dict] = field(default_factory=list)
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_calculation(snap: Snapshot) -> CalculationCheckResult:
    """Verify per-slot negative-sale solar attribution matches pricing.

    For each slot, ``expected_solar_negative_sale_kwh`` should equal
    ``expected_solar_kwh`` when the slot's sell price is non-positive and 0
    otherwise. The aggregate ``total_negative_sale_kwh`` should equal the sum
    of per-slot values.

    Args:
        snap: Coordinator snapshot containing pipeline.calculation and pipeline.pricing.

    Returns:
        CalculationCheckResult with per-slot rows and overall pass/fail.
    """
    result = CalculationCheckResult()

    calc = snap.pipeline.get("calculation")
    if not calc:
        result.skipped = True
        result.skip_reason = "pipeline.calculation is null"
        return result

    pricing = snap.pipeline.get("pricing")
    if not pricing:
        result.skipped = True
        result.skip_reason = "pipeline.pricing is null (needed for expected lockout attribution)"
        return result

    result.module_slot_count = calc.get("slot_count", 0)
    result.total_negative_sale_kwh = calc.get("total_negative_sale_kwh", 0.0)
    result.computed_at = calc.get("computed_at", "")
    result.lockout_windows = list(calc.get("feed_in_lockout_windows") or [])

    price_by_start: dict[str, float] = {
        s["start"]: s.get("sell", 0.0)
        for s in (pricing.get("slots") or [])
    }

    comp_neg_sale = 0.0
    for s in calc.get("slots") or []:
        start = s.get("start", "")
        sell_price = price_by_start.get(start)
        locked = sell_price is not None and sell_price <= 0.0
        solar_kwh = s.get("expected_solar_kwh", 0.0)
        neg_sale_kwh = s.get("expected_solar_negative_sale_kwh", 0.0)
        comp_neg_sale += neg_sale_kwh
        notes = list(s.get("notes") or [])

        if sell_price is None:
            ok = True
        else:
            expected_neg = solar_kwh if locked else 0.0
            ok = abs(neg_sale_kwh - expected_neg) <= 1e-4
        if not ok:
            result.mismatches.append(start)
            result.overall_ok = False
        result.slot_rows.append({
            "start": start,
            "sell_price": sell_price,
            "locked": locked,
            "solar_kwh": solar_kwh,
            "neg_sale_kwh": neg_sale_kwh,
            "notes": notes,
            "ok": ok,
        })

    result.computed_neg_sale_kwh = round(comp_neg_sale, 4)
    if result.total_negative_sale_kwh > 0 and abs(comp_neg_sale - result.total_negative_sale_kwh) > 0.01:
        result.mismatches.append("neg_sale_sum_mismatch")
        result.overall_ok = False

    return result


class CalculationSlotsTable(Static):
    """DataTable: Time | Sell (€) | Locked? | Solar (kWh) | Neg Sale (kWh) | Notes | ✓/✗."""

    DEFAULT_CSS = """
    CalculationSlotsTable { height: auto; }
    CalculationSlotsTable DataTable { height: 22; }
    """

    def __init__(self, cc: CalculationCheckResult) -> None:
        """Initialise with the calculation check result.

        Args:
            cc: Result of check_calculation() containing per-slot solar attribution.
        """
        super().__init__()
        self._cc = cc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per calculation slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Sell (€)", "Locked?", "Solar (kWh)", "Neg Sale (kWh)", "Notes", "")
        dim = "dim"
        prev_date = None

        for row in self._cc.slot_rows:
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

            ok = row["ok"]
            sp = row["sell_price"]
            sp_str = f"{sp:.4f}" if sp is not None else "—"
            locked_str = "✓" if row["locked"] else "✗"
            solar = row["solar_kwh"]
            neg = row["neg_sale_kwh"]
            neg_style = "" if ok else "red"
            notes_str = ", ".join(row["notes"])[:30] if row["notes"] else ""

            table.add_row(
                Text(time_str, style="cyan"),
                Text(sp_str, style=dim if sp is None or sp == 0.0 else ""),
                Text(locked_str, style=dim if not row["locked"] else ""),
                Text(f"{solar:.4f}", style=dim if solar == 0 else ""),
                Text(f"{neg:.4f}", style=neg_style if not ok else (dim if neg == 0 else "")),
                Text(notes_str, style=dim),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class CalculationCheckWidget(Static):
    """Collapsible calculation deep-check: negative-sale attribution + lockout windows."""

    DEFAULT_CSS = "CalculationCheckWidget { height: auto; }"

    def __init__(self, cc: CalculationCheckResult) -> None:
        """Initialise with the pre-computed calculation check result.

        Args:
            cc: Result of check_calculation() for one coordinator.
        """
        super().__init__()
        self._cc = cc

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible; list lockout windows when present."""
        cc = self._cc

        if cc.skipped:
            yield Static(f"  ⚠  calculation_check   SKIP   {cc.skip_reason}")
            return

        color = "green" if cc.overall_ok else "red"
        mark = "✓" if cc.overall_ok else "✗"
        status = "PASS" if cc.overall_ok else "FAIL"
        n_lock = len(cc.lockout_windows)
        neg = f"  neg_sale={cc.total_negative_sale_kwh:.3f}kWh" if cc.total_negative_sale_kwh else ""
        title = (
            f"[{color}]{mark}[/{color}]  calculation_check   [{color}]{status}[/{color}]"
            f"   {cc.module_slot_count} slots  {n_lock} lockout window(s){neg}"
        )

        with Collapsible(title=title, collapsed=True):
            if cc.total_negative_sale_kwh > 0:
                neg_ok = "neg_sale_sum_mismatch" not in cc.mismatches
                neg_style = "green" if neg_ok else "red"
                yield Static(
                    f"  neg_sale: computed [{neg_style}]{cc.computed_neg_sale_kwh:.4f}[/{neg_style}]kWh"
                    f"  declared {cc.total_negative_sale_kwh:.4f}kWh",
                    markup=True,
                )
            with Collapsible(title="Slots", collapsed=False):
                yield CalculationSlotsTable(cc)
            if cc.lockout_windows:
                lines = "  Lockout windows:\n" + "\n".join(
                    f"    {w['start']}  →  {w['end']}" for w in cc.lockout_windows
                )
                yield Static(lines)
