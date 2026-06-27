"""Monthly-bill deep check and its TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover
    from backports.zoneinfo import ZoneInfo    # type: ignore[no-redef]

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


@dataclass
class MonthlyBillCheckResult:
    """Result of the monthly bill deep-check: total verification and per-slot cost."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    carry_eur: float = 0.0
    yday_to_now_eur: float = 0.0
    total_month_eur: float = 0.0
    today_cost_eur: float = 0.0
    yesterday_cost_eur: float = 0.0
    month_str: str = ""
    previous_month_str: str = ""
    previous_month_eur: float = 0.0
    finalized_day_count: int = 0
    pricing_mismatch_count: int = 0
    energy_mismatch_count: int = 0
    grid_history_sample_count: int = 0
    grid_history_first_sample: str = ""
    grid_history_last_sample: str = ""
    mismatches: list[str] = field(default_factory=list)
    slot_rows: list[dict] = field(default_factory=list)
    overall_ok: bool = True


def _local_midnight_utc(tz_name: str, days_ago: int) -> datetime | None:
    """Return the UTC instant of local midnight ``days_ago`` days before today.

    Args:
        tz_name: IANA timezone name from the debug payload (``config.time_zone``).
        days_ago: 0 for today's local midnight, 1 for yesterday's.

    Returns:
        UTC-aware datetime, or ``None`` when the timezone name is missing or
        unresolvable (the daily cross-checks are then skipped).
    """
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:    # pragma: no cover - defensive against bad tz strings
        return None
    d = datetime.now(tz).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)


def _local_date_of(iso_ts: str, tz_name: str) -> date | None:
    """Return the local calendar date of an ISO timestamp in ``tz_name``.

    Args:
        iso_ts: ISO-8601 timestamp string (the bill's ``computed_at``).
        tz_name: IANA timezone name from ``config.time_zone``.

    Returns:
        Local ``date``, or ``None`` when either input is missing/unparseable.
    """
    if not iso_ts or not tz_name:
        return None
    try:
        return datetime.fromisoformat(iso_ts).astimezone(ZoneInfo(tz_name)).date()
    except Exception:    # pragma: no cover - defensive against bad inputs
        return None


def _ds_date(date_str: str) -> date | None:
    """Return the parsed ``date`` for a "YYYY-MM-DD" ledger key, or ``None``."""
    try:
        return date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


def _slot_start(slot: dict) -> datetime | None:
    """Return a bill slot's parsed start datetime, or ``None`` when unparseable."""
    try:
        return datetime.fromisoformat(slot.get("start", ""))
    except (ValueError, TypeError):
        return None


def _slot_cost(slot: dict) -> float:
    """Return a bill slot's ``net_cost_eur`` (0.0 when absent)."""
    return slot.get("net_cost_eur", 0.0)


def check_monthly_bill(snap: Snapshot) -> MonthlyBillCheckResult:
    """Validate monthly bill totals, per-slot cost formulas, pricing, and energy alignment.

    Cross-checks (every BillSlot is verified, including zero-flow slots):
      * carry + yday_to_now == total_month_eur
      * sum(slot.net_cost_eur) == yday_to_now_eur
      * today_cost_eur == sum of slots starting at/after today's local midnight
      * yesterday_cost_eur == sum of slots in [yday_start, today_start)
        (verified only when the live window reaches back to yesterday)
      * per-slot net_cost_eur == imported_kwh*buy − exported_kwh*sell
        (no floor on sell — negative prices honoured)
      * per-slot imported_kwh / exported_kwh match the corresponding
        ``pipeline.observed_grid`` slot, scaled by the partial-overlap
        fraction when the window clips the slot edges.
      * each bill slot's buy/sell prices match the overlapping
        ``pipeline.pricing`` slot, verifying PriceSeries was applied faithfully.

    Builds a dense ``slot_rows`` list — one entry per BillSlot — so widgets can
    render every slot (including those with zero imported/exported) without
    hiding gaps in the upstream series.

    Args:
        snap: Coordinator snapshot containing pipeline.monthly_bill,
            pipeline.pricing, pipeline.observed_grid, and
            ``inputs.grid_import_power_history`` / ``grid_export_power_history``.

    Returns:
        MonthlyBillCheckResult with aggregation cross-checks, dense per-slot
        rows, and overall pass/fail.
    """
    result = MonthlyBillCheckResult()

    mb = snap.pipeline.get("monthly_bill")
    if not mb:
        result.skipped = True
        result.skip_reason = "pipeline.monthly_bill is null (no grid power history yet)"
        return result

    result.slot_count = mb.get("slot_count", 0)
    result.carry_eur = mb.get("carry_eur", 0.0)
    result.yday_to_now_eur = mb.get("yday_to_now_eur", 0.0)
    result.total_month_eur = mb.get("total_month_eur", 0.0)
    result.today_cost_eur = mb.get("today_cost_eur", 0.0)
    result.yesterday_cost_eur = mb.get("yesterday_cost_eur", 0.0)
    result.month_str = mb.get("month_str", "")
    result.previous_month_str = mb.get("previous_month_str", "")
    result.previous_month_eur = mb.get("previous_month_eur", 0.0)

    if abs((result.carry_eur + result.yday_to_now_eur) - result.total_month_eur) > 1e-4:
        result.mismatches.append("total_mismatch")
        result.overall_ok = False

    slots = mb.get("slots") or []
    slot_sum = sum(s.get("net_cost_eur", 0.0) for s in slots)
    if abs(slot_sum - result.yday_to_now_eur) > 1e-4:
        result.mismatches.append("yday_sum_mismatch")
        result.overall_ok = False

    # Carry is derived from the per-day ledger: it must equal the sum of the
    # current month's finalized-day entries that sit fully before the live
    # window, and previous_month_eur the sum of the previous month's entries.
    # The live-window-start date is taken from the payload's own computed_at
    # (not wall-clock now) so the cross-check cannot race the coordinator
    # across a local midnight.
    finalized = mb.get("finalized_days") or {}
    result.finalized_day_count = len(finalized)
    ref_local = _local_date_of(mb.get("computed_at", ""), snap.config.get("time_zone", ""))
    if ref_local is not None:
        yday_local = ref_local - timedelta(days=1)
        # Live window starts at yesterday's midnight, except on the 1st of the
        # month (yesterday is in the previous month) when it starts today.
        live_start_date = (
            yday_local if yday_local.strftime("%Y-%m") == result.month_str else ref_local
        )
        carry_from_ledger = sum(
            cost for ds, cost in finalized.items()
            if ds[:7] == result.month_str
            and _ds_date(ds) is not None and _ds_date(ds) < live_start_date
        )
        prev_from_ledger = sum(
            cost for ds, cost in finalized.items() if ds[:7] == result.previous_month_str
        )
        if abs(carry_from_ledger - result.carry_eur) > 1e-3:
            result.mismatches.append("carry_ledger_mismatch")
            result.overall_ok = False
        if result.previous_month_str and abs(prev_from_ledger - result.previous_month_eur) > 1e-3:
            result.mismatches.append("prev_month_ledger_mismatch")
            result.overall_ok = False

    # Daily breakdowns: today_cost_eur covers [today_start, now); it is always
    # a suffix of the live window, so it must equal the sum of bill slots
    # starting at or after today's local midnight. yesterday_cost_eur covers
    # [yday_start, today_start); it only overlaps the live window on a normal
    # day (on the 1st of a month yesterday sits in the prior month and is
    # outside the live slots), so that leg is verified only when the live
    # window reaches back to yesterday's midnight.
    today_start = _local_midnight_utc(snap.config.get("time_zone", ""), days_ago=0)
    if today_start is not None:
        yday_start = today_start - timedelta(days=1)
        today_sum = sum(
            _slot_cost(s) for s in slots if _slot_start(s) is not None
            and _slot_start(s) >= today_start
        )
        if abs(today_sum - result.today_cost_eur) > 1e-3:
            result.mismatches.append("today_cost_mismatch")
            result.overall_ok = False

        earliest = min((_slot_start(s) for s in slots if _slot_start(s)), default=None)
        if earliest is not None and earliest <= yday_start:
            yday_sum = sum(
                _slot_cost(s) for s in slots if _slot_start(s) is not None
                and yday_start <= _slot_start(s) < today_start
            )
            if abs(yday_sum - result.yesterday_cost_eur) > 1e-3:
                result.mismatches.append("yesterday_cost_mismatch")
                result.overall_ok = False

    pricing = snap.pipeline.get("pricing") or {}
    pricing_slots = pricing.get("slots") or []
    parsed_pricing: list[tuple[datetime, float, float]] = []
    for p in pricing_slots:
        try:
            parsed_pricing.append((
                datetime.fromisoformat(p.get("start", "")),
                float(p.get("buy", 0.0)),
                float(p.get("sell", 0.0)),
            ))
        except (ValueError, TypeError):
            continue
    parsed_pricing.sort(key=lambda x: x[0])

    parsed_samples: list[tuple[datetime, float]] = []
    for key in ("grid_import_power_history", "grid_export_power_history"):
        history = snap.inputs.get(key) or {}
        for s in history.get("samples") or []:
            try:
                parsed_samples.append((
                    datetime.fromisoformat(s.get("timestamp", "")),
                    float(s.get("power_kw", 0.0)),
                ))
            except (ValueError, TypeError):
                continue
    parsed_samples.sort(key=lambda x: x[0])
    result.grid_history_sample_count = len(parsed_samples)
    if parsed_samples:
        result.grid_history_first_sample = parsed_samples[0][0].isoformat()
        result.grid_history_last_sample = parsed_samples[-1][0].isoformat()

    observed_grid = snap.pipeline.get("observed_grid") or {}
    grid_slot_by_start: dict[datetime, dict] = {}
    for gs in observed_grid.get("slots") or []:
        try:
            grid_slot_by_start[datetime.fromisoformat(gs.get("start", ""))] = gs
        except (ValueError, TypeError):
            continue

    slot_formula_errors = 0
    pricing_mismatches = 0
    energy_mismatches = 0

    for s in slots:
        try:
            bs_start = datetime.fromisoformat(s.get("start", ""))
            bs_end = datetime.fromisoformat(s.get("end", ""))
        except (ValueError, TypeError):
            continue

        imp = s.get("imported_kwh", 0.0)
        exp = s.get("exported_kwh", 0.0)
        buy = s.get("buy_eur_kwh", 0.0)
        sell = s.get("sell_eur_kwh", 0.0)
        actual_cost = s.get("net_cost_eur", 0.0)
        expected_cost = imp * buy - exp * sell
        cost_ok = abs(expected_cost - actual_cost) <= 1e-4
        if not cost_ok:
            slot_formula_errors += 1

        match_pricing = None
        for i, (ps_start, _, _) in enumerate(parsed_pricing):
            next_start = parsed_pricing[i + 1][0] if i + 1 < len(parsed_pricing) else None
            if ps_start <= bs_start and (next_start is None or bs_start < next_start):
                match_pricing = parsed_pricing[i]
                break
        pricing_ok = True
        if match_pricing is not None:
            ps_start, exp_buy, exp_sell = match_pricing
            if abs(exp_buy - buy) > 1e-4 or abs(exp_sell - sell) > 1e-4:
                pricing_mismatches += 1
                pricing_ok = False

        # Cross-check against the upstream ObservedGridSeries slot. Bill
        # slots may be partial overlaps of a price slot (month-rollover
        # bridges, live window's leading edge), so pro-rate by the overlap
        # fraction matching the bill module's own calculation.
        gs = None
        if match_pricing is not None:
            gs = grid_slot_by_start.get(match_pricing[0])
        if gs is not None:
            try:
                ps_end_dt = datetime.fromisoformat(gs.get("end", ""))
                ps_start_dt = datetime.fromisoformat(gs.get("start", ""))
                full_secs = (ps_end_dt - ps_start_dt).total_seconds()
            except (ValueError, TypeError):
                full_secs = 0
            win_secs = (bs_end - bs_start).total_seconds()
            overlap_fraction = (win_secs / full_secs) if full_secs > 0 else 0.0
            exp_imp = gs.get("imported_kwh", 0.0) * overlap_fraction
            exp_exp = gs.get("exported_kwh", 0.0) * overlap_fraction
        else:
            exp_imp = 0.0
            exp_exp = 0.0
        sample_count = sum(1 for ts, _ in parsed_samples if bs_start <= ts < bs_end)
        energy_ok = abs(exp_imp - imp) <= 1e-3 and abs(exp_exp - exp) <= 1e-3
        if not energy_ok:
            energy_mismatches += 1

        result.slot_rows.append({
            "start": s.get("start", ""),
            "end": s.get("end", ""),
            "imported_kwh": imp,
            "exported_kwh": exp,
            "expected_imported_kwh": exp_imp,
            "expected_exported_kwh": exp_exp,
            "buy_eur_kwh": buy,
            "sell_eur_kwh": sell,
            "net_cost_eur": actual_cost,
            "expected_net_cost_eur": expected_cost,
            "sample_count": sample_count,
            "cost_ok": cost_ok,
            "pricing_ok": pricing_ok,
            "energy_ok": energy_ok,
        })

    if slot_formula_errors:
        result.mismatches.append(f"{slot_formula_errors}_slot_formula_mismatch")
        result.overall_ok = False
    result.pricing_mismatch_count = pricing_mismatches
    if pricing_mismatches:
        result.mismatches.append(f"{pricing_mismatches}_pricing_mismatch")
        result.overall_ok = False
    result.energy_mismatch_count = energy_mismatches
    if energy_mismatches:
        result.mismatches.append(f"{energy_mismatches}_energy_mismatch")
        result.overall_ok = False

    return result


class MonthlyBillSlotsTable(Static):
    """DataTable: Time | Imp kWh | Exp kWh | Buy € | Sell € | Net € | Samples | ✓/✗.

    Renders every slot — including those with zero imported/exported — so
    gaps in the source import / export histories are visible to the reviewer
    instead of being silently dropped from the breakdown.
    """

    DEFAULT_CSS = """
    MonthlyBillSlotsTable { height: auto; }
    MonthlyBillSlotsTable DataTable { height: 22; }
    """

    def __init__(self, mb: MonthlyBillCheckResult) -> None:
        """Initialise with the monthly bill check result.

        Args:
            mb: Result of check_monthly_bill() containing the dense slot_rows.
        """
        super().__init__()
        self._mb = mb

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per BillSlot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns(
            "Time", "Imp kWh", "Exp kWh", "Buy €", "Sell €", "Net €", "Samp", "",
        )
        dim = "dim"
        prev_date = None

        for row in self._mb.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 8)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 7)
                prev_date = cur_date

            imp = row["imported_kwh"]
            exp = row["exported_kwh"]
            buy = row["buy_eur_kwh"]
            sell = row["sell_eur_kwh"]
            net = row["net_cost_eur"]
            samples = row["sample_count"]
            ok = row["cost_ok"] and row["pricing_ok"] and row["energy_ok"]

            zero_imp_exp = imp == 0.0 and exp == 0.0
            base_style = dim if zero_imp_exp else ""
            samples_style = "red" if samples == 0 else dim

            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{imp:.4f}", style=base_style),
                Text(f"{exp:.4f}", style=base_style),
                Text(f"{buy:.4f}", style=dim),
                Text(f"{sell:.4f}", style=dim),
                Text(f"{net:.4f}", style=base_style),
                Text(str(samples), style=samples_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class MonthlyBillCheckWidget(Static):
    """Collapsible monthly bill deep-check: total verification and bill breakdown."""

    DEFAULT_CSS = "MonthlyBillCheckWidget { height: auto; }"

    def __init__(self, mb: MonthlyBillCheckResult) -> None:
        """Initialise with the pre-computed monthly bill check result.

        Args:
            mb: Result of check_monthly_bill() for one coordinator.
        """
        super().__init__()
        self._mb = mb

    def compose(self) -> ComposeResult:
        """Render bill summary, slot table, and mismatch status."""
        mb = self._mb

        if mb.skipped:
            yield Static(f"  ⚠  monthly_bill_check   SKIP   {mb.skip_reason}")
            return

        color = "green" if mb.overall_ok else "red"
        mark = "✓" if mb.overall_ok else "✗"
        status = "PASS" if mb.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  monthly_bill_check   [{color}]{status}[/{color}]"
            f"   {mb.slot_count} slots  carry={mb.carry_eur:.4f}€"
            f"  yday_to_now={mb.yday_to_now_eur:.4f}€  total={mb.total_month_eur:.4f}€"
        )

        with Collapsible(title=title, collapsed=True):
            mismatches_str = ", ".join(mb.mismatches) if mb.mismatches else "none"
            prev = (
                f"{mb.previous_month_str}: {mb.previous_month_eur:.4f} EUR"
                if mb.previous_month_str else "—"
            )
            grid_window = (
                f"{mb.grid_history_first_sample} → {mb.grid_history_last_sample}"
                if mb.grid_history_sample_count else "—"
            )
            yield Static(
                f"  Month: {mb.month_str}\n"
                f"  Carry (month start → yday start): {mb.carry_eur:.4f} EUR\n"
                f"  Live (since carry boundary): {mb.yday_to_now_eur:.4f} EUR\n"
                f"  Total month bill: {mb.total_month_eur:.4f} EUR\n"
                f"  Today cost: {mb.today_cost_eur:.4f} EUR\n"
                f"  Yesterday cost: {mb.yesterday_cost_eur:.4f} EUR\n"
                f"  Previous month: {prev}\n"
                f"  Finalized ledger days: {mb.finalized_day_count}\n"
                f"  Slots: {mb.slot_count}\n"
                f"  Grid power samples: {mb.grid_history_sample_count}  ({grid_window})\n"
                f"  Pricing mismatches: {mb.pricing_mismatch_count}\n"
                f"  Energy reconstruction mismatches: {mb.energy_mismatch_count}\n"
                f"  Mismatches: {mismatches_str}",
                markup=True,
            )
            with Collapsible(title="Slots", collapsed=False):
                yield MonthlyBillSlotsTable(mb)


# Categories handled by deep-check widgets; excluded from the plain validator display.
