"""Export-guard deep check — the monitor-only battery-export guard + SoC-floor watch.

Validates the internal consistency of the ``export_guard`` / ``soc_floor_guard``
blocks the debug view exposes (``docs/battery_export_guard.md``): the state
matches the episode that is (or is not) in progress, a trip really outlasted the
persistence window, energies and peaks are sane, and a finished trip's
timestamps are ordered. The guard only observes, so the check cannot compare it
against a write — it asserts that what the panel shows can be trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot

_STATES = ("idle", "suspect", "tripped")


@dataclass
class ExportGuardCheckResult:
    """Result of the export-guard deep-check."""

    skipped: bool = False
    skip_reason: str = ""
    state: str = "—"
    trip_count: int = 0
    episode_onset: str | None = None
    episode_peak_kw: float | None = None
    episode_kwh: float | None = None
    last_trip_onset: str | None = None
    last_trip_kwh: float | None = None
    floor_in_breach: bool = False
    floor_breach_count: int = 0
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def _ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, returning ``None`` when absent or malformed."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fail(result: ExportGuardCheckResult, reason: str) -> None:
    """Record one mismatch and mark the result failed."""
    result.mismatches.append(reason)
    result.overall_ok = False


def _check_episode(
    result: ExportGuardCheckResult, ep: dict, label: str, persist_s: float,
    threshold_kw: float, finished: bool,
) -> None:
    """Validate one episode dict (in progress or finished).

    Args:
        result: Result to record mismatches on.
        ep: The episode block.
        label: ``episode`` or ``last_trip`` (prefix for mismatch names).
        persist_s: The guard's persistence window in seconds.
        threshold_kw: The guard's trip threshold.
        finished: Whether the episode must carry trip and clear times.
    """
    onset = _ts(ep.get("onset"))
    tripped = _ts(ep.get("tripped_at"))
    cleared = _ts(ep.get("cleared_at"))
    if onset is None:
        _fail(result, f"{label}_onset_missing")
        return
    if (ep.get("export_kwh") or 0.0) < 0:
        _fail(result, f"{label}_negative_energy")
    if (ep.get("peak_kw") or 0.0) < threshold_kw - 1e-6:
        _fail(result, f"{label}_peak_below_threshold")
    if tripped is not None and (tripped - onset).total_seconds() < persist_s - 1e-6:
        _fail(result, f"{label}_tripped_before_persistence")
    if finished:
        if tripped is None or cleared is None:
            _fail(result, f"{label}_missing_trip_or_clear_time")
        elif cleared < tripped:
            _fail(result, f"{label}_cleared_before_tripped")


def check_export_guard(snap: Snapshot) -> ExportGuardCheckResult:
    """Validate the export-guard and SoC-floor blocks of one debug snapshot.

    Args:
        snap: Coordinator snapshot.

    Returns:
        ExportGuardCheckResult with the guard state and overall pass/fail.
    """
    result = ExportGuardCheckResult()
    guard = snap.debug.get("export_guard")
    if not guard:
        result.skipped = True
        result.skip_reason = "export_guard is null (older build or metering-only)"
        return result

    state = guard.get("state")
    result.state = str(state)
    result.trip_count = int(guard.get("trip_count") or 0)
    persist_s = float(guard.get("persist_s") or 0)
    threshold_kw = float(guard.get("threshold_kw") or 0.0)
    episode = guard.get("episode")
    last_trip = guard.get("last_trip")

    if state not in _STATES:
        _fail(result, f"unknown_state:{state}")
    if state == "idle" and episode is not None:
        _fail(result, "idle_with_open_episode")
    if state in ("suspect", "tripped") and episode is None:
        _fail(result, f"{state}_without_episode")
    if state == "suspect" and episode and episode.get("tripped_at"):
        _fail(result, "suspect_episode_has_trip_time")
    if state == "tripped" and episode and not episode.get("tripped_at"):
        _fail(result, "tripped_episode_missing_trip_time")

    if episode:
        result.episode_onset = episode.get("onset")
        result.episode_peak_kw = episode.get("peak_kw")
        result.episode_kwh = episode.get("export_kwh")
        _check_episode(result, episode, "episode", persist_s, threshold_kw, finished=False)
    if last_trip:
        result.last_trip_onset = last_trip.get("onset")
        result.last_trip_kwh = last_trip.get("export_kwh")
        _check_episode(result, last_trip, "last_trip", persist_s, threshold_kw, finished=True)

    min_trips = (1 if last_trip else 0) + (1 if state == "tripped" else 0)
    if result.trip_count < min_trips:
        _fail(result, "trip_count_below_recorded_trips")

    floor = snap.debug.get("soc_floor_guard") or {}
    if floor:
        result.floor_in_breach = bool(floor.get("in_breach"))
        result.floor_breach_count = int(floor.get("breach_count") or 0)
        if result.floor_in_breach != (floor.get("since") is not None):
            _fail(result, "soc_floor_breach_flag_disagrees_with_since")
        if result.floor_breach_count < (1 if result.floor_in_breach else 0):
            _fail(result, "soc_floor_breach_count_too_low")
        seen = floor.get("min_soc_seen")
        if seen is not None and not 0.0 <= seen <= 1.0:
            _fail(result, "soc_floor_min_soc_out_of_range")

    return result


class ExportGuardCheckWidget(Static):
    """Collapsible export-guard deep-check: guard state, episodes, SoC-floor watch."""

    DEFAULT_CSS = "ExportGuardCheckWidget { height: auto; }"

    def __init__(self, ec: ExportGuardCheckResult) -> None:
        """Initialise with the pre-computed export-guard check result.

        Args:
            ec: Result of check_export_guard() for one coordinator.
        """
        super().__init__()
        self._ec = ec

    @staticmethod
    def _fmt(value: object) -> str:
        """Format an optional value for the table."""
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def compose(self) -> ComposeResult:
        """Render the guard state line and the episode table."""
        ec = self._ec
        if ec.skipped:
            yield Static(f"  ⚠  export_guard_check   SKIP   {ec.skip_reason}")
            return

        color = "green" if ec.overall_ok else "red"
        mark = "✓" if ec.overall_ok else "✗"
        status = "PASS" if ec.overall_ok else "FAIL"
        alert = "  [yellow]TRIPPED[/yellow]" if ec.state == "tripped" else ""
        breach = "  [yellow]SoC-floor breach[/yellow]" if ec.floor_in_breach else ""
        title = (
            f"[{color}]{mark}[/{color}]  export_guard_check   [{color}]{status}[/{color}]"
            f"   state={ec.state}  trips={ec.trip_count}{alert}{breach}"
        )
        with Collapsible(title=title, collapsed=True):
            table = DataTable(zebra_stripes=True)
            table.add_columns("episode", "onset", "peak kW", "kWh")
            table.add_row(
                "current", self._fmt(ec.episode_onset),
                self._fmt(ec.episode_peak_kw), self._fmt(ec.episode_kwh),
            )
            table.add_row(
                "last trip", self._fmt(ec.last_trip_onset), "—",
                self._fmt(ec.last_trip_kwh),
            )
            yield table
            yield Static(
                f"  SoC-floor watch: in_breach={ec.floor_in_breach} "
                f"breaches={ec.floor_breach_count}"
            )
            if ec.mismatches:
                yield Static(
                    "  [red]mismatches:[/red] " + ", ".join(ec.mismatches),
                    markup=True,
                )
