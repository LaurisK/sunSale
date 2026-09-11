"""Tests for the monitor-only battery-export guard and SoC-floor watch."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.outbound.export_guard import (
    STATE_IDLE,
    STATE_SUSPECT,
    STATE_TRIPPED,
    TRANSITION_CLEARED,
    TRANSITION_TRIPPED,
    ExportGuard,
    SocFloorWatch,
)

T0 = datetime(2026, 9, 9, 19, 5, tzinfo=UTC)


def _feed(guard: ExportGuard, seconds: range, grid_kw: float, pv_kw: float | None,
          mode: StorageMode = StorageMode.FeedIn) -> list[str | None]:
    """Feed one sample per ``seconds`` step and return the transitions."""
    return [
        guard.update(T0 + timedelta(seconds=s), mode, grid_kw, pv_kw, soc=0.21)
        for s in seconds
    ]


def test_sustained_battery_export_trips_after_persistence():
    guard = ExportGuard()
    transitions = _feed(guard, range(0, 130, 10), grid_kw=-10.0, pv_kw=0.0)
    assert transitions.index(TRANSITION_TRIPPED) == 12  # t = 120 s
    assert guard.state == STATE_TRIPPED
    ep = guard.episode
    assert ep is not None and ep.onset == T0
    assert ep.commanded_mode == "feed_in"
    assert ep.soc_at_onset == pytest.approx(0.21)
    assert ep.peak_kw == pytest.approx(10.0)


def test_single_transition_blip_does_not_trip():
    guard = ExportGuard()
    assert guard.update(T0, StorageMode.FeedIn, -10.0, 0.0) is None
    assert guard.state == STATE_SUSPECT
    assert guard.update(T0 + timedelta(seconds=10), StorageMode.FeedIn, 0.05, 0.0) is None
    assert guard.state == STATE_IDLE
    assert guard.episode is None


def test_export_covered_by_pv_is_not_battery_export():
    guard = ExportGuard()
    transitions = _feed(guard, range(0, 300, 10), grid_kw=-5.0, pv_kw=5.5)
    assert TRANSITION_TRIPPED not in transitions
    assert guard.state == STATE_IDLE


@pytest.mark.parametrize("mode", [StorageMode.Discharge, StorageMode.GridCharge, None])
def test_forced_or_unknown_mode_never_trips(mode):
    guard = ExportGuard()
    transitions = _feed(guard, range(0, 300, 10), grid_kw=-10.0, pv_kw=0.0, mode=mode)
    assert TRANSITION_TRIPPED not in transitions


def test_missing_pv_makes_the_sample_unevaluable():
    guard = ExportGuard()
    transitions = _feed(guard, range(0, 300, 10), grid_kw=-10.0, pv_kw=None)
    assert TRANSITION_TRIPPED not in transitions
    assert guard.state == STATE_IDLE


def test_trip_clears_after_quiet_period_and_integrates_energy():
    guard = ExportGuard()
    _feed(guard, range(0, 190, 10), grid_kw=-10.0, pv_kw=0.0)   # active 0..180 s
    quiet = _feed(guard, range(190, 260, 10), grid_kw=0.1, pv_kw=0.0)
    assert quiet.count(TRANSITION_CLEARED) == 1
    assert guard.state == STATE_IDLE
    trip = guard.last_trip
    assert trip is not None and trip.cleared_at is not None
    # 18 active intervals inside the episode plus the one ending at the first
    # quiet sample, each 10 s at 10 kW.
    assert trip.export_kwh == pytest.approx(19 * 10 * 10 / 3600)
    assert guard.as_dict()["trip_count"] == 1


def test_long_sample_gap_is_not_integrated():
    guard = ExportGuard()
    guard.update(T0, StorageMode.SelfUse, -4.0, 0.0)
    guard.update(T0 + timedelta(minutes=10), StorageMode.SelfUse, -4.0, 0.0)
    assert guard.episode is not None
    assert guard.episode.export_kwh == 0.0


def test_snapshot_is_attached_and_serialised():
    guard = ExportGuard()
    _feed(guard, range(0, 130, 10), grid_kw=-10.0, pv_kw=0.0)
    guard.attach_snapshot({"operating_mode": "4096"})
    out = guard.as_dict()
    assert out["state"] == STATE_TRIPPED
    assert out["episode"]["snapshot"] == {"operating_mode": "4096"}
    assert out["episode"]["tripped_at"] is not None
    assert out["persist_s"] == 120


def test_soc_floor_breach_and_recovery():
    watch = SocFloorWatch()
    assert watch.update(T0, StorageMode.SelfUse, 0.05, -2.5, min_soc=0.06) == "breach"
    assert watch.in_breach
    assert watch.update(T0 + timedelta(minutes=5), StorageMode.SelfUse, 0.04, -2.0, 0.06) is None
    assert watch.update(T0 + timedelta(minutes=10), StorageMode.SelfUse, 0.04, 0.0, 0.06) == "recovered"
    out = watch.as_dict()
    assert out["in_breach"] is False
    assert out["breach_count"] == 1
    assert out["last_breach"]["min_soc_seen"] == pytest.approx(0.04)


@pytest.mark.parametrize(
    ("mode", "soc", "battery_kw"),
    [
        (StorageMode.SelfUse, 0.05, 1.0),        # charging at the floor is recovery
        (StorageMode.SelfUse, 0.20, -3.0),       # discharging above the floor is normal
        (StorageMode.Discharge, 0.05, -3.0),     # forced mode is bounded by dispatch soc_min
        (StorageMode.SelfUse, None, -3.0),       # unknown SoC
    ],
)
def test_soc_floor_ignores_non_breaches(mode, soc, battery_kw):
    watch = SocFloorWatch()
    assert watch.update(T0, mode, soc, battery_kw, min_soc=0.06) is None
    assert not watch.in_breach
