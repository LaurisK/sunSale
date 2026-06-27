"""Tests for inbound/pre_rollover_snapshot.py — pure Python."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import (
    CounterSnapshotHistory,
    CounterSnapshotRecord,
)
from custom_components.sun_sale.inbound.pre_rollover_snapshot import (
    maybe_capture_snapshots,
)


# Local timezone fixed at UTC throughout these tests so the window check
# matches the supplied wall-clock hours/minutes directly.
LOCAL_TZ = timezone.utc


@dataclass(frozen=True)
class _Reading:
    """Minimal stand-in for a counter reading exposing today_total_kwh."""
    today_total_kwh: float


def _empty() -> CounterSnapshotHistory:
    """Return a fresh empty history."""
    return CounterSnapshotHistory(records=())


def test_outside_window_no_snapshot_taken() -> None:
    """When ``now`` is outside the window, history is returned unchanged."""
    now = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    out = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(5.0))],
        now=now,
        local_tz=LOCAL_TZ,
    )
    assert out.records == ()


def test_inside_window_appends_snapshot_per_side() -> None:
    """All non-None readings produce records inside the window."""
    now = datetime(2024, 1, 15, 23, 45, tzinfo=timezone.utc)
    out = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[
            ("generation", _Reading(12.5)),
            ("grid_import", _Reading(3.2)),
            ("grid_export", _Reading(7.8)),
        ],
        now=now,
        local_tz=LOCAL_TZ,
    )
    by_side = {r.side_id: r for r in out.records}
    assert by_side["generation"].today_total_kwh == 12.5
    assert by_side["grid_import"].today_total_kwh == 3.2
    assert by_side["grid_export"].today_total_kwh == 7.8
    assert all(r.captured_at == now for r in out.records)


def test_none_reading_skipped() -> None:
    """A side with reading=None is not snapshotted."""
    now = datetime(2024, 1, 15, 23, 45, tzinfo=timezone.utc)
    out = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[
            ("generation", _Reading(5.0)),
            ("grid_import", None),
        ],
        now=now,
        local_tz=LOCAL_TZ,
    )
    assert len(out.records) == 1
    assert out.records[0].side_id == "generation"


def test_multiple_cycles_in_window_accumulate() -> None:
    """Two cycles both in window → two snapshots per side, both retained."""
    t1 = datetime(2024, 1, 15, 23, 35, tzinfo=timezone.utc)
    t2 = datetime(2024, 1, 15, 23, 55, tzinfo=timezone.utc)
    h1 = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(10.0))],
        now=t1,
        local_tz=LOCAL_TZ,
    )
    h2 = maybe_capture_snapshots(
        snapshot_history=h1,
        sources=[("generation", _Reading(10.5))],
        now=t2,
        local_tz=LOCAL_TZ,
    )
    assert [r.today_total_kwh for r in h2.records] == [10.0, 10.5]


def test_retention_prunes_old_records() -> None:
    """Records older than retention_days are dropped on the returned history."""
    now = datetime(2024, 1, 15, 23, 45, tzinfo=timezone.utc)
    old = CounterSnapshotRecord(
        side_id="generation",
        captured_at=now - timedelta(days=5),
        today_total_kwh=99.0,
    )
    out = maybe_capture_snapshots(
        snapshot_history=CounterSnapshotHistory(records=(old,)),
        sources=[("generation", _Reading(1.0))],
        now=now,
        local_tz=LOCAL_TZ,
        retention_days=2,
    )
    assert all(r.today_total_kwh != 99.0 for r in out.records)
    assert len(out.records) == 1
    assert out.records[0].today_total_kwh == 1.0


def test_retention_runs_outside_window_too() -> None:
    """Even when the window is closed, stale records are pruned."""
    now = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    old = CounterSnapshotRecord(
        side_id="generation",
        captured_at=now - timedelta(days=5),
        today_total_kwh=99.0,
    )
    out = maybe_capture_snapshots(
        snapshot_history=CounterSnapshotHistory(records=(old,)),
        sources=[("generation", _Reading(1.0))],
        now=now,
        local_tz=LOCAL_TZ,
        retention_days=2,
    )
    assert out.records == ()


def test_reading_without_attribute_skipped_gracefully() -> None:
    """A reading missing today_total_kwh is silently dropped (no crash)."""

    @dataclass(frozen=True)
    class _Bad:
        """No today_total_kwh attribute."""
        value: float

    now = datetime(2024, 1, 15, 23, 45, tzinfo=timezone.utc)
    out = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Bad(5.0))],
        now=now,
        local_tz=LOCAL_TZ,
    )
    assert out.records == ()


def test_positive_skew_shifts_capture_earlier_in_ha_time() -> None:
    """Inverter 5 min ahead of HA → snapshot fires when HA hits 23:25 (window opens early).

    Without skew correction, HA 23:25 is outside the [23:30, 23:59] window
    and no capture happens — but the inverter already reads 23:30, so we DO
    want the snapshot. ``clock_skew_seconds=+300`` should align the window
    check with the inverter's idea of "now".
    """
    ha_now = datetime(2024, 1, 15, 23, 25, tzinfo=timezone.utc)

    # Without skew: outside window, no capture.
    without = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(5.0))],
        now=ha_now,
        local_tz=LOCAL_TZ,
    )
    assert without.records == ()

    # With +300s skew (inverter ahead): window check uses 23:30, captures.
    with_skew = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(5.0))],
        now=ha_now,
        local_tz=LOCAL_TZ,
        clock_skew_seconds=300.0,
    )
    assert len(with_skew.records) == 1
    # The captured_at remains HA UTC, not the shifted moment.
    assert with_skew.records[0].captured_at == ha_now


def test_negative_skew_extends_window_later_in_ha_time() -> None:
    """Inverter 5 min behind HA → snapshot still fires when HA already at 00:02.

    HA 00:02 (next day) is past the window normally. With skew=-300s the
    window check uses 23:57 — still inside the window, capture happens.
    """
    ha_now = datetime(2024, 1, 16, 0, 2, tzinfo=timezone.utc)
    with_skew = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(5.0))],
        now=ha_now,
        local_tz=LOCAL_TZ,
        clock_skew_seconds=-300.0,
    )
    assert len(with_skew.records) == 1


def test_window_boundary_inclusive() -> None:
    """Snapshots at the exact start and end-of-minute of the window are captured."""
    # Start boundary
    t_start = datetime(2024, 1, 15, 23, 30, 0, tzinfo=timezone.utc)
    out_start = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(1.0))],
        now=t_start,
        local_tz=LOCAL_TZ,
    )
    assert len(out_start.records) == 1

    # End boundary (end is inclusive at end:59.999999)
    t_end = datetime(2024, 1, 15, 23, 59, 59, tzinfo=timezone.utc)
    out_end = maybe_capture_snapshots(
        snapshot_history=_empty(),
        sources=[("generation", _Reading(1.0))],
        now=t_end,
        local_tz=LOCAL_TZ,
    )
    assert len(out_end.records) == 1
