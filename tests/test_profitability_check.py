"""Tests for tools/checks/profitability.py — today's day class mirrors weekends and holidays."""
from __future__ import annotations

import sys

import pytest

from tools.checks.profitability import check_profitability
from tools.checks.snapshot import Snapshot

MONDAY_STATEHOOD_DAY = "2026-02-16T12:00:00+02:00"  # a Lithuanian public holiday
TUESDAY = "2026-02-17T12:00:00+02:00"
SATURDAY = "2026-02-21T12:00:00+02:00"


def _check(today_class: str, computed_at: str, country: str | None = "LT"):
    """Run the check on a payload whose score was computed at ``computed_at``."""
    payload = {
        "config": {"time_zone": "Europe/Vilnius", "holiday_country": country},
        "pipeline": {"profitability_score": {
            "score": 0.5, "today_peak_eur_kwh": 0.0, "today_class": today_class,
            "window_days": 30, "class_medians": {}, "computed_at": computed_at,
        }},
    }
    return check_profitability(Snapshot(entry_id="e1", debug=payload))


def test_a_public_holiday_must_be_classed_holiday():
    pytest.importorskip("holidays")
    assert _check("holiday", MONDAY_STATEHOOD_DAY).overall_ok
    result = _check("weekday", MONDAY_STATEHOOD_DAY)
    assert (result.overall_ok, result.mismatches) == (False, ["today_class_mismatch"])
    assert _check("weekday", TUESDAY).overall_ok


def test_weekends_win_and_no_country_means_no_holidays():
    assert _check("weekend", SATURDAY).overall_ok
    assert not _check("weekday", SATURDAY).overall_ok
    assert _check("weekday", MONDAY_STATEHOOD_DAY, country=None).overall_ok


def test_the_class_is_not_judged_without_a_local_calendar(monkeypatch):
    monkeypatch.setitem(sys.modules, "holidays", None)  # import raises ImportError
    assert _check("holiday", MONDAY_STATEHOOD_DAY).overall_ok
    assert _check("weekday", MONDAY_STATEHOOD_DAY).overall_ok
