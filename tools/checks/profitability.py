"""Profitability-score deep check and its TUI widget."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

from textual.app import ComposeResult
from textual.widgets import Collapsible, Static

from .snapshot import Snapshot


def _resolve_local_tz(tz_name: str | None) -> tzinfo:
    """Return the tzinfo for an IANA name, falling back to UTC.

    Args:
        tz_name: IANA timezone name from the debug payload's config block
            (e.g. "Europe/Riga"); may be empty/None on older payloads.

    Returns:
        tzinfo for the name, or UTC when unset or unparseable.
    """
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:    # ZoneInfoNotFoundError + anything unexpected
        return timezone.utc


@dataclass
class ProfitabilityCheckResult:
    """Result of the profitability deep-check: score range and peak cross-check."""

    skipped: bool = False
    skip_reason: str = ""
    score: float | None = None
    today_peak_eur_kwh: float = 0.0
    today_class: str = ""
    window_days: int = 0
    class_medians: dict = field(default_factory=dict)
    computed_at: str = ""
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_profitability(snap: Snapshot) -> ProfitabilityCheckResult:
    """Validate profitability score range and cross-check today's peak against pipeline.pricing.

    Args:
        snap: Coordinator snapshot containing pipeline.profitability_score and pipeline.pricing.

    Returns:
        ProfitabilityCheckResult with score data and overall pass/fail.
    """
    result = ProfitabilityCheckResult()

    ps = snap.pipeline.get("profitability_score")
    if not ps:
        result.skipped = True
        result.skip_reason = "pipeline.profitability_score is null (insufficient price history)"
        return result

    result.score = ps.get("score")
    result.today_peak_eur_kwh = ps.get("today_peak_eur_kwh", 0.0)
    result.today_class = ps.get("today_class", "")
    result.window_days = ps.get("window_days", 0)
    result.class_medians = dict(ps.get("class_medians") or {})
    result.computed_at = ps.get("computed_at", "")

    if result.score is not None and not (0.0 <= result.score <= 1.0):
        result.mismatches.append("score_out_of_range")
        result.overall_ok = False

    # Cross-check: today's peak should equal max spot across today's pricing
    # slots, bucketed by *local* date to match the coordinator/profitability
    # scorer (both key on local midnight, not UTC).
    pricing = snap.pipeline.get("pricing")
    if pricing and result.today_peak_eur_kwh > 0:
        local_tz = _resolve_local_tz(snap.config.get("time_zone"))
        today_local = datetime.now(timezone.utc).astimezone(local_tz).date()
        today_spots = []
        for s in pricing.get("slots") or []:
            try:
                slot_date = datetime.fromisoformat(s["start"]).astimezone(local_tz).date()
            except (KeyError, ValueError):
                continue
            if slot_date == today_local:
                today_spots.append(s.get("spot", 0.0))
        if today_spots:
            expected_peak = max(today_spots)
            if abs(expected_peak - result.today_peak_eur_kwh) > 1e-3:
                result.mismatches.append("peak_mismatch")
                result.overall_ok = False

    return result


class ProfitabilityCheckWidget(Static):
    """Collapsible profitability deep-check: score, peak, and window stats."""

    DEFAULT_CSS = "ProfitabilityCheckWidget { height: auto; }"

    def __init__(self, pr: ProfitabilityCheckResult) -> None:
        """Initialise with the pre-computed profitability check result.

        Args:
            pr: Result of check_profitability() for one coordinator.
        """
        super().__init__()
        self._pr = pr

    def compose(self) -> ComposeResult:
        """Render score and window stats as a collapsible static block."""
        pr = self._pr

        if pr.skipped:
            yield Static(f"  ⚠  profitability_check   SKIP   {pr.skip_reason}")
            return

        color = "green" if pr.overall_ok else "red"
        mark = "✓" if pr.overall_ok else "✗"
        status = "PASS" if pr.overall_ok else "FAIL"
        score_str = f"{pr.score:.0%}" if pr.score is not None else "sparse"
        title = (
            f"[{color}]{mark}[/{color}]  profitability_check   [{color}]{status}[/{color}]"
            f"   score={score_str}  peak={pr.today_peak_eur_kwh:.4f}€/kWh"
            f"  class={pr.today_class}  window={pr.window_days}d"
        )

        with Collapsible(title=title, collapsed=True):
            medians_str = ", ".join(
                f"{k}={v:.4f}" for k, v in sorted(pr.class_medians.items())
            )
            peak_ok = "peak_mismatch" not in pr.mismatches
            peak_style = "green" if peak_ok else "red"
            yield Static(
                f"  Today class: {pr.today_class}\n"
                f"  Today peak: [{peak_style}]{pr.today_peak_eur_kwh:.4f}[/{peak_style}] €/kWh\n"
                f"  Score: {score_str}\n"
                f"  Window: {pr.window_days} days\n"
                f"  Class medians: {medians_str or '—'}\n"
                f"  Computed at: {pr.computed_at}",
                markup=True,
            )
