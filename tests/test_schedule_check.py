"""Tests for tools/integration_check.check_schedule — schedule_policy mirror."""
from __future__ import annotations

import sys
from pathlib import Path

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
