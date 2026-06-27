"""Base-load and household-consumption deep checks and their TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


@dataclass
class BaseLoadCheckResult:
    """Result of the base-load deep-check: 24 hourly slots, non-negative values, confidence."""

    skipped: bool = False
    skip_reason: str = ""
    fallback_kw: float = 0.0
    overall_p10_kw: float = 0.0
    overall_median_kw: float = 0.0
    confidence: float | None = None
    sample_count: int = 0
    distinct_days: int = 0
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


# P15 floor percentile — must match pipeline/base_load.DEFAULT_PERCENTILE.


_BASE_LOAD_PERCENTILE = 0.15

# Per-day per-hour completeness gate — must match
# CONSUMPTION_DAILY_MIN_HOUR_COMPLETENESS in contract/const.py.


_BASE_LOAD_MIN_HOUR_COMPLETENESS = 0.8

# Tolerance (kW) for the per-hour P15 cross-check against the daily buckets.
# Tighter than the typical floor magnitude so a mismatch is unambiguous.


_BASE_LOAD_KW_TOL = 1e-3


def check_base_load(snap: Snapshot) -> BaseLoadCheckResult:
    """Verify the base-load profile shape and cross-check P15 against daily buckets.

    Per-hour ``baseload_kw`` is recomputed from
    ``inputs.consumption_daily_buckets`` by replaying the P15 over the same
    qualified bucket-days and compared against the reported value. Reports
    a mismatch when the difference exceeds ``_BASE_LOAD_KW_TOL``. Buckets
    flagged ``is_fallback=True`` are skipped from the cross-check (their
    value comes from a different path).

    Args:
        snap: Coordinator snapshot containing pipeline.base_load_profile and
            inputs.consumption_daily_buckets.

    Returns:
        BaseLoadCheckResult with per-hour data and overall pass/fail.
    """
    result = BaseLoadCheckResult()

    blp = snap.pipeline.get("base_load_profile")
    if not blp:
        result.skipped = True
        result.skip_reason = "pipeline.base_load_profile is null"
        return result

    result.fallback_kw = blp.get("fallback_kw", 0.0)
    result.overall_p10_kw = blp.get("overall_p10_kw", 0.0)
    result.overall_median_kw = blp.get("overall_median_kw", 0.0)
    result.confidence = blp.get("confidence")
    result.sample_count = blp.get("sample_count", 0)
    result.distinct_days = blp.get("distinct_days", 0)
    result.computed_at = blp.get("computed_at", "")

    slots = blp.get("slots") or []
    if len(slots) != 24:
        result.mismatches.append(f"slot_count={len(slots)} (expected 24)")
        result.overall_ok = False

    if result.confidence is not None and not (0.0 <= result.confidence <= 1.0):
        result.mismatches.append("confidence_out_of_range")
        result.overall_ok = False

    buckets = (snap.inputs.get("consumption_daily_buckets") or {})
    records = buckets.get("records") or []
    qualified_per_hour: list[list[float]] = [[] for _ in range(24)]
    for r in records:
        kwh_list = r.get("hour_kwh") or []
        cov_list = r.get("hour_completeness") or []
        if len(kwh_list) != 24 or len(cov_list) != 24:
            continue
        for h in range(24):
            if float(cov_list[h]) >= _BASE_LOAD_MIN_HOUR_COMPLETENESS:
                qualified_per_hour[h].append(float(kwh_list[h]))

    record_count = len(records)
    if result.distinct_days != record_count:
        result.mismatches.append(
            f"distinct_days={result.distinct_days} (expected {record_count})"
        )
        result.overall_ok = False

    for s in slots:
        hour = s.get("hour", 0)
        kw = s.get("baseload_kw", 0.0)
        is_fallback = s.get("is_fallback", False)
        ok = kw >= 0.0
        if not ok:
            result.mismatches.append(f"negative_kw_h{hour}")
            result.overall_ok = False
        expected = None
        if not is_fallback:
            values = qualified_per_hour[hour] if 0 <= hour < 24 else []
            if values:
                expected = _percentile(values, _BASE_LOAD_PERCENTILE)
                if abs(expected - kw) > _BASE_LOAD_KW_TOL:
                    result.mismatches.append(
                        f"p15_mismatch_h{hour}: reported={kw:.4f} "
                        f"expected={expected:.4f} (n={len(values)})"
                    )
                    result.overall_ok = False
        result.slot_rows.append({
            "hour": hour,
            "baseload_kw": kw,
            "sample_count": s.get("sample_count", 0),
            "is_fallback": is_fallback,
            "expected_p15": expected,
            "ok": ok,
        })

    return result


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches pipeline/base_load._percentile).

    Args:
        values: Non-empty list of floats.
        p: Percentile in [0, 1].

    Returns:
        Interpolated percentile value, or 0.0 for empty input.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_v = sorted(values)
    n = len(sorted_v)
    rank = p * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac


class BaseLoadSlotsTable(Static):
    """DataTable: Hour | Baseload (kW) | Samples | Fallback? | ✓/✗."""

    DEFAULT_CSS = """
    BaseLoadSlotsTable { height: auto; }
    BaseLoadSlotsTable DataTable { height: 15; }
    """

    def __init__(self, bl: BaseLoadCheckResult) -> None:
        """Initialise with the base load check result.

        Args:
            bl: Result of check_base_load() containing per-hour baseload data.
        """
        super().__init__()
        self._bl = bl

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per hour (0–23)."""
        table = self.query_one(DataTable)
        table.add_columns("Hour", "Baseload (kW)", "Samples", "Fallback", "")
        dim = "dim"

        for row in self._bl.slot_rows:
            ok = row["ok"]
            fallback = row["is_fallback"]
            table.add_row(
                Text(f"{row['hour']:02d}:00"),
                Text(f"{row['baseload_kw']:.4f}", style="red" if not ok else ""),
                Text(str(row["sample_count"]), style=dim if row["sample_count"] == 0 else ""),
                Text("fallback" if fallback else "", style=dim),
                Text("✗" if not ok else "", style="red"),
            )


class BaseLoadCheckWidget(Static):
    """Collapsible base-load deep-check: 24-hour profile with confidence and metrics."""

    DEFAULT_CSS = "BaseLoadCheckWidget { height: auto; }"

    def __init__(self, bl: BaseLoadCheckResult) -> None:
        """Initialise with the pre-computed base load check result.

        Args:
            bl: Result of check_base_load() for one coordinator.
        """
        super().__init__()
        self._bl = bl

    def compose(self) -> ComposeResult:
        """Render metric summary and Slots sub-Collapsible."""
        bl = self._bl

        if bl.skipped:
            yield Static(f"  ⚠  base_load_check   SKIP   {bl.skip_reason}")
            return

        color = "green" if bl.overall_ok else "red"
        mark = "✓" if bl.overall_ok else "✗"
        status = "PASS" if bl.overall_ok else "FAIL"
        conf_str = f"{bl.confidence:.0%}" if bl.confidence is not None else "low"
        title = (
            f"[{color}]{mark}[/{color}]  base_load_check   [{color}]{status}[/{color}]"
            f"   median={bl.overall_median_kw:.3f}kW  P10={bl.overall_p10_kw:.3f}kW"
            f"   confidence={conf_str}  days={bl.distinct_days}"
        )

        with Collapsible(title=title, collapsed=True):
            with Collapsible(title="Slots (24h profile)", collapsed=False):
                yield BaseLoadSlotsTable(bl)


@dataclass
class HouseholdConsumptionCheckResult:
    """Result of the household-consumption deep-check: today-total kWh sanity."""

    skipped: bool = False
    skip_reason: str = ""
    consumption_today_kwh: float | None = None
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_household_consumption(snap: Snapshot) -> HouseholdConsumptionCheckResult:
    """Validate household consumption today-total is non-negative when present.

    Args:
        snap: Coordinator snapshot containing inputs.consumption_today_kwh.

    Returns:
        HouseholdConsumptionCheckResult with observed value and overall pass/fail.
    """
    result = HouseholdConsumptionCheckResult()

    raw = snap.inputs.get("consumption_today_kwh")
    if raw is None:
        result.skipped = True
        result.skip_reason = "inputs.consumption_today_kwh is null (sensor not configured)"
        return result

    try:
        result.consumption_today_kwh = float(raw)
    except (TypeError, ValueError):
        result.mismatches.append("non_numeric_value")
        result.overall_ok = False
        return result

    if result.consumption_today_kwh < 0.0:
        result.mismatches.append("negative_consumption")
        result.overall_ok = False

    return result


class HouseholdConsumptionCheckWidget(Static):
    """Collapsible household-consumption deep-check: today-total kWh sanity."""

    DEFAULT_CSS = "HouseholdConsumptionCheckWidget { height: auto; }"

    def __init__(self, hc: HouseholdConsumptionCheckResult) -> None:
        """Initialise with the pre-computed household consumption check result.

        Args:
            hc: Result of check_household_consumption() for one coordinator.
        """
        super().__init__()
        self._hc = hc

    def compose(self) -> ComposeResult:
        """Render consumption value and sanity status."""
        hc = self._hc

        if hc.skipped:
            yield Static(f"  ⚠  household_consumption_check   SKIP   {hc.skip_reason}")
            return

        color = "green" if hc.overall_ok else "red"
        mark = "✓" if hc.overall_ok else "✗"
        status = "PASS" if hc.overall_ok else "FAIL"
        kwh_str = f"{hc.consumption_today_kwh:.3f}kWh" if hc.consumption_today_kwh is not None else "—"
        title = (
            f"[{color}]{mark}[/{color}]  household_consumption_check   [{color}]{status}[/{color}]"
            f"   today={kwh_str}"
        )

        with Collapsible(title=title, collapsed=True):
            yield Static(f"  Consumption today: {kwh_str}")
