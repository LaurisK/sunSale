"""Pre-rollover counter snapshot — captures today-total values before local midnight.

The inverter's daily-resetting counters (generation, grid import, grid export)
reset to 0 at local midnight. After the reset there is no way to recover
yesterday's total from those counters alone, so the bake-in needs an
authoritative source. This module captures snapshots of each counter's
``today_total_kwh`` value within a configurable late-evening window. The
next-day bake-in then reads the most recent snapshot per side as a fallback
when no dedicated yesterday-total sensor is mapped.

The module is parameterised by a list of ``(side_id, latest_reading)`` pairs
supplied by the coordinator each cycle. ``latest_reading`` may be ``None``
when the underlying entity is unavailable; only sides with a non-None reading
are snapshotted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TzInfo
from typing import Any, Sequence

from ..contract.models import CounterSnapshotHistory, CounterSnapshotRecord


def maybe_capture_snapshots(
    snapshot_history: CounterSnapshotHistory,
    sources: Sequence[tuple[str, Any | None]],
    now: datetime,
    local_tz: TzInfo,
    window_start: tuple[int, int] = (23, 30),
    window_end: tuple[int, int] = (23, 59),
    retention_days: int = 2,
    clock_skew_seconds: float | None = None,
) -> CounterSnapshotHistory:
    """Append a snapshot per side when ``now`` is within the rollover window.

    A snapshot is captured for each ``(side_id, reading)`` pair where the
    reading is not ``None``. Records older than ``retention_days`` are pruned
    from the returned history regardless of whether a capture occurred this
    cycle — so the store rotates even on cycles outside the window.

    When ``clock_skew_seconds`` is supplied, the window check is performed
    against ``now + skew`` (i.e. what the inverter believes "now" to be).
    Captured records still timestamp ``captured_at`` as HA UTC — only the
    window decision is shifted, so the snapshot is taken at the *HA* moment
    when the *inverter* is in the window. ``None`` skips the shift and falls
    back to HA-local timing.

    Args:
        snapshot_history: Current rolling snapshot history.
        sources: List of ``(side_id, latest_reading)`` pairs. ``latest_reading``
            must expose ``today_total_kwh`` when not ``None``; ``None`` skips
            that side this cycle.
        now: Cycle timestamp (UTC).
        local_tz: Local timezone defining the rollover window.
        window_start: ``(hour, minute)`` inclusive local start of the window.
        window_end: ``(hour, minute)`` inclusive local end of the window.
        retention_days: Records older than this many days from ``now`` are
            dropped from the output.
        clock_skew_seconds: Inverter clock skew (positive = inverter ahead).
            When supplied, shifts ``now`` by this amount for the window
            decision so the capture aligns with the inverter's rollover
            boundary. ``None`` disables the shift.

    Returns:
        A new ``CounterSnapshotHistory``. Returns a history identical to the
        input (modulo retention pruning) when outside the window or when no
        non-None readings were supplied.
    """
    cutoff = now - timedelta(days=retention_days)
    kept = tuple(r for r in snapshot_history.records if r.captured_at >= cutoff)

    window_now = (
        now + timedelta(seconds=clock_skew_seconds)
        if clock_skew_seconds is not None else now
    )
    if not _within_window(window_now, local_tz, window_start, window_end):
        if kept == snapshot_history.records:
            return snapshot_history
        return CounterSnapshotHistory(records=kept)

    new_records: list[CounterSnapshotRecord] = []
    for side_id, reading in sources:
        if reading is None:
            continue
        try:
            value = float(reading.today_total_kwh)
        except (AttributeError, TypeError, ValueError):
            continue
        new_records.append(
            CounterSnapshotRecord(
                side_id=side_id,
                captured_at=now,
                today_total_kwh=value,
            )
        )

    if not new_records and kept == snapshot_history.records:
        return snapshot_history

    return CounterSnapshotHistory(records=kept + tuple(new_records))


def _within_window(
    now: datetime,
    local_tz: TzInfo,
    window_start: tuple[int, int],
    window_end: tuple[int, int],
) -> bool:
    """Return True when ``now`` lies inside the local-time window inclusive.

    The window is defined by ``(hour, minute)`` bounds on the local day. This
    helper does not support windows that wrap midnight — callers requiring
    that must split the window into two contiguous halves.

    Args:
        now: Cycle timestamp (UTC).
        local_tz: Local timezone.
        window_start: Inclusive ``(hour, minute)`` start bound.
        window_end: Inclusive ``(hour, minute)`` end bound (extended to
            ``end:59.999999`` so a single-minute granular end still admits the
            full final minute).

    Returns:
        True when ``window_start <= local_now <= window_end:59.999999``.
    """
    local_now = now.astimezone(local_tz)
    start_t = local_now.replace(
        hour=window_start[0], minute=window_start[1], second=0, microsecond=0,
    )
    end_t = local_now.replace(
        hour=window_end[0], minute=window_end[1], second=59, microsecond=999999,
    )
    return start_t <= local_now <= end_t
