"""Inverter-mode band deep check and its TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


@dataclass
class InverterModeCheckResult:
    """Result of the inverter-mode deep-check: stitched history+plan timeline."""

    skipped: bool = False
    skip_reason: str = ""
    history_count: int = 0
    plan_count: int = 0
    # Unified history+plan bands (each ev/plan-band is one row); ordered chronologically.
    band_rows: list[dict] = field(default_factory=list)
    observed_mode: str | None = None
    observed_reg: int | None = None
    target_mode: str | None = None
    automation_enabled: bool = False
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_inverter_mode(snap: Snapshot) -> InverterModeCheckResult:
    """Stitch inverter_mode history+plan into a single timeline and validate ordering.

    History entries from the coordinator are mode-change events; each event runs
    until the next event's timestamp (or until ``now`` for the tail). Plan slots
    from the optimiser are 15-min storage modes; contiguous same-mode runs are
    collapsed into bands so the table is scannable rather than 99 rows of
    self_use/stand_by.

    Args:
        snap: Coordinator snapshot containing outputs.inverter_mode.

    Returns:
        InverterModeCheckResult with per-band rows, current observed reading,
        and any chronology/tail-vs-observed mismatches.
    """
    result = InverterModeCheckResult()

    block = snap.outputs.get("inverter_mode")
    if not block:
        result.skipped = True
        result.skip_reason = "outputs.inverter_mode is null"
        return result

    history = block.get("history") or []
    plan = block.get("plan") or []
    reading = block.get("reading") or {}

    result.history_count = len(history)
    result.plan_count = len(plan)
    result.observed_mode = reading.get("mode")
    result.observed_reg = reading.get("raw_state")
    result.automation_enabled = bool(block.get("automation_enabled"))

    now = datetime.now(timezone.utc)

    # ── History bands: stretch each event up to the next event's t (or now). ─
    history_bands: list[dict] = []
    last_t: datetime | None = None
    last_mode: str | None = None
    for i, ev in enumerate(history):
        start_str = ev.get("t", "") or ""
        mode = ev.get("mode", "") or ""
        try:
            start_dt: datetime | None = datetime.fromisoformat(start_str).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            start_dt = None

        if last_t is not None and start_dt is not None and start_dt < last_t:
            result.mismatches.append(f"history out of order at {start_str}")
            result.overall_ok = False
        if last_mode is not None and mode == last_mode:
            result.mismatches.append(f"consecutive duplicate mode '{mode}' at {start_str}")
            result.overall_ok = False
        if start_dt is not None:
            last_t = start_dt
        last_mode = mode

        if i + 1 < len(history):
            end_str = history[i + 1].get("t", "") or ""
            try:
                end_dt: datetime | None = datetime.fromisoformat(end_str).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                end_dt = None
        else:
            end_dt = now
            end_str = end_dt.isoformat()

        is_current = (
            start_dt is not None and end_dt is not None and start_dt <= now < end_dt
        )
        duration_s = (
            (end_dt - start_dt).total_seconds()
            if (start_dt is not None and end_dt is not None) else None
        )
        history_bands.append({
            "section":    "history",
            "start":      start_str,
            "end":        end_str,
            "mode":       ev.get("mode", "") or "",
            "reg":        ev.get("raw_state"),
            "duration_s": duration_s,
            "is_current": is_current,
        })

    # Tail of history must match the current observed reading.
    if history and result.observed_mode is not None:
        tail = history[-1].get("mode")
        if tail != result.observed_mode:
            result.mismatches.append(
                f"observed={result.observed_mode} != history tail={tail}"
            )
            result.overall_ok = False

    # ── Plan bands: collapse contiguous same-mode slots. ────────────────────
    plan_bands: list[dict] = []
    for slot in plan:
        slot_mode  = slot.get("mode", "") or ""
        slot_start = slot.get("start", "") or ""
        slot_end   = slot.get("end", "")   or ""
        try:
            slot_start_dt: datetime | None = datetime.fromisoformat(slot_start).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            slot_start_dt = None
        try:
            slot_end_dt: datetime | None = datetime.fromisoformat(slot_end).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            slot_end_dt = None

        if (
            plan_bands
            and plan_bands[-1]["mode"] == slot_mode
            and plan_bands[-1]["_end_dt"] == slot_start_dt
            and slot_start_dt is not None
        ):
            plan_bands[-1]["end"] = slot_end
            plan_bands[-1]["_end_dt"] = slot_end_dt
        else:
            plan_bands.append({
                "section":   "plan",
                "start":     slot_start,
                "end":       slot_end,
                "mode":      slot_mode,
                "reg":       None,
                "_start_dt": slot_start_dt,
                "_end_dt":   slot_end_dt,
            })

    # Finalise plan bands: derive duration + current flag, drop helper keys.
    for band in plan_bands:
        sd = band.pop("_start_dt")
        ed = band.pop("_end_dt")
        band["duration_s"] = (ed - sd).total_seconds() if (sd is not None and ed is not None) else None
        band["is_current"] = (sd is not None and ed is not None and sd <= now < ed)
        if band["is_current"] and result.target_mode is None:
            result.target_mode = band["mode"]

    result.band_rows = history_bands + plan_bands

    return result


class InverterModeBandsTable(Static):
    """DataTable: Section | From | To | Duration | Mode | Reg | ▶."""

    DEFAULT_CSS = """
    InverterModeBandsTable { height: auto; }
    InverterModeBandsTable DataTable { height: 22; }
    """

    def __init__(self, ic: InverterModeCheckResult) -> None:
        """Initialise with the inverter-mode check result.

        Args:
            ic: Result of check_inverter_mode() containing per-band rows.
        """
        super().__init__()
        self._ic = ic

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per band, plus a 'now' divider."""
        table = self.query_one(DataTable)
        table.add_columns("Sect", "From", "To", "Duration", "Mode", "Reg", "")
        dim = "dim"
        now_divider_emitted = False

        for row in self._ic.band_rows:
            section = row["section"]
            if section == "plan" and not now_divider_emitted:
                table.add_row(
                    Text("─── now ───", style="bold yellow"),
                    *[Text("─", style=dim)] * 6,
                )
                now_divider_emitted = True

            from_str = _fmt_band_time(row.get("start"))
            to_str   = _fmt_band_time(row.get("end"))
            dur_str  = _fmt_duration(row.get("duration_s"))
            mode     = row.get("mode") or ""
            reg      = row.get("reg")
            reg_str  = str(reg) if reg is not None else ""
            is_current = bool(row.get("is_current"))
            prefix = "▶ " if is_current else "  "

            section_style = "cyan" if section == "history" else "magenta"
            mode_style = "bold" if is_current else ""
            time_style = "bold cyan" if is_current else "cyan"

            table.add_row(
                Text(section, style=section_style),
                Text(prefix + from_str, style=time_style),
                Text(to_str, style="cyan"),
                Text(dur_str, style=dim if not is_current else ""),
                Text(mode, style=mode_style),
                Text(reg_str, style=dim),
                Text("●" if is_current else "", style="yellow"),
            )


def _fmt_band_time(value: object) -> str:
    """Format an ISO-8601 timestamp string as 'MM-dd HH:MM' for the bands table.

    Args:
        value: ISO-8601 timestamp string, or None / non-string sentinel.

    Returns:
        Formatted local-UTC string, or the raw input on parse failure.
    """
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return value
    return dt.strftime("%m-%d %H:%M")


def _fmt_duration(seconds: object) -> str:
    """Format a duration in seconds as a compact 'Xd Yh Zm' / 'Yh Zm' / 'Zm' string.

    Args:
        seconds: Duration in seconds, or None / non-numeric.

    Returns:
        Compact human-readable duration, or '—' when input is missing.
    """
    if not isinstance(seconds, (int, float)):
        return "—"
    total_min = int(seconds // 60)
    if total_min < 60:
        return f"{total_min}m"
    hours, mins = divmod(total_min, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


class InverterModeCheckWidget(Static):
    """Collapsible inverter-mode deep-check: stitched history+plan bands."""

    DEFAULT_CSS = "InverterModeCheckWidget { height: auto; }"

    def __init__(self, ic: InverterModeCheckResult) -> None:
        """Initialise with the pre-computed inverter-mode check result.

        Args:
            ic: Result of check_inverter_mode() for one coordinator.
        """
        super().__init__()
        self._ic = ic

    def compose(self) -> ComposeResult:
        """Render summary line and a Bands sub-Collapsible with history+plan table."""
        ic = self._ic

        if ic.skipped:
            yield Static(f"  ⚠  inverter_mode_check   SKIP   {ic.skip_reason}")
            return

        color = "green" if ic.overall_ok else "red"
        mark = "✓" if ic.overall_ok else "✗"
        status = "PASS" if ic.overall_ok else "FAIL"
        observed = ic.observed_mode or "—"
        target = ic.target_mode or "—"
        auto = "auto" if ic.automation_enabled else "manual"
        title = (
            f"[{color}]{mark}[/{color}]  inverter_mode_check   [{color}]{status}[/{color}]"
            f"   {ic.history_count} hist / {ic.plan_count} plan"
            f"   obs={observed}  →  target={target}  [{auto}]"
        )

        with Collapsible(title=title, collapsed=True):
            if ic.mismatches:
                for msg in ic.mismatches:
                    yield Static(f"  [red]✗[/red]  {msg}", markup=True)
            with Collapsible(title="Bands (history → now → plan)", collapsed=False):
                yield InverterModeBandsTable(ic)
