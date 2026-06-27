"""Tests for tools/integration_check.check_baked_observed."""
from __future__ import annotations

import sys
from pathlib import Path

# tools/ is a sibling of tests/, not a package — add to sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.integration_check import (  # noqa: E402
    Snapshot,
    check_baked_observed,
)


def _snap_with_baked_records(records: list[dict]) -> Snapshot:
    """Build a snapshot whose pipeline carries a baked_observed_history payload."""
    return Snapshot(
        entry_id="test",
        debug={"pipeline": {"baked_observed_history": {"records": records}}},
    )


def test_skipped_when_no_baked_history() -> None:
    """A snapshot with no pipeline.baked_observed_history is skipped."""
    snap = Snapshot(entry_id="test", debug={"pipeline": {}})
    result = check_baked_observed(snap)
    assert result.skipped is True
    assert result.record_count == 0


def test_ok_record_under_threshold() -> None:
    """A record whose |counter - baked| <= max(abs, rel × counter) is ``ok``."""
    snap = _snap_with_baked_records([{
        "date": "2024-01-14",
        "side_id": "generation",
        "counter_total_used": 10.0,
        "source_kind": "dedicated_sensor",
        "baked_sum": 10.05,
        "slots": [],
    }])
    result = check_baked_observed(snap)
    row = result.rows[0]
    assert row.status == "ok"
    assert result.overall_ok is True
    assert result.fault_count == 0


def test_fault_above_relative_threshold() -> None:
    """A 10% divergence on generation (rel_tol=2%) is flagged as fault."""
    snap = _snap_with_baked_records([{
        "date": "2024-01-14",
        "side_id": "generation",
        "counter_total_used": 50.0,
        "source_kind": "dedicated_sensor",
        "baked_sum": 45.0,   # 10% off, threshold = max(0.2, 0.02*50) = 1.0
        "slots": [],
    }])
    result = check_baked_observed(snap)
    assert result.rows[0].status == "fault"
    assert result.fault_count == 1
    assert result.overall_ok is False


def test_fault_uses_absolute_floor_on_small_days() -> None:
    """On a tiny counter, |Δ| = 0.5 kWh exceeds the 0.2 floor → fault."""
    snap = _snap_with_baked_records([{
        "date": "2024-01-14",
        "side_id": "generation",
        "counter_total_used": 1.0,
        "source_kind": "snapshot",
        "baked_sum": 0.5,    # |Δ|=0.5, threshold = max(0.2, 0.02) = 0.2
        "slots": [],
    }])
    result = check_baked_observed(snap)
    assert result.rows[0].status == "fault"


def test_failed_no_source_status() -> None:
    """A failed_no_source record is counted but not as a fault."""
    snap = _snap_with_baked_records([{
        "date": "2024-01-14",
        "side_id": "generation",
        "counter_total_used": 0.0,
        "source_kind": "failed_no_source",
        "baked_sum": 0.0,
        "slots": [],
    }])
    result = check_baked_observed(snap)
    assert result.rows[0].status == "no_source"
    assert result.no_source_count == 1
    assert result.fault_count == 0
    assert result.overall_ok is True


def test_multi_side_independent_outcomes() -> None:
    """Multiple records — one ok, one fault, one no_source — counted separately."""
    snap = _snap_with_baked_records([
        {"date": "2024-01-14", "side_id": "generation",
         "counter_total_used": 10.0, "source_kind": "dedicated_sensor",
         "baked_sum": 10.0, "slots": []},
        {"date": "2024-01-14", "side_id": "grid_import",
         "counter_total_used": 5.0, "source_kind": "snapshot",
         "baked_sum": 3.0, "slots": []},   # rel_tol=2% → 0.1 floor → |Δ|=2 → fault
        {"date": "2024-01-14", "side_id": "grid_export",
         "counter_total_used": 0.0, "source_kind": "failed_no_source",
         "baked_sum": 0.0, "slots": []},
    ])
    result = check_baked_observed(snap)
    assert result.record_count == 3
    assert result.fault_count == 1
    assert result.no_source_count == 1
    assert result.overall_ok is False
