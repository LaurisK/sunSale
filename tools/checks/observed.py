"""Observed generation / grid / baked-in deep checks and their TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


@dataclass
class ObservedGenerationCheckResult:
    """Result of the observed-generation deep-check: PV samples → per-slot averages → totals.

    Cross-checks the full engine pipeline for the single ``generation`` side:

      * each published ``ObservedGenerationSlot.generated_kwh`` is rebuilt by
        averaging the raw ``pv_power_history`` samples that fall within
        ``[slot.start, slot.end)`` and multiplying by the slot duration;
      * the sum of today's slots must equal ``total_today_so_far_kwh``;
      * the sum of yesterday's slots must equal ``total_yesterday_kwh``;
      * yesterday's series total is compared against the inverter's
        authoritative day-total (``counter_total_used`` on the bake-in
        record), so a single failed check surfaces both engine drift and
        bake-in skew.
    """

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0
    computed_yesterday_kwh: float = 0.0
    computed_today_kwh: float = 0.0
    pv_history_sample_count: int = 0
    pv_history_first_sample: str = ""
    pv_history_last_sample: str = ""
    yesterday_baked_counter_total: float | None = None
    yesterday_baked_source_kind: str = ""
    yesterday_total_vs_counter_delta: float | None = None
    energy_mismatch_count: int = 0
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


# Tolerance for the yesterday-series vs inverter-counter cross-check. The
# bake-in step normally clamps |Δ| to zero modulo float rounding, but the
# series total is rounded to 4 decimals at the debug-view boundary, so a 10 Wh
# floor avoids spurious failures on small days.


_GEN_YDAY_COUNTER_TOL = 0.01


def check_observed_generation(snap: Snapshot) -> ObservedGenerationCheckResult:
    """Verify observed generation slots against PV samples, daily totals, and the bake counter.

    For each published slot, the raw ``pv_power_history`` samples falling
    inside ``[slot.start, slot.end)`` are averaged (in kW) and converted to
    kWh via the slot duration. Today's slots are always raw averages of the
    samples, so the reconstruction must match within rounding. Yesterday's
    slots come from a ``BakedDayRecord`` once the bake-in has run — the raw
    reconstruction is still surfaced but is not compared to the published
    value (the bake-in scales it).

    Cross-checks performed:
      * non-negative ``generated_kwh`` per slot,
      * per-slot reconstruction (today only),
      * ``sum(today_slots) == total_today_so_far_kwh``,
      * ``sum(yesterday_slots) == total_yesterday_kwh``,
      * ``total_yesterday_kwh ≈ counter_total_used`` from the matching baked
        record (the inverter's authoritative day-total).

    Args:
        snap: Coordinator snapshot containing ``pipeline.observed_generation``,
            ``inputs.pv_power_history``, and ``pipeline.baked_observed_history``.

    Returns:
        ObservedGenerationCheckResult with per-slot data and overall pass/fail.
    """
    result = ObservedGenerationCheckResult()

    og = snap.pipeline.get("observed_generation")
    if not og:
        result.skipped = True
        result.skip_reason = "pipeline.observed_generation is null (no inverter history yet)"
        return result

    result.slot_count = og.get("slot_count", 0)
    result.total_yesterday_kwh = og.get("total_yesterday_kwh", 0.0)
    result.total_today_so_far_kwh = og.get("total_today_so_far_kwh", 0.0)
    result.computed_at = og.get("computed_at", "")

    pv_history = snap.inputs.get("pv_power_history") or {}
    pv_samples: list[tuple[datetime, float]] = []
    for s in pv_history.get("samples") or []:
        try:
            pv_samples.append((
                datetime.fromisoformat(s.get("timestamp", "")),
                float(s.get("power_w", 0.0)),
            ))
        except (ValueError, TypeError):
            continue
    pv_samples.sort(key=lambda x: x[0])
    result.pv_history_sample_count = len(pv_samples)
    if pv_samples:
        result.pv_history_first_sample = pv_samples[0][0].isoformat()
        result.pv_history_last_sample = pv_samples[-1][0].isoformat()

    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    comp_yesterday = 0.0
    comp_today = 0.0
    energy_mismatches = 0

    for s in og.get("slots") or []:
        start_str = s.get("start", "")
        end_str = s.get("end", "")
        kwh = s.get("generated_kwh", 0.0)
        sign_ok = kwh >= -1e-6
        if not sign_ok:
            result.mismatches.append(f"negative_value:{start_str[:16]}")
            result.overall_ok = False

        try:
            bs_start = datetime.fromisoformat(start_str)
            bs_end = datetime.fromisoformat(end_str) if end_str else None
        except (ValueError, TypeError):
            bs_start = bs_end = None

        sample_count = 0
        expected_kwh = 0.0
        energy_ok = True
        if bs_start is not None and bs_end is not None:
            in_slot = [w for (ts, w) in pv_samples if bs_start <= ts < bs_end]
            sample_count = len(in_slot)
            if in_slot:
                duration_h = (bs_end - bs_start).total_seconds() / 3600.0
                avg_kw = sum(max(0.0, w) for w in in_slot) / len(in_slot) / 1000.0
                expected_kwh = avg_kw * duration_h
            # Yesterday's slots are baked (scaled) so the raw reconstruction
            # is not expected to match the published value. Compare only on
            # today's slots where the published value is the raw average.
            d_local = bs_start.date() if bs_start else None
            if d_local == today and in_slot:
                if abs(expected_kwh - kwh) > 1e-3:
                    energy_ok = False
                    energy_mismatches += 1

        d_utc = bs_start.date() if bs_start else None
        if d_utc == yesterday:
            comp_yesterday += kwh
        elif d_utc == today:
            comp_today += kwh

        result.slot_rows.append({
            "start": start_str,
            "generated_kwh": kwh,
            "expected_kwh": expected_kwh,
            "sample_count": sample_count,
            "sign_ok": sign_ok,
            "energy_ok": energy_ok,
        })

    result.computed_yesterday_kwh = round(comp_yesterday, 4)
    result.computed_today_kwh = round(comp_today, 4)
    result.energy_mismatch_count = energy_mismatches
    if energy_mismatches:
        result.mismatches.append(f"{energy_mismatches}_energy_mismatch")
        result.overall_ok = False

    TOL = 0.01
    if abs(comp_yesterday - result.total_yesterday_kwh) > TOL:
        result.mismatches.append("yesterday_total_mismatch")
        result.overall_ok = False
    if abs(comp_today - result.total_today_so_far_kwh) > TOL:
        result.mismatches.append("today_total_mismatch")
        result.overall_ok = False

    baked = snap.pipeline.get("baked_observed_history") or {}
    yesterday_str = yesterday.isoformat()
    for r in baked.get("records") or []:
        if r.get("side_id") == "generation" and r.get("date") == yesterday_str:
            counter_total = float(r.get("counter_total_used", 0.0))
            source_kind = r.get("source_kind", "")
            result.yesterday_baked_counter_total = counter_total
            result.yesterday_baked_source_kind = source_kind
            if source_kind != "failed_no_source":
                delta = abs(counter_total - result.total_yesterday_kwh)
                result.yesterday_total_vs_counter_delta = round(delta, 4)
                if delta > _GEN_YDAY_COUNTER_TOL:
                    result.mismatches.append("yesterday_vs_counter_mismatch")
                    result.overall_ok = False
            break

    return result


class ObservedGenerationSlotsTable(Static):
    """DataTable: Time | Gen kWh | Exp kWh | Samp | ✓/✗, grouped by date.

    ``Exp kWh`` is the kWh the harness rebuilds by averaging the raw PV power
    samples in the slot window — today's slots should match within rounding,
    yesterday's are baked so a divergence here is expected (the bake factor
    is what scales the raw values).
    """

    DEFAULT_CSS = """
    ObservedGenerationSlotsTable { height: auto; }
    ObservedGenerationSlotsTable DataTable { height: 22; }
    """

    def __init__(self, og: ObservedGenerationCheckResult) -> None:
        """Initialise with the observed generation check result.

        Args:
            og: Result of check_observed_generation() containing per-slot generated kWh
                and per-slot reconstruction from raw PV power samples.
        """
        super().__init__()
        self._og = og

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Gen kWh", "Exp kWh", "Samp", "")
        dim = "dim"
        prev_date = None

        for row in self._og.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 5)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 4)
                prev_date = cur_date

            kwh = row["generated_kwh"]
            expected = row["expected_kwh"]
            samples = row["sample_count"]
            ok = row["sign_ok"] and row["energy_ok"]

            base_style = dim if kwh == 0 else ""
            samples_style = "red" if samples == 0 else dim
            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{kwh:.4f}", style=base_style),
                Text(f"{expected:.4f}", style=dim),
                Text(str(samples), style=samples_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class ObservedGenerationCheckWidget(Static):
    """Collapsible observed-generation deep-check: inverter slots and daily totals."""

    DEFAULT_CSS = "ObservedGenerationCheckWidget { height: auto; }"

    def __init__(self, og: ObservedGenerationCheckResult) -> None:
        """Initialise with the pre-computed observed generation check result.

        Args:
            og: Result of check_observed_generation() for one coordinator.
        """
        super().__init__()
        self._og = og

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and yesterday/today totals."""
        og = self._og

        if og.skipped:
            yield Static(f"  ⚠  observed_generation_check   SKIP   {og.skip_reason}")
            return

        color = "green" if og.overall_ok else "red"
        mark = "✓" if og.overall_ok else "✗"
        status = "PASS" if og.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  observed_generation_check   [{color}]{status}[/{color}]"
            f"   {og.slot_count} slots"
            f"   yest={og.total_yesterday_kwh:.3f}kWh  today={og.total_today_so_far_kwh:.3f}kWh"
        )

        with Collapsible(title=title, collapsed=True):
            yest_ok = "yesterday_total_mismatch" not in og.mismatches
            today_ok = "today_total_mismatch" not in og.mismatches
            yest_style = "green" if yest_ok else "red"
            today_style = "green" if today_ok else "red"
            yield Static(
                f"  yesterday: computed [{yest_style}]{og.computed_yesterday_kwh:.4f}[/{yest_style}]kWh"
                f"  declared {og.total_yesterday_kwh:.4f}kWh\n"
                f"  today:     computed [{today_style}]{og.computed_today_kwh:.4f}[/{today_style}]kWh"
                f"  declared {og.total_today_so_far_kwh:.4f}kWh",
                markup=True,
            )

            counter = og.yesterday_baked_counter_total
            if counter is not None:
                vs_ok = "yesterday_vs_counter_mismatch" not in og.mismatches
                vs_style = "green" if vs_ok else "red"
                delta = og.yesterday_total_vs_counter_delta
                delta_str = f"{delta:.4f}" if delta is not None else "—"
                yield Static(
                    f"  yday vs inverter: series [{vs_style}]{og.total_yesterday_kwh:.4f}[/{vs_style}]kWh"
                    f"  counter {counter:.4f}kWh"
                    f"  |Δ|={delta_str}kWh  ({og.yesterday_baked_source_kind or 'no_record'})",
                    markup=True,
                )
            else:
                yield Static(
                    "  yday vs inverter: [dim]no bake record for yesterday yet[/dim]",
                    markup=True,
                )

            yield Static(
                f"  pv_power_history: {og.pv_history_sample_count} sample(s)"
                + (
                    f"  first={og.pv_history_first_sample}  last={og.pv_history_last_sample}"
                    if og.pv_history_sample_count else ""
                )
                + (
                    f"  energy_mismatches={og.energy_mismatch_count}"
                    if og.energy_mismatch_count else ""
                )
            )
            with Collapsible(title="Slots", collapsed=False):
                yield ObservedGenerationSlotsTable(og)


@dataclass
class ObservedGridCheckResult:
    """Result of the observed-grid deep-check: per-slot import/export vs upstream samples.

    Mirrors the generation deep-check for both grid sides (import + export):
    each side's published per-slot kWh is rebuilt from its own
    ``grid_*_power_history`` stream, the per-day totals are cross-checked
    against the slot sums, and yesterday's series total is cross-checked
    against the inverter's authoritative day-total stored on the bake-in
    record (``counter_total_used``) for each side independently.
    """

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_yesterday_imported_kwh: float = 0.0
    total_yesterday_exported_kwh: float = 0.0
    total_today_imported_kwh: float = 0.0
    total_today_exported_kwh: float = 0.0
    computed_yesterday_imported_kwh: float = 0.0
    computed_yesterday_exported_kwh: float = 0.0
    computed_today_imported_kwh: float = 0.0
    computed_today_exported_kwh: float = 0.0
    grid_history_sample_count: int = 0
    grid_history_first_sample: str = ""
    grid_history_last_sample: str = ""
    energy_mismatch_count: int = 0
    yesterday_import_counter_total: float | None = None
    yesterday_import_source_kind: str = ""
    yesterday_import_vs_counter_delta: float | None = None
    yesterday_export_counter_total: float | None = None
    yesterday_export_source_kind: str = ""
    yesterday_export_vs_counter_delta: float | None = None
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_observed_grid(snap: Snapshot) -> ObservedGridCheckResult:
    """Verify observed grid slot import/export against raw history + declared totals.

    Cross-checks every ObservedGridSlot:
      * imported_kwh ≥ 0 and exported_kwh ≥ 0
      * per-slot import/export reconstructed from
        ``inputs.grid_import_power_history`` and
        ``inputs.grid_export_power_history`` — each direction averages its
        own stream within [slot.start, slot.end). Per-cycle correction is
        gone in the redesign, so the reconstructed values should match the
        declared ones modulo rounding outside the today + bake-in window.
      * Sum of slot imports / exports matches each declared total for both
        yesterday and today_so_far.

    Args:
        snap: Coordinator snapshot containing ``pipeline.observed_grid`` and
            both ``inputs.grid_*_power_history`` entries.

    Returns:
        ObservedGridCheckResult with per-slot data and overall pass/fail.
    """
    result = ObservedGridCheckResult()

    og = snap.pipeline.get("observed_grid")
    if not og:
        result.skipped = True
        result.skip_reason = "pipeline.observed_grid is null (no grid power history yet)"
        return result

    result.slot_count = og.get("slot_count", 0)
    result.total_yesterday_imported_kwh = og.get("total_yesterday_imported_kwh", 0.0)
    result.total_yesterday_exported_kwh = og.get("total_yesterday_exported_kwh", 0.0)
    result.total_today_imported_kwh = og.get("total_today_imported_kwh", 0.0)
    result.total_today_exported_kwh = og.get("total_today_exported_kwh", 0.0)
    result.computed_at = og.get("computed_at", "")

    def _parse_history(key: str) -> list[tuple[datetime, float]]:
        """Parse a directional-power history payload into ``[(ts, kw)]``."""
        history = snap.inputs.get(key) or {}
        raw_samples = history.get("samples") or []
        parsed: list[tuple[datetime, float]] = []
        for s in raw_samples:
            try:
                parsed.append((
                    datetime.fromisoformat(s.get("timestamp", "")),
                    float(s.get("power_kw", 0.0)),
                ))
            except (ValueError, TypeError):
                continue
        parsed.sort(key=lambda x: x[0])
        return parsed

    imp_samples = _parse_history("grid_import_power_history")
    exp_samples = _parse_history("grid_export_power_history")
    parsed_samples = imp_samples + exp_samples
    parsed_samples.sort(key=lambda x: x[0])
    result.grid_history_sample_count = len(parsed_samples)
    if parsed_samples:
        result.grid_history_first_sample = parsed_samples[0][0].isoformat()
        result.grid_history_last_sample = parsed_samples[-1][0].isoformat()

    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    cy_imp = cy_exp = ct_imp = ct_exp = 0.0
    energy_mismatches = 0

    for s in og.get("slots") or []:
        start_str = s.get("start", "")
        end_str = s.get("end", "")
        imp = s.get("imported_kwh", 0.0)
        exp = s.get("exported_kwh", 0.0)
        sign_ok = imp >= -1e-6 and exp >= -1e-6
        if not sign_ok:
            result.mismatches.append(f"negative_value:{start_str[:16]}")
            result.overall_ok = False

        try:
            bs_start = datetime.fromisoformat(start_str)
            bs_end = datetime.fromisoformat(end_str)
        except (ValueError, TypeError):
            bs_start = bs_end = None

        sample_count = 0
        exp_imp = 0.0
        exp_exp = 0.0
        energy_ok = True
        if bs_start is not None and bs_end is not None:
            imp_in_slot = [kw for (ts, kw) in imp_samples if bs_start <= ts < bs_end]
            exp_in_slot = [kw for (ts, kw) in exp_samples if bs_start <= ts < bs_end]
            sample_count = len(imp_in_slot) + len(exp_in_slot)
            if imp_in_slot or exp_in_slot:
                duration_h = (bs_end - bs_start).total_seconds() / 3600.0
                avg_imp_kw = (sum(max(0.0, k) for k in imp_in_slot) / len(imp_in_slot)) if imp_in_slot else 0.0
                avg_exp_kw = (sum(max(0.0, k) for k in exp_in_slot) / len(exp_in_slot)) if exp_in_slot else 0.0
                exp_imp = avg_imp_kw * duration_h
                exp_exp = avg_exp_kw * duration_h
            # Today's slot values may be scaled by the end-of-day counter
            # correction, so per-slot energy mismatches are tolerated for
            # today; we still surface the comparison in the slot row.
            d_local = bs_start.date() if bs_start else None
            if d_local != today:
                if abs(exp_imp - imp) > 1e-3 or abs(exp_exp - exp) > 1e-3:
                    energy_ok = False
                    energy_mismatches += 1

        d_utc = bs_start.date() if bs_start else None
        if d_utc == yesterday:
            cy_imp += imp
            cy_exp += exp
        elif d_utc == today:
            ct_imp += imp
            ct_exp += exp

        result.slot_rows.append({
            "start": start_str,
            "imported_kwh": imp,
            "exported_kwh": exp,
            "expected_imported_kwh": exp_imp,
            "expected_exported_kwh": exp_exp,
            "sample_count": sample_count,
            "sign_ok": sign_ok,
            "energy_ok": energy_ok,
        })

    result.computed_yesterday_imported_kwh = round(cy_imp, 4)
    result.computed_yesterday_exported_kwh = round(cy_exp, 4)
    result.computed_today_imported_kwh = round(ct_imp, 4)
    result.computed_today_exported_kwh = round(ct_exp, 4)
    result.energy_mismatch_count = energy_mismatches
    if energy_mismatches:
        result.mismatches.append(f"{energy_mismatches}_energy_mismatch")
        result.overall_ok = False

    TOL = 0.01
    if abs(cy_imp - result.total_yesterday_imported_kwh) > TOL:
        result.mismatches.append("yesterday_imported_total_mismatch")
        result.overall_ok = False
    if abs(cy_exp - result.total_yesterday_exported_kwh) > TOL:
        result.mismatches.append("yesterday_exported_total_mismatch")
        result.overall_ok = False
    if abs(ct_imp - result.total_today_imported_kwh) > TOL:
        result.mismatches.append("today_imported_total_mismatch")
        result.overall_ok = False
    if abs(ct_exp - result.total_today_exported_kwh) > TOL:
        result.mismatches.append("today_exported_total_mismatch")
        result.overall_ok = False

    baked = snap.pipeline.get("baked_observed_history") or {}
    yesterday_str = yesterday.isoformat()
    for r in baked.get("records") or []:
        if r.get("date") != yesterday_str:
            continue
        side_id = r.get("side_id", "")
        counter_total = float(r.get("counter_total_used", 0.0))
        source_kind = r.get("source_kind", "")
        if side_id == "grid_import":
            result.yesterday_import_counter_total = counter_total
            result.yesterday_import_source_kind = source_kind
            if source_kind != "failed_no_source":
                delta = abs(counter_total - result.total_yesterday_imported_kwh)
                result.yesterday_import_vs_counter_delta = round(delta, 4)
                if delta > TOL:
                    result.mismatches.append("yesterday_import_vs_counter_mismatch")
                    result.overall_ok = False
        elif side_id == "grid_export":
            result.yesterday_export_counter_total = counter_total
            result.yesterday_export_source_kind = source_kind
            if source_kind != "failed_no_source":
                delta = abs(counter_total - result.total_yesterday_exported_kwh)
                result.yesterday_export_vs_counter_delta = round(delta, 4)
                if delta > TOL:
                    result.mismatches.append("yesterday_export_vs_counter_mismatch")
                    result.overall_ok = False

    return result


class ObservedGridSlotsTable(Static):
    """DataTable: Time | Imp kWh | Exp kWh | Exp Imp | Exp Exp | Samp | ✓/✗."""

    DEFAULT_CSS = """
    ObservedGridSlotsTable { height: auto; }
    ObservedGridSlotsTable DataTable { height: 22; }
    """

    def __init__(self, og: ObservedGridCheckResult) -> None:
        """Initialise with the observed grid check result.

        Args:
            og: Result of check_observed_grid() containing per-slot import/export.
        """
        super().__init__()
        self._og = og

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per slot, date-separated."""
        table = self.query_one(DataTable)
        table.add_columns(
            "Time", "Imp kWh", "Exp kWh", "Exp Imp", "Exp Exp", "Samp", "",
        )
        dim = "dim"
        prev_date = None

        for row in self._og.slot_rows:
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

            imp = row["imported_kwh"]
            exp = row["exported_kwh"]
            exp_imp = row["expected_imported_kwh"]
            exp_exp = row["expected_exported_kwh"]
            samples = row["sample_count"]
            ok = row["sign_ok"] and row["energy_ok"]

            zero_both = imp == 0.0 and exp == 0.0
            base_style = dim if zero_both else ""
            samples_style = "red" if samples == 0 else dim

            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{imp:.4f}", style=base_style),
                Text(f"{exp:.4f}", style=base_style),
                Text(f"{exp_imp:.4f}", style=dim),
                Text(f"{exp_exp:.4f}", style=dim),
                Text(str(samples), style=samples_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class ObservedGridCheckWidget(Static):
    """Collapsible observed-grid deep-check: per-slot import/export and daily totals."""

    DEFAULT_CSS = "ObservedGridCheckWidget { height: auto; }"

    def __init__(self, og: ObservedGridCheckResult) -> None:
        """Initialise with the pre-computed observed grid check result.

        Args:
            og: Result of check_observed_grid() for one coordinator.
        """
        super().__init__()
        self._og = og

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and import/export totals for yesterday/today."""
        og = self._og

        if og.skipped:
            yield Static(f"  ⚠  observed_grid_check   SKIP   {og.skip_reason}")
            return

        color = "green" if og.overall_ok else "red"
        mark = "✓" if og.overall_ok else "✗"
        status = "PASS" if og.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  observed_grid_check   [{color}]{status}[/{color}]"
            f"   {og.slot_count} slots"
            f"   yest_imp={og.total_yesterday_imported_kwh:.3f}/exp={og.total_yesterday_exported_kwh:.3f}kWh"
            f"   today_imp={og.total_today_imported_kwh:.3f}/exp={og.total_today_exported_kwh:.3f}kWh"
        )

        with Collapsible(title=title, collapsed=True):
            yest_imp_ok = "yesterday_imported_total_mismatch" not in og.mismatches
            yest_exp_ok = "yesterday_exported_total_mismatch" not in og.mismatches
            today_imp_ok = "today_imported_total_mismatch" not in og.mismatches
            today_exp_ok = "today_exported_total_mismatch" not in og.mismatches
            yi_s = "green" if yest_imp_ok else "red"
            ye_s = "green" if yest_exp_ok else "red"
            ti_s = "green" if today_imp_ok else "red"
            te_s = "green" if today_exp_ok else "red"
            yield Static(
                f"  yesterday imp: computed [{yi_s}]{og.computed_yesterday_imported_kwh:.4f}[/{yi_s}]kWh"
                f"  declared {og.total_yesterday_imported_kwh:.4f}kWh\n"
                f"  yesterday exp: computed [{ye_s}]{og.computed_yesterday_exported_kwh:.4f}[/{ye_s}]kWh"
                f"  declared {og.total_yesterday_exported_kwh:.4f}kWh\n"
                f"  today imp:     computed [{ti_s}]{og.computed_today_imported_kwh:.4f}[/{ti_s}]kWh"
                f"  declared {og.total_today_imported_kwh:.4f}kWh\n"
                f"  today exp:     computed [{te_s}]{og.computed_today_exported_kwh:.4f}[/{te_s}]kWh"
                f"  declared {og.total_today_exported_kwh:.4f}kWh",
                markup=True,
            )

            def _vs_counter_line(
                label: str,
                series_total: float,
                counter: float | None,
                source_kind: str,
                delta: float | None,
                mismatch_key: str,
            ) -> str:
                """Format one side's yday-series vs inverter-counter comparison line."""
                if counter is None:
                    return (
                        f"  yday {label} vs inverter: "
                        "[dim]no bake record for yesterday yet[/dim]"
                    )
                ok = mismatch_key not in og.mismatches
                style = "green" if ok else "red"
                delta_str = f"{delta:.4f}" if delta is not None else "—"
                return (
                    f"  yday {label} vs inverter: "
                    f"series [{style}]{series_total:.4f}[/{style}]kWh  "
                    f"counter {counter:.4f}kWh  |Δ|={delta_str}kWh  "
                    f"({source_kind or 'no_record'})"
                )

            yield Static(
                _vs_counter_line(
                    "imp", og.total_yesterday_imported_kwh,
                    og.yesterday_import_counter_total,
                    og.yesterday_import_source_kind,
                    og.yesterday_import_vs_counter_delta,
                    "yesterday_import_vs_counter_mismatch",
                ) + "\n" + _vs_counter_line(
                    "exp", og.total_yesterday_exported_kwh,
                    og.yesterday_export_counter_total,
                    og.yesterday_export_source_kind,
                    og.yesterday_export_vs_counter_delta,
                    "yesterday_export_vs_counter_mismatch",
                ),
                markup=True,
            )

            yield Static(
                f"  grid_*_power_history: {og.grid_history_sample_count} sample(s)"
                + (
                    f"  first={og.grid_history_first_sample}  last={og.grid_history_last_sample}"
                    if og.grid_history_sample_count else ""
                )
            )
            with Collapsible(title="Slots", collapsed=False):
                yield ObservedGridSlotsTable(og)


_BAKE_CHECK_TOLERANCES: dict[str, tuple[float, float]] = {
    "generation":  (0.2, 0.02),
    "grid_import": (0.1, 0.02),
    "grid_export": (0.1, 0.02),
}


@dataclass
class BakedObservedCheckRow:
    """Per-record summary line surfaced in the bake-in deep-check widget."""

    date_str: str = ""
    side_id: str = ""
    source_kind: str = ""
    counter_total_used: float = 0.0
    baked_sum: float = 0.0
    delta: float = 0.0
    threshold: float = 0.0
    status: str = ""    # "ok" | "fault" | "no_source"


@dataclass
class BakedObservedCheckResult:
    """Aggregate result of ``check_baked_observed``.

    Reports per-record status plus rollup counts. The bake-in is the
    authoritative pipeline-vs-inverter comparison; ``fault_count`` is the
    number of records whose ``|counter - baked| > max(abs, rel × counter)``.
    ``no_source_count`` covers records where the resolver failed and no
    comparison is possible.
    """

    skipped: bool = False
    skip_reason: str = ""
    record_count: int = 0
    fault_count: int = 0
    no_source_count: int = 0
    rows: list[BakedObservedCheckRow] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_baked_observed(snap: Snapshot) -> BakedObservedCheckResult:
    """Compare ``counter_total_used`` against ``baked_sum`` per baked record.

    Per record:
      * ``source_kind == "failed_no_source"`` → status ``no_source`` (not a fault,
        but counted separately so the rollup surfaces resolver failures).
      * otherwise: ``delta = |counter_total_used - baked_sum|``;
        ``threshold = max(abs_tol, rel_tol × counter_total_used)``;
        ``status = "fault"`` when ``delta > threshold``, else ``"ok"``.

    Args:
        snap: Coordinator snapshot containing ``pipeline.baked_observed_history``.

    Returns:
        ``BakedObservedCheckResult`` with per-record rows + aggregate counts.
    """
    result = BakedObservedCheckResult()

    baked = snap.pipeline.get("baked_observed_history")
    if not baked:
        result.skipped = True
        result.skip_reason = (
            "pipeline.baked_observed_history is null "
            "(no bake-in has run yet on this entry)"
        )
        return result

    records = baked.get("records") or []
    result.record_count = len(records)

    for r in records:
        side_id = r.get("side_id", "")
        source_kind = r.get("source_kind", "")
        counter_total = float(r.get("counter_total_used", 0.0))
        baked_sum = float(r.get("baked_sum", 0.0))

        if source_kind == "failed_no_source":
            result.no_source_count += 1
            result.rows.append(BakedObservedCheckRow(
                date_str=r.get("date", ""),
                side_id=side_id,
                source_kind=source_kind,
                counter_total_used=counter_total,
                baked_sum=baked_sum,
                delta=0.0,
                threshold=0.0,
                status="no_source",
            ))
            continue

        abs_tol, rel_tol = _BAKE_CHECK_TOLERANCES.get(side_id, (0.2, 0.02))
        threshold = max(abs_tol, rel_tol * counter_total)
        delta = abs(counter_total - baked_sum)
        is_fault = delta > threshold
        if is_fault:
            result.fault_count += 1
            result.mismatches.append(f"{r.get('date', '')}:{side_id}")
            result.overall_ok = False
        result.rows.append(BakedObservedCheckRow(
            date_str=r.get("date", ""),
            side_id=side_id,
            source_kind=source_kind,
            counter_total_used=counter_total,
            baked_sum=baked_sum,
            delta=delta,
            threshold=threshold,
            status="fault" if is_fault else "ok",
        ))

    return result


class BakedObservedRowsTable(Static):
    """DataTable: Date | Side | Source | Counter | Baked | |Δ| | Threshold | Status."""

    DEFAULT_CSS = """
    BakedObservedRowsTable { height: auto; }
    BakedObservedRowsTable DataTable { height: 20; }
    """

    def __init__(self, bk: BakedObservedCheckResult) -> None:
        """Initialise with the baked-observed check result.

        Args:
            bk: Result of ``check_baked_observed`` containing per-record rows.
        """
        super().__init__()
        self._bk = bk

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the table with one row per BakedDayRecord."""
        table = self.query_one(DataTable)
        table.add_columns("Date", "Side", "Source", "Counter", "Baked", "|Δ|", "Thr", "✓/✗")
        for r in self._bk.rows:
            style = "green" if r.status == "ok" else (
                "yellow" if r.status == "no_source" else "red"
            )
            mark = "✓" if r.status == "ok" else (
                "—" if r.status == "no_source" else "✗"
            )
            table.add_row(
                Text(r.date_str),
                Text(r.side_id),
                Text(r.source_kind, style="dim"),
                Text(f"{r.counter_total_used:.3f}"),
                Text(f"{r.baked_sum:.3f}"),
                Text(f"{r.delta:.3f}", style=style),
                Text(f"{r.threshold:.3f}", style="dim"),
                Text(mark, style=style),
            )


class BakedObservedCheckWidget(Static):
    """Collapsible bake-in deep-check: per (date, side) counter-vs-baked divergence."""

    DEFAULT_CSS = "BakedObservedCheckWidget { height: auto; }"

    def __init__(self, bk: BakedObservedCheckResult) -> None:
        """Initialise with the pre-computed baked-observed check result.

        Args:
            bk: Result of ``check_baked_observed`` for one coordinator.
        """
        super().__init__()
        self._bk = bk

    def compose(self) -> ComposeResult:
        """Render aggregate counts and the per-record table."""
        bk = self._bk

        if bk.skipped:
            yield Static(f"  ⚠  baked_observed_check   SKIP   {bk.skip_reason}")
            return

        color = "green" if bk.overall_ok and bk.no_source_count == 0 else (
            "yellow" if bk.overall_ok else "red"
        )
        mark = "✓" if bk.overall_ok else "✗"
        status = "PASS" if bk.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  baked_observed_check   [{color}]{status}[/{color}]"
            f"   {bk.record_count} record(s)"
            f"   faults={bk.fault_count}   no_source={bk.no_source_count}"
        )
        with Collapsible(title=title, collapsed=True):
            yield BakedObservedRowsTable(bk)
