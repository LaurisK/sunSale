"""Tests for the capability integration check (``tools/checks/capability.py``).

The check's job is to catch the planner-side half of the capability seam
regressing: that the DP received ``min(configured, capable)`` per leg, and that
the composed discharge-to-grid ceiling is really the binding one of its legs.
"""
from __future__ import annotations

import copy

from tools.integration_check import check_capability

# Reference-install geometry: battery 200 A x 51.2 V = 10.24 kW, RC +/-10 kW,
# export 20 kW. RC binds, so a configured 15 kW must reach the DP as 10 kW.
_EFFECTIVE = {
    "max_battery_charge_kw": 10.24,
    "max_battery_discharge_kw": 10.24,
    "max_export_kw": 20.0,
    "max_grid_discharge_kw": 10.0,
    "resolved_at": "2026-08-03T12:00:00+00:00",
    "degraded": [],
    "planned_max_discharge_to_grid_kw": 10.0,
    "planned_export_limit_kw": 20.0,
}
_POLICY = {
    "max_discharge_to_grid_kw": 15.0,
    "export_limit_kw": 20.0,
    "effective": _EFFECTIVE,
}


class _Snap:
    """Minimal snapshot stub exposing only ``pipeline``."""

    def __init__(self, policy: dict | None) -> None:
        self.entry_id = "entry"
        self.outputs: dict = {}
        self.pipeline: dict = {"schedule_policy": policy} if policy is not None else {}


def _policy(**overrides) -> dict:
    """Return the reference policy with ``effective`` keys overridden."""
    policy = copy.deepcopy(_POLICY)
    policy["effective"].update(overrides)
    return policy


def test_passes_on_correctly_reduced_caps() -> None:
    result = check_capability(_Snap(_policy()))
    assert result.overall_ok
    assert result.mismatches == []
    assert result.max_grid_discharge_kw == 10.0


def test_skips_when_policy_absent() -> None:
    result = check_capability(_Snap(None))
    assert result.skipped
    assert "schedule_policy" in result.skip_reason


def test_skips_when_effective_block_absent() -> None:
    """A pre-v0.2.0 snapshot (or an unresolvable driver) must skip, not fail."""
    result = check_capability(_Snap({
        "max_discharge_to_grid_kw": 15.0, "export_limit_kw": 20.0, "effective": None,
    }))
    assert result.skipped
    assert "effective" in result.skip_reason


def test_fails_when_planner_cap_not_reduced() -> None:
    """The pre-fix bug: DP handed 15 kW while the hardware caps at 10 kW."""
    result = check_capability(_Snap(
        _policy(planned_max_discharge_to_grid_kw=15.0),
    ))
    assert not result.overall_ok
    assert "planned_max_discharge_to_grid_kw" in result.mismatches


def test_fails_when_export_cap_not_reduced() -> None:
    policy = _policy(max_export_kw=8.0)
    # planned stays 20.0 although capability says 8.0
    result = check_capability(_Snap(policy))
    assert not result.overall_ok
    assert "planned_export_limit_kw" in result.mismatches


def test_fails_when_composition_exceeds_a_leg() -> None:
    """Discharge-to-grid can never exceed the battery or export leg."""
    result = check_capability(_Snap(_policy(
        max_grid_discharge_kw=30.0, planned_max_discharge_to_grid_kw=15.0,
    )))
    assert not result.overall_ok
    assert "grid_discharge_exceeds_battery_discharge" in result.mismatches
    assert "grid_discharge_exceeds_export" in result.mismatches


def test_fails_on_negative_leg() -> None:
    result = check_capability(_Snap(_policy(max_battery_charge_kw=-1.0)))
    assert not result.overall_ok
    assert "negative_max_battery_charge_kw" in result.mismatches


def test_reports_degraded_roles_without_failing() -> None:
    """Unreadable bounds are surfaced but are not themselves a failure.

    A bound sunSale cannot read is an upstream outage, not a sunSale defect —
    the same distinction the seam draws everywhere else.
    """
    result = check_capability(_Snap(_policy(
        max_battery_charge_kw=None,
        degraded=["battery_max_charge_current"],
    )))
    assert result.overall_ok
    assert result.degraded == ["battery_max_charge_current"]


def test_unreduced_configured_cap_below_capability_passes() -> None:
    """A conservative operator cap must be preserved, not raised to capability."""
    policy = copy.deepcopy(_POLICY)
    policy["max_discharge_to_grid_kw"] = 3.0
    policy["effective"]["planned_max_discharge_to_grid_kw"] = 3.0
    result = check_capability(_Snap(policy))
    assert result.overall_ok


def test_fails_when_planned_battery_leg_exceeds_capability() -> None:
    """The DP must not budget 15 kW of battery transfer against a 10.24 kW ceiling.

    This is the charge-side half of the original over-planning bug: the export
    cap was reduced but ``BatteryConfig``'s power limits were not, so the DP's
    per-slot energy envelope stayed at the configured value.
    """
    result = check_capability(_Snap(_policy(
        planned_max_battery_charge_kw=15.0,
        planned_max_battery_discharge_kw=15.0,
    )))
    assert not result.overall_ok
    assert "planned_battery_charge_exceeds_capability" in result.mismatches
    assert "planned_battery_discharge_exceeds_capability" in result.mismatches


def test_passes_when_planned_battery_leg_below_capability() -> None:
    """A configured limit under the hardware ceiling is legitimate."""
    result = check_capability(_Snap(_policy(
        planned_max_battery_charge_kw=5.0,
        planned_max_battery_discharge_kw=5.0,
    )))
    assert result.overall_ok
