"""Derived (consumption + losses) observed-series deep checks and widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from textual.app import ComposeResult
from textual.widgets import Collapsible, Static

from .snapshot import Snapshot


@dataclass
class ObservedConsumptionCheckResult:
    """Result of the derived-consumption deep-check.

    Validates per-slot non-negativity and that the declared
    yesterday / today totals match the slot sums. Slot values are
    derived from upstream ``derived_power_history`` samples by the
    formula ``max(0, backup + ac_port_signed + grid_net_signed)`` —
    we don't re-run the engine here but do verify the sample stream's
    presence and the slot-sum agreement.
    """

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0
    computed_yesterday_kwh: float = 0.0
    computed_today_kwh: float = 0.0
    derived_history_sample_count: int = 0
    derived_history_first_sample: str = ""
    derived_history_last_sample: str = ""
    energy_mismatch_count: int = 0
    computed_at: str = ""
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


@dataclass
class ObservedLossesCheckResult:
    """Result of the derived-losses deep-check.

    Mirrors the consumption check shape; the formula being verified is
    ``max(0, solar − battery_signed − ac_port_signed − backup)``.
    """

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_yesterday_kwh: float = 0.0
    total_today_so_far_kwh: float = 0.0
    computed_yesterday_kwh: float = 0.0
    computed_today_kwh: float = 0.0
    derived_history_sample_count: int = 0
    derived_history_first_sample: str = ""
    derived_history_last_sample: str = ""
    energy_mismatch_count: int = 0
    computed_at: str = ""
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


# Tolerance for declared-vs-recomputed slot-sum totals (kWh).


_DERIVED_TOTAL_TOL = 1e-3


def _check_derived_series(
    snap: Snapshot,
    *,
    series_key: str,
    slot_value_key: str,
) -> tuple[bool, dict]:
    """Shared validator for the two derived observed series (consumption / losses).

    Returns a generic result dict so each typed wrapper can fold it into its own
    dataclass. The two series share their input source (``derived_power_history``)
    and slot-sum-vs-total verification logic; only the per-slot field name and
    the snapshot key differ.

    Args:
        snap: Coordinator snapshot.
        series_key: ``pipeline`` key — ``"observed_consumption"`` or
            ``"observed_losses"``.
        slot_value_key: Per-slot kWh field — ``"consumed_kwh"`` or
            ``"losses_kwh"``.

    Returns:
        ``(skipped, fields)`` where ``fields`` is the data to copy into the
        typed result. ``skipped=True`` means no series was available.
    """
    series = snap.pipeline.get(series_key)
    if not series:
        return True, {"skip_reason": f"pipeline.{series_key} is null (no derived-power samples yet)"}

    total_yest = float(series.get("total_yesterday_kwh") or 0.0)
    total_today = float(series.get("total_today_so_far_kwh") or 0.0)
    computed_at = series.get("computed_at", "")

    history = snap.inputs.get("derived_power_history") or {}
    raw_samples = history.get("samples") or []
    sample_count = 0
    first_ts = last_ts = ""
    for s in raw_samples:
        ts = s.get("timestamp", "")
        if not ts:
            continue
        sample_count += 1
        if not first_ts:
            first_ts = ts
        last_ts = ts

    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    cy = ct = 0.0
    energy_mismatches = 0

    fields: dict = {
        "slot_count": 0,
        "total_yesterday_kwh": total_yest,
        "total_today_so_far_kwh": total_today,
        "computed_at": computed_at,
        "derived_history_sample_count": sample_count,
        "derived_history_first_sample": first_ts,
        "derived_history_last_sample": last_ts,
        "mismatches": [],
        "overall_ok": True,
    }

    slots = series.get("slots") or []
    fields["slot_count"] = len(slots)
    for s in slots:
        val = s.get(slot_value_key, 0.0)
        if val < -1e-6:
            energy_mismatches += 1
        start_str = s.get("start", "")
        try:
            start_dt = datetime.fromisoformat(start_str)
        except (ValueError, TypeError):
            continue
        d = start_dt.date()
        if d == yesterday:
            cy += val
        elif d == today:
            ct += val

    fields["computed_yesterday_kwh"] = round(cy, 4)
    fields["computed_today_kwh"] = round(ct, 4)
    fields["energy_mismatch_count"] = energy_mismatches

    if energy_mismatches:
        fields["mismatches"].append("negative_slot_values")
        fields["overall_ok"] = False
    if abs(cy - total_yest) > _DERIVED_TOTAL_TOL:
        fields["mismatches"].append("yesterday_total_vs_slots")
        fields["overall_ok"] = False
    if abs(ct - total_today) > _DERIVED_TOTAL_TOL:
        fields["mismatches"].append("today_total_vs_slots")
        fields["overall_ok"] = False
    return False, fields


def check_observed_consumption(snap: Snapshot) -> ObservedConsumptionCheckResult:
    """Validate ObservedConsumptionSeries: per-slot non-negativity + total↔slot sum.

    Args:
        snap: Coordinator snapshot containing ``pipeline.observed_consumption``
            and ``inputs.derived_power_history``.

    Returns:
        ObservedConsumptionCheckResult with overall pass/fail status.
    """
    result = ObservedConsumptionCheckResult()
    skipped, fields = _check_derived_series(
        snap, series_key="observed_consumption", slot_value_key="consumed_kwh",
    )
    if skipped:
        result.skipped = True
        result.skip_reason = fields["skip_reason"]
        return result
    for k, v in fields.items():
        setattr(result, k, v)
    return result


def check_observed_losses(snap: Snapshot) -> ObservedLossesCheckResult:
    """Validate ObservedLossesSeries: per-slot non-negativity + total↔slot sum.

    Args:
        snap: Coordinator snapshot containing ``pipeline.observed_losses``
            and ``inputs.derived_power_history``.

    Returns:
        ObservedLossesCheckResult with overall pass/fail status.
    """
    result = ObservedLossesCheckResult()
    skipped, fields = _check_derived_series(
        snap, series_key="observed_losses", slot_value_key="losses_kwh",
    )
    if skipped:
        result.skipped = True
        result.skip_reason = fields["skip_reason"]
        return result
    for k, v in fields.items():
        setattr(result, k, v)
    return result


class _DerivedSeriesCheckWidget(Static):
    """Shared collapsible widget for consumption / losses deep-checks.

    Subclasses bind the result dataclass + a human-readable label; this
    base handles the common skip / pass-fail / summary rendering.
    """

    DEFAULT_CSS = "_DerivedSeriesCheckWidget { height: auto; }"

    _label: str = ""

    def __init__(self, result) -> None:
        """Initialise with the pre-computed derived-series check result.

        Args:
            result: ObservedConsumptionCheckResult or ObservedLossesCheckResult.
        """
        super().__init__()
        self._r = result

    def compose(self) -> ComposeResult:
        """Render summary header and detail lines."""
        r = self._r
        if r.skipped:
            yield Static(f"  ⚠  {self._label}_check   SKIP   {r.skip_reason}")
            return

        color = "green" if r.overall_ok else "red"
        mark = "✓" if r.overall_ok else "✗"
        status = "PASS" if r.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  {self._label}_check   [{color}]{status}[/{color}]"
            f"   slots={r.slot_count}"
            f"   yday={r.total_yesterday_kwh:.3f}kWh"
            f"   today={r.total_today_so_far_kwh:.3f}kWh"
        )

        with Collapsible(title=title, collapsed=True):
            mismatches_str = ", ".join(r.mismatches) if r.mismatches else "none"
            yield Static(
                f"  Declared yesterday: {r.total_yesterday_kwh:.4f} kWh\n"
                f"  Computed yesterday: {r.computed_yesterday_kwh:.4f} kWh\n"
                f"  Declared today:     {r.total_today_so_far_kwh:.4f} kWh\n"
                f"  Computed today:     {r.computed_today_kwh:.4f} kWh\n"
                f"  Derived samples:    {r.derived_history_sample_count}"
                f" ({r.derived_history_first_sample} → {r.derived_history_last_sample})\n"
                f"  Negative slots:     {r.energy_mismatch_count}\n"
                f"  Computed at:        {r.computed_at}\n"
                f"  Mismatches:         {mismatches_str}",
                markup=True,
            )


class ObservedConsumptionCheckWidget(_DerivedSeriesCheckWidget):
    """Collapsible derived-consumption deep-check."""

    _label = "observed_consumption"


class ObservedLossesCheckWidget(_DerivedSeriesCheckWidget):
    """Collapsible derived-losses deep-check."""

    _label = "observed_losses"
