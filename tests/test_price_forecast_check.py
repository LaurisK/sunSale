"""Tests for the price-forecast integration check (``tools/checks/price_forecast.py``).

The check exists to catch the published week-ahead forecast drifting from the
sources it claims to summarise, so these tests corrupt each of those claims in
turn and assert the check notices.
"""
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from tools.integration_check import check_price_forecast

_TODAY = datetime.now(UTC).date()
_RESOLUTION_S = 900


def _pricing_slots(peak: float = 0.25, base: float = 0.05, days: int = 2) -> list[dict]:
    """Build settled price slots with a flat 17:00–20:00 peak."""
    slots = []
    midnight = datetime.combine(_TODAY, datetime.min.time(), UTC)
    for i in range(days * 96):
        start = midnight + timedelta(seconds=_RESOLUTION_S * i)
        # Four peak hours, so the 4 h band equals the peak exactly.
        spot = peak if 17 <= start.hour < 21 else base
        slots.append({"start": start.isoformat(), "spot": spot,
                      "buy": spot + 0.1, "sell": spot - 0.025, "export": None})
    return slots


def _day(horizon: int, source: str = "model", **overrides) -> dict:
    """Build one published forecast day, overridable field by field."""
    entry = {
        "day": (_TODAY + timedelta(days=horizon)).isoformat(),
        "day_class": "weekday",
        "horizon_days": horizon,
        "source": source,
        "peak_1h_eur_kwh": 0.25,
        "peak_4h_eur_kwh": 0.25,
        "trough_1h_eur_kwh": 0.05,
        "trough_4h_eur_kwh": 0.05,
        "spread_4h_eur_kwh": 0.20,
        "negative_hours": 0.0,
        "negative_generation_kwh": 0.0,
        "absorbable_kwh": 0.0,
        "surplus_kwh": 0.0,
        "confidence": 1.0,
    }
    entry.update(overrides)
    return entry


def _debug(days: list[dict] | None = None, **pf_overrides) -> dict:
    """Assemble a debug payload with pricing, forecast and price_forecast blocks."""
    if days is None:
        days = [_day(0, "actual"), _day(1, "actual")] + [_day(h) for h in range(2, 7)]
    price_forecast = {
        "computed_at": datetime.now(UTC).isoformat(),
        "shape_days": 120,
        "history_days": 300,
        "day_count": len(days),
        "days": days,
    }
    price_forecast.update(pf_overrides)
    return {
        "config": {"time_zone": "UTC"},
        "pipeline": {
            "pricing": {"resolution_s": _RESOLUTION_S, "slots": _pricing_slots()},
            "forecast": {f"total_d{n}_kwh": 40.0 for n in range(2, 7)}
                        | {"total_today_kwh": 40.0, "total_tomorrow_kwh": 40.0},
            "price_forecast": price_forecast,
        },
    }


class _Snap:
    """Minimal snapshot stub exposing the blocks the check reads."""

    def __init__(self, debug: dict) -> None:
        self.entry_id = "entry"
        self.debug = debug

    @property
    def config(self) -> dict:
        """Return the debug payload's config block."""
        return self.debug.get("config", {})

    @property
    def pipeline(self) -> dict:
        """Return the debug payload's pipeline block."""
        return self.debug.get("pipeline", {})


def test_reference_payload_passes():
    """A self-consistent forecast passes every cross-check."""
    result = check_price_forecast(_Snap(_debug()))
    assert result.overall_ok, result.mismatches
    assert result.day_count == 7
    assert result.actual_days == 2


def test_missing_block_skips_rather_than_failing():
    """A fresh install with no forecast yet is a skip, not a failure."""
    result = check_price_forecast(_Snap({"config": {}, "pipeline": {}}))
    assert result.skipped
    assert not result.mismatches


def test_actual_day_must_match_the_upstream_price_slots():
    """A settled day that disagrees with pipeline.pricing is caught."""
    days = [_day(0, "actual", peak_4h_eur_kwh=0.99, spread_4h_eur_kwh=0.94),
            _day(1, "actual")]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("peak_4h_mismatch" in m for m in result.mismatches)


def test_band_ordering_violation_is_caught():
    """A trough above its peak is physically impossible."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, trough_4h_eur_kwh=0.9, trough_1h_eur_kwh=0.9)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("band_ordering" in m for m in result.mismatches)


def test_engine_cannot_have_learned_from_days_the_store_lacks():
    """More learned days than stored ones means the two have drifted apart."""
    result = check_price_forecast(_Snap(_debug(shape_days=9999)))
    assert not result.overall_ok
    assert "shape_days_exceeds_history" in result.mismatches


def test_negative_generation_cannot_exceed_the_day_total():
    """PV exposed to negative prices is bounded by that day's forecast total."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, negative_generation_kwh=999.0)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("negative_generation_exceeds_total" in m for m in result.mismatches)


def test_spread_must_match_its_own_peak_and_trough():
    """A published spread inconsistent with the bands it summarises is caught."""
    days = [_day(0, "actual"), _day(1, "actual"), _day(2, spread_4h_eur_kwh=0.99)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("spread_mismatch" in m for m in result.mismatches)


def test_modelled_day_without_4h_history_passes():
    """A modelled day publishes a null 4 h band until that band has history."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, peak_4h_eur_kwh=None, trough_4h_eur_kwh=None, spread_4h_eur_kwh=None)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert result.overall_ok, result.mismatches


def test_settled_day_must_carry_the_4h_band():
    """A settled day always has full curves, so a null 4 h band there is a bug."""
    days = [_day(0, "actual", peak_4h_eur_kwh=None, trough_4h_eur_kwh=None,
                 spread_4h_eur_kwh=None),
            _day(1, "actual")]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("actual_4h_missing" in m for m in result.mismatches)


def test_missing_today_is_caught():
    """The horizon must start at today."""
    days = [_day(h) for h in range(1, 7)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert "missing_today" in result.mismatches


def test_day_and_horizon_must_agree():
    """A day label that does not match its horizon offset is caught."""
    bad = _day(3)
    bad["day"] = (_TODAY + timedelta(days=5)).isoformat()
    days = [_day(0, "actual"), _day(1, "actual"), bad]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("horizon_day_mismatch" in m for m in result.mismatches)


def test_out_of_range_values_are_caught():
    """Negative hours beyond a day and confidence beyond 1 are both invalid."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, negative_hours=30.0, confidence=2.0)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("negative_hours_range" in m for m in result.mismatches)
    assert any("confidence_range" in m for m in result.mismatches)


def test_duplicate_horizon_is_caught():
    """Two entries claiming the same horizon is a malformed payload."""
    days = [_day(0, "actual"), _day(1, "actual"), _day(2), copy.deepcopy(_day(2))]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("duplicate_horizon" in m for m in result.mismatches)


def test_split_must_add_up_to_the_figure_it_partitions():
    """absorbable + surplus that does not reconstruct negative generation is caught."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, negative_generation_kwh=10.0, absorbable_kwh=2.0, surplus_kwh=3.0)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("split_mismatch" in m for m in result.mismatches)


def test_negative_half_of_the_split_is_caught():
    """Neither half of the split can be negative."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, negative_generation_kwh=0.0, absorbable_kwh=5.0, surplus_kwh=-5.0)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("split_negative" in m for m in result.mismatches)


def test_half_reported_split_is_caught():
    """The split is reported as a pair or not at all."""
    days = [_day(0, "actual"), _day(1, "actual"), _day(2, surplus_kwh=None)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert not result.overall_ok
    assert any("split_half_missing" in m for m in result.mismatches)


def test_absent_split_is_accepted():
    """An install with no battery size reports neither half, and that is valid."""
    days = [_day(0, "actual", absorbable_kwh=None, surplus_kwh=None),
            _day(1, "actual", absorbable_kwh=None, surplus_kwh=None)]
    days += [_day(h, absorbable_kwh=None, surplus_kwh=None) for h in range(2, 7)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert result.overall_ok, result.mismatches


def test_total_surplus_is_reported():
    """The harness surfaces the week's unavoidable spill as a single figure."""
    days = [_day(0, "actual"), _day(1, "actual"),
            _day(2, negative_generation_kwh=10.0, absorbable_kwh=4.0, surplus_kwh=6.0),
            _day(3, negative_generation_kwh=5.0, absorbable_kwh=5.0, surplus_kwh=0.0)]
    result = check_price_forecast(_Snap(_debug(days)))
    assert result.surplus_kwh_total == pytest.approx(6.0)
