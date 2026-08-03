"""Capability deep check — the live control-point envelope handed to the planner.

Validates the capability seam (``docs/capability_seam.md``): that the caps the DP
was given equal ``min(configured, capable)`` per leg, and that the composed
discharge-to-grid ceiling really is the binding one of its three legs. A gap
between configured and planned is *expected* when the hardware advertises a lower
bound — the check asserts the arithmetic, not the absence of a shortfall.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot

# Tolerance for the min() reconciliation (kW). Generous enough to absorb the
# float round-trip through JSON, tight enough to catch a wrong leg.
_CAP_TOL_KW = 1e-6


@dataclass
class CapabilityCheckResult:
    """Result of the capability deep-check."""

    skipped: bool = False
    skip_reason: str = ""
    max_battery_charge_kw: float | None = None
    max_battery_discharge_kw: float | None = None
    max_export_kw: float | None = None
    max_grid_discharge_kw: float | None = None
    configured_max_discharge_to_grid_kw: float | None = None
    configured_export_limit_kw: float | None = None
    planned_max_discharge_to_grid_kw: float | None = None
    planned_export_limit_kw: float | None = None
    planned_max_battery_charge_kw: float | None = None
    planned_max_battery_discharge_kw: float | None = None
    degraded: list[str] = field(default_factory=list)
    binding_leg: str = "—"
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def _reduce(configured: float | None, capable: float | None) -> float | None:
    """Return the expected post-reduction cap (mirrors ``InverterCapability.reduce``)."""
    if configured is None:
        return capable
    if capable is None:
        return configured
    return min(configured, capable)


def _agrees(expected: float | None, actual: float | None) -> bool:
    """Return whether two optional kW values agree within ``_CAP_TOL_KW``."""
    if expected is None or actual is None:
        return expected is actual or expected == actual
    return abs(expected - actual) <= _CAP_TOL_KW


def check_capability(snap: Snapshot) -> CapabilityCheckResult:
    """Validate the planner's caps against the driver's live capability.

    Cross-checks ``pipeline.schedule_policy.effective`` (what the driver
    resolved, plus what the DP received) against
    ``pipeline.schedule_policy.max_discharge_to_grid_kw`` /
    ``export_limit_kw`` (what was configured).

    Args:
        snap: Coordinator snapshot containing pipeline.schedule_policy.

    Returns:
        CapabilityCheckResult with per-leg values and overall pass/fail.
    """
    result = CapabilityCheckResult()

    policy = snap.pipeline.get("schedule_policy") or {}
    if not policy:
        result.skipped = True
        result.skip_reason = "pipeline.schedule_policy is null"
        return result
    effective = policy.get("effective")
    if not effective:
        result.skipped = True
        result.skip_reason = (
            "schedule_policy.effective is null "
            "(no driver set up, or capability unresolvable)"
        )
        return result

    result.max_battery_charge_kw = effective.get("max_battery_charge_kw")
    result.max_battery_discharge_kw = effective.get("max_battery_discharge_kw")
    result.max_export_kw = effective.get("max_export_kw")
    result.max_grid_discharge_kw = effective.get("max_grid_discharge_kw")
    result.degraded = list(effective.get("degraded") or [])
    result.configured_max_discharge_to_grid_kw = policy.get("max_discharge_to_grid_kw")
    result.configured_export_limit_kw = policy.get("export_limit_kw")
    result.planned_max_discharge_to_grid_kw = effective.get(
        "planned_max_discharge_to_grid_kw",
    )
    result.planned_export_limit_kw = effective.get("planned_export_limit_kw")
    result.planned_max_battery_charge_kw = effective.get("planned_max_battery_charge_kw")
    result.planned_max_battery_discharge_kw = effective.get(
        "planned_max_battery_discharge_kw",
    )

    # 1. Discharge-to-grid must be min(configured, capable).
    expected_discharge = _reduce(
        result.configured_max_discharge_to_grid_kw, result.max_grid_discharge_kw,
    )
    if not _agrees(expected_discharge, result.planned_max_discharge_to_grid_kw):
        result.mismatches.append("planned_max_discharge_to_grid_kw")
        result.overall_ok = False

    # 2. Export limit must be min(configured, capable).
    expected_export = _reduce(result.configured_export_limit_kw, result.max_export_kw)
    if not _agrees(expected_export, result.planned_export_limit_kw):
        result.mismatches.append("planned_export_limit_kw")
        result.overall_ok = False

    # 3. The composed grid-discharge ceiling must be the binding one of its three
    #    legs (battery discharge current, RC setpoint, export cap). The RC leg is
    #    not exposed separately, so assert the composition is no greater than
    #    either leg that *is* exposed.
    legs = {
        "battery_discharge": result.max_battery_discharge_kw,
        "export": result.max_export_kw,
    }
    if result.max_grid_discharge_kw is not None:
        for name, leg in legs.items():
            if leg is not None and result.max_grid_discharge_kw > leg + _CAP_TOL_KW:
                result.mismatches.append(f"grid_discharge_exceeds_{name}")
                result.overall_ok = False
        binding = min(
            ((v, k) for k, v in {**legs, "rc_or_other": result.max_grid_discharge_kw}.items()
             if v is not None),
            default=None,
        )
        if binding is not None:
            result.binding_leg = binding[1]

    # 4. The battery legs the DP planned against must not exceed capability.
    #    (They may be lower — a configured limit below the hardware ceiling.)
    for label, planned, capable in (
        ("battery_charge", result.planned_max_battery_charge_kw,
         result.max_battery_charge_kw),
        ("battery_discharge", result.planned_max_battery_discharge_kw,
         result.max_battery_discharge_kw),
    ):
        if planned is not None and capable is not None and planned > capable + _CAP_TOL_KW:
            result.mismatches.append(f"planned_{label}_exceeds_capability")
            result.overall_ok = False

    # 5. Non-negative sanity on every resolved leg.
    for name, value in (
        ("max_battery_charge_kw", result.max_battery_charge_kw),
        ("max_battery_discharge_kw", result.max_battery_discharge_kw),
        ("max_export_kw", result.max_export_kw),
        ("max_grid_discharge_kw", result.max_grid_discharge_kw),
    ):
        if value is not None and value < 0:
            result.mismatches.append(f"negative_{name}")
            result.overall_ok = False

    return result


class CapabilityCheckWidget(Static):
    """Collapsible capability deep-check: per-leg ceilings and the planner reduction."""

    DEFAULT_CSS = "CapabilityCheckWidget { height: auto; }"

    def __init__(self, cc: CapabilityCheckResult) -> None:
        """Initialise with the pre-computed capability check result.

        Args:
            cc: Result of check_capability() for one coordinator.
        """
        super().__init__()
        self._cc = cc

    @staticmethod
    def _fmt(value: float | None) -> str:
        """Format an optional kW value for the table."""
        return "—" if value is None else f"{value:g}"

    def compose(self) -> ComposeResult:
        """Render the per-leg ceiling table and the configured→planned reduction."""
        cc = self._cc

        if cc.skipped:
            yield Static(f"  ⚠  capability_check   SKIP   {cc.skip_reason}")
            return

        color = "green" if cc.overall_ok else "red"
        mark = "✓" if cc.overall_ok else "✗"
        status = "PASS" if cc.overall_ok else "FAIL"
        degraded_str = f"  degraded={len(cc.degraded)}" if cc.degraded else ""
        title = (
            f"[{color}]{mark}[/{color}]  capability_check   [{color}]{status}[/{color}]"
            f"   grid_discharge={self._fmt(cc.max_grid_discharge_kw)}kW"
            f"  binding={cc.binding_leg}{degraded_str}"
        )

        with Collapsible(title=title, collapsed=True):
            table = DataTable(zebra_stripes=True)
            table.add_columns("leg", "capable kW")
            for name, value in (
                ("battery charge",    cc.max_battery_charge_kw),
                ("battery discharge", cc.max_battery_discharge_kw),
                ("export",            cc.max_export_kw),
                ("grid discharge",    cc.max_grid_discharge_kw),
            ):
                table.add_row(name, self._fmt(value))
            yield table

            reduction = DataTable(zebra_stripes=True)
            reduction.add_columns("cap", "configured", "planned")
            reduction.add_row(
                "max_discharge_to_grid_kw",
                self._fmt(cc.configured_max_discharge_to_grid_kw),
                self._fmt(cc.planned_max_discharge_to_grid_kw),
            )
            reduction.add_row(
                "export_limit_kw",
                self._fmt(cc.configured_export_limit_kw),
                self._fmt(cc.planned_export_limit_kw),
            )
            reduction.add_row(
                "battery_charge_kw", self._fmt(cc.max_battery_charge_kw),
                self._fmt(cc.planned_max_battery_charge_kw),
            )
            reduction.add_row(
                "battery_discharge_kw", self._fmt(cc.max_battery_discharge_kw),
                self._fmt(cc.planned_max_battery_discharge_kw),
            )
            yield reduction

            if cc.degraded:
                yield Static(
                    "  [yellow]unreadable bounds:[/yellow] " + ", ".join(cc.degraded),
                    markup=True,
                )
            if cc.mismatches:
                yield Static(
                    "  [red]mismatches:[/red] " + ", ".join(cc.mismatches),
                    markup=True,
                )
