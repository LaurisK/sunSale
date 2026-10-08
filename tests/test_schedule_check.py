"""Tests for tools/integration_check.check_schedule — schedule_policy mirror."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# tools/ is a sibling of tests/, not a package — add to sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.integration_check import (  # noqa: E402
    Snapshot,
    check_schedule,
)


def _snap(schedule_policy: dict | None) -> Snapshot:
    """Build a snapshot with a minimal one-slot schedule and the given policy."""
    pipeline: dict = {}
    if schedule_policy is not None:
        pipeline["schedule_policy"] = schedule_policy
    return Snapshot(
        entry_id="test",
        debug={
            "pipeline": pipeline,
            "outputs": {
                "schedule": {
                    "slots": [{
                        "start": "2024-01-15T00:00:00+00:00",
                        "end": "2024-01-15T01:00:00+00:00",
                        "mode": "Self-Use",
                        "power_kw": 0.0,
                        "expected_soc_after": 0.5,
                        "expected_profit_eur": 0.0,
                        "reason": "",
                    }],
                    "total_expected_profit_eur": 0.0,
                },
            },
        },
    )


def test_export_limit_recorded_from_policy() -> None:
    """The policy's export_limit_kw lands on the result and passes when >= 0."""
    result = check_schedule(_snap({"export_limit_kw": 10.0}))
    assert result.export_limit_kw == 10.0
    assert result.overall_ok is True


def test_export_limit_absent_stays_none() -> None:
    """No schedule_policy block → export_limit_kw stays None, check passes."""
    result = check_schedule(_snap(None))
    assert result.export_limit_kw is None
    assert result.overall_ok is True


def test_negative_export_limit_flagged() -> None:
    """A negative cap is nonsensical and must fail the check."""
    result = check_schedule(_snap({"export_limit_kw": -1.0}))
    assert result.export_limit_kw == -1.0
    assert result.overall_ok is False
    assert "export_limit_kw" in result.mismatches


# ---------------------------------------------------------------------------
# Terminal valuation recompute (load reserve)
# ---------------------------------------------------------------------------


def _terminal_snap(terminal: dict, *, enabled: bool = True) -> Snapshot:
    """Snapshot whose inputs make the terminal figures hand-computable.

    Plannable sells 0.1/0.2/0.3 (+ one negative) → median 0.2; flat =
    0.2 × 0.9 × (1 + 0.5 × 0.5) × 0.5 = 0.1125. Buys median 0.25 → 0.225,
    capped by grid charging at 0.10 + 0.04 = 0.14. Every day has a (tiny)
    forecast total but no slot shape, so no hour is covered; a flat 0.5 kW
    baseload → reserve = 72 h × 0.5 / 0.9 = 40 kWh.
    """
    snap = _snap({
        "allow_grid_charging": True,
        "load_reserve_enabled": enabled,
        "terminal_value_discount": 0.5,
        "profitability_tilt_alpha": 0.5,
    })
    snap.debug["config"] = {"time_zone": "UTC", "round_trip_efficiency": 0.9}
    snap.debug["pipeline"].update({
        "pricing": {"slots": [
            {"buy": b, "sell": s, "priced": p, "forecast": f}
            for b, s, p, f in (
                (0.20, 0.10, True, False), (0.30, 0.20, True, False),
                (0.40, 0.30, False, True), (0.10, -0.05, True, False),
                (9.99, 9.99, False, False),  # placeholder — never plannable
            )
        ]},
        "profitability_score": {"score": 0.5},
        "degradation_cost_per_kwh": 0.04,
        "forecast": {"slots": [], **{
            k: 1.0 for k in (
                "total_today_kwh", "total_tomorrow_kwh", "total_d2_kwh",
                "total_d3_kwh", "total_d4_kwh", "total_d5_kwh", "total_d6_kwh",
            )
        }},
        "base_load_profile": {"slots": [
            {"hour": h, "baseload_kw": 0.5} for h in range(24)
        ]},
    })
    schedule = snap.debug["outputs"]["schedule"]
    schedule["computed_at"] = "2024-01-15T00:00:00+00:00"
    schedule["terminal"] = terminal
    return snap


_GOOD_TERMINAL = {"flat_eur_kwh": 0.1125, "reserve_eur_kwh": 0.14, "reserve_kwh": 40.0}


def test_terminal_recompute_passes() -> None:
    """Declared figures matching the independent recompute pass."""
    result = check_schedule(_terminal_snap(dict(_GOOD_TERMINAL)))
    assert result.terminal is not None
    assert result.terminal.computed_flat_eur_kwh == pytest.approx(0.1125)
    assert result.terminal.computed_reserve_eur_kwh == pytest.approx(0.14)
    assert result.terminal.computed_reserve_kwh == pytest.approx(40.0)
    assert result.overall_ok is True


def test_terminal_price_mismatch_flagged() -> None:
    """A wrong reserve price fails the check."""
    result = check_schedule(_terminal_snap({**_GOOD_TERMINAL, "reserve_eur_kwh": 0.225}))
    assert result.overall_ok is False
    assert "terminal" in result.mismatches


def test_terminal_reserve_size_mismatch_flagged() -> None:
    """A wrong reserve size fails the check."""
    result = check_schedule(_terminal_snap({**_GOOD_TERMINAL, "reserve_kwh": 20.0}))
    assert result.overall_ok is False


def test_reserve_held_with_switch_off_flagged() -> None:
    """The switch off must mean no reserve at all."""
    result = check_schedule(_terminal_snap(dict(_GOOD_TERMINAL), enabled=False))
    assert result.overall_ok is False


def test_switch_off_zero_reserve_passes() -> None:
    """Switch off, reserve 0: prices still checked, size not recomputed."""
    result = check_schedule(
        _terminal_snap({**_GOOD_TERMINAL, "reserve_kwh": 0.0}, enabled=False),
    )
    assert result.overall_ok is True
    assert result.terminal.computed_reserve_kwh is None


def test_terminal_recompute_honours_days_and_extra() -> None:
    """Policy days and extra kWh feed the reserve recompute."""
    snap = _terminal_snap({**_GOOD_TERMINAL, "reserve_kwh": (5 * 24 * 0.5 + 9.0) / 0.9})
    snap.debug["pipeline"]["schedule_policy"].update(
        {"load_reserve_days": 5, "load_reserve_extra_kwh": 9.0},
    )
    result = check_schedule(snap)
    assert result.terminal.computed_reserve_kwh == pytest.approx((60.0 + 9.0) / 0.9)
    assert result.overall_ok is True


def test_terminal_recompute_skips_days_without_forecast() -> None:
    """Days the forecast has no total for add nothing, as in the pipeline.

    Window 2024-01-15 01:00 → 01-18 01:00. With tomorrow (01-16) and d2
    (01-17) zeroed, only today's 23 h and d3's first hour count.
    """
    expected = 24 * 0.5 / 0.9
    snap = _terminal_snap({**_GOOD_TERMINAL, "reserve_kwh": expected})
    forecast = snap.debug["pipeline"]["forecast"]
    forecast["total_tomorrow_kwh"] = 0.0
    forecast["total_d2_kwh"] = 0.0
    result = check_schedule(snap)
    assert result.terminal.computed_reserve_kwh == pytest.approx(expected)
    assert result.overall_ok is True


# ---------------------------------------------------------------------------
# Discharge gate invariants
# ---------------------------------------------------------------------------


def _gate_snap(gate: dict, *, enabled: bool = True, discharge_hours=(18,), sells=None) -> Snapshot:
    """Snapshot with one Discharge slot per hour in ``discharge_hours``.

    ``sells`` maps hour -> sell price in the pricing block (default 0.50).
    """
    snap = _snap({"discharge_gate_enabled": enabled, "allow_discharge_to_grid": True})
    snap.debug["config"] = {"time_zone": "UTC"}
    schedule = snap.debug["outputs"]["schedule"]
    schedule["computed_at"] = "2024-01-15T00:00:00+00:00"
    schedule["slots"] = [
        {
            "start": f"2024-01-15T{h:02d}:00:00+00:00",
            "end": f"2024-01-15T{h + 1:02d}:00:00+00:00",
            "mode": "discharge", "power_kw": 5.0, "expected_soc_after": 0.5,
            "expected_profit_eur": 0.0, "reason": "",
        }
        for h in discharge_hours
    ]
    schedule["gate"] = gate
    snap.debug["pipeline"]["pricing"] = {"slots": [
        {"start": f"2024-01-15T{h:02d}:00:00+00:00", "sell": (sells or {}).get(h, 0.50)}
        for h in range(24)
    ]}
    return snap


_GOOD_GATE = {
    "active": True, "average_eur_kwh": 0.30, "average_day": "2024-01-15",
    "overfill_kwh": 0.0, "overfill_slots": [],
}


def test_gate_check_passes_when_every_discharge_is_above_the_average() -> None:
    result = check_schedule(_gate_snap(dict(_GOOD_GATE)))
    assert result.gate is not None and result.gate.ok
    assert result.overall_ok is True


def test_gate_check_flags_a_discharge_below_the_average() -> None:
    result = check_schedule(_gate_snap(dict(_GOOD_GATE), sells={18: 0.10}))
    assert result.gate.below_average_discharges == 1
    assert "gate" in result.mismatches and result.overall_ok is False


def test_gate_check_accepts_a_forced_overfill_slot_below_the_average() -> None:
    gate = {**_GOOD_GATE, "overfill_kwh": 5.0,
            "overfill_slots": ["2024-01-15T18:00:00+00:00"]}
    result = check_schedule(_gate_snap(gate, sells={18: 0.10}))
    assert result.gate.ok


def test_gate_check_flags_an_overfill_slot_that_is_not_a_discharge_slot() -> None:
    gate = {**_GOOD_GATE, "overfill_kwh": 5.0,
            "overfill_slots": ["2024-01-15T20:00:00+00:00"]}
    result = check_schedule(_gate_snap(gate))
    assert not result.gate.ok
    assert any("not a Discharge" in p for p in result.gate.problems)


def test_gate_check_flags_a_stale_average() -> None:
    gate = {**_GOOD_GATE, "average_day": "2024-01-13"}
    result = check_schedule(_gate_snap(gate))
    assert not result.gate.ok


def test_gate_check_flags_a_gate_that_shaped_the_plan_while_off() -> None:
    result = check_schedule(_gate_snap(dict(_GOOD_GATE), enabled=False))
    assert not result.gate.ok


def test_gate_check_off_and_inactive_is_fine() -> None:
    gate = {**_GOOD_GATE, "active": False}
    result = check_schedule(_gate_snap(gate, enabled=False, sells={18: 0.01}))
    assert result.gate.ok


def test_gate_check_without_an_average_is_skipped_not_failed() -> None:
    gate = {**_GOOD_GATE, "active": False, "average_eur_kwh": None, "average_day": None}
    result = check_schedule(_gate_snap(gate))
    assert result.gate.ok and result.gate.skip_reason

