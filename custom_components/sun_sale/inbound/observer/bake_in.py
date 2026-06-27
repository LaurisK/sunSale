"""Once-per-day bake-in operation — finalises yesterday's per-slot kWh.

The bake-in runs in the inbound layer per cycle, but is **idempotent per
(date, side)**: once a ``BakedDayRecord`` exists for a given local date and
side, that record is frozen and never modified. New records appear only on
the day after the data they describe.

Per side, per cycle (only while no record exists for the target date):

1. Resolve the authoritative yesterday-total via
   ``yesterday_total_resolver.resolve_yesterday_total``.
2. If the resolver returns ``None`` *and* the current local time is past
   ``BAKE_IN_HARD_CUTOFF_LOCAL`` — record ``failed_no_source`` with the raw
   averaged slots and freeze.
3. If the resolver returns ``None`` and the cutoff has not yet been reached —
   skip this side this cycle; the next cycle retries.
4. Otherwise, build the raw yesterday slots from the engine and apply
   ``apply_proportional_bake_in`` — the resulting slots, the resolved counter
   total, and the source kind become the new ``BakedDayRecord``.

Empty-price guard: if the engine returns no slots for the target window —
i.e. the ``PriceSeries`` does not yet cover the just-ended day, as happens in
the minutes right after local midnight before pricing re-assembles — the side
is skipped (retry next cycle) rather than frozen with a 0-slot record. A
frozen empty record would permanently wipe that day from the observed series
and starve the monthly-bill carry; skipping lets the bake succeed later once
prices arrive, and a day that never gets prices simply falls back to raw
averaging.

Old records are pruned from the returned history according to
``BAKED_OBSERVED_HISTORY_RETENTION_DAYS``.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from datetime import tzinfo as TzInfo
from typing import Any, Sequence

from ...contract.const import (
    BAKE_IN_HARD_CUTOFF_LOCAL,
    BAKED_OBSERVED_HISTORY_RETENTION_DAYS,
    SOURCE_KIND_FAILED_NO_SOURCE,
)
from ...contract.models import (
    BakedDayRecord,
    BakedObservedHistory,
    CounterSnapshotHistory,
    SlotKwh,
)
from ..yesterday_total_resolver import resolve_yesterday_total
from .engine import ObservedSeriesEngine


def try_bake_yesterday(
    engine: ObservedSeriesEngine,
    samples_by_side: dict[str, Sequence[Any]],
    price_slots: Sequence[Any],
    baked_history: BakedObservedHistory,
    snapshot_history: CounterSnapshotHistory,
    hass: Any,
    raw_config: dict,
    now: datetime,
    local_tz: TzInfo,
) -> BakedObservedHistory:
    """Attempt to bake yesterday for each side; return updated baked history.

    Idempotent per (date, side): a side already represented in
    ``baked_history`` for ``target_date_local`` (yesterday in ``local_tz``)
    is skipped. Retention pruning runs every call regardless of whether a
    bake occurred.

    Args:
        engine: ``ObservedSeriesEngine`` instance whose ``sides`` drives the
            iteration. Generation = 1 side; grid = 2.
        samples_by_side: Per-side sample streams. Each key is a registered
            side's ``id``; the value is the rolling power-history samples
            for that direction. Sides with no entry in the dict use an empty
            stream (their slots become 0).
        price_slots: Price grid; passed through to the engine to define slot
            boundaries for yesterday.
        baked_history: Current persisted baked history.
        snapshot_history: Current persisted snapshot history (passed to the
            resolver as the snapshot fallback source).
        hass: Home Assistant instance, used by the resolver for dedicated
            sensor reads. May be ``None`` in tests.
        raw_config: Raw config-entry dict, used by the resolver to look up
            dedicated yesterday-total entity IDs.
        now: Cycle timestamp (UTC).
        local_tz: Local timezone defining the day boundary and the hard cutoff.

    Returns:
        Updated ``BakedObservedHistory``. Identical to the input (modulo
        retention) when nothing changed this cycle.
    """
    local_now = now.astimezone(local_tz)
    target_date = local_now.date() - timedelta(days=1)
    target_date_str = target_date.isoformat()

    cutoff_h, cutoff_m = BAKE_IN_HARD_CUTOFF_LOCAL
    past_cutoff = (local_now.hour, local_now.minute) >= (cutoff_h, cutoff_m)

    kept = _prune_retention(baked_history.records, local_now.date())
    existing_keys = {(r.date_str, r.side_id) for r in kept}

    target_start = datetime.combine(
        target_date, time(0, 0), tzinfo=local_tz,
    ).astimezone(timezone.utc)
    target_end = datetime.combine(
        target_date + timedelta(days=1), time(0, 0), tzinfo=local_tz,
    ).astimezone(timezone.utc)

    raw_per_side: dict[str, list[SlotKwh]] | None = None
    new_records: list[BakedDayRecord] = []

    for side in engine.sides:
        if (target_date_str, side.id) in existing_keys:
            continue

        resolved = resolve_yesterday_total(
            side_id=side.id,
            target_date_local=target_date,
            hass=hass,
            raw_config=raw_config,
            snapshot_history=snapshot_history,
            local_tz=local_tz,
        )

        if resolved is None and not past_cutoff:
            continue  # retry next cycle

        if raw_per_side is None:
            raw_per_side = engine.build_slots_for_window(
                samples_by_side=samples_by_side,
                price_slots=price_slots,
                window_start=target_start,
                window_end=target_end,
            )
        raw_slots = raw_per_side.get(side.id, [])

        # An empty slot list means the price grid does not yet cover the
        # target day — which happens right after local midnight, before the
        # PriceSeries has re-assembled the just-ended day. Freezing a 0-slot
        # record here would permanently wipe that day from the observed
        # series (and starve the monthly-bill carry). Skip instead and retry
        # next cycle; by mid-morning the grid covers yesterday and the bake
        # succeeds. A genuinely price-less day never freezes a record, so
        # observed series fall back to raw averaging for it.
        if not raw_slots:
            continue

        if resolved is None:
            new_records.append(BakedDayRecord(
                date_str=target_date_str,
                side_id=side.id,
                counter_total_used=0.0,
                source_kind=SOURCE_KIND_FAILED_NO_SOURCE,
                baked_slots=tuple(raw_slots),
                baked_sum=round(sum(s.kwh for s in raw_slots), 6),
                baked_at=now,
            ))
            continue

        counter_total, source_kind = resolved
        bake_result = engine.apply_proportional_bake_in(
            raw_slots_per_side={side.id: raw_slots},
            counter_totals_per_side={side.id: counter_total},
        )
        baked_slots, _status, _factor = bake_result[side.id]
        baked_sum = round(sum(s.kwh for s in baked_slots), 6)
        new_records.append(BakedDayRecord(
            date_str=target_date_str,
            side_id=side.id,
            counter_total_used=counter_total,
            source_kind=source_kind,
            baked_slots=tuple(baked_slots),
            baked_sum=baked_sum,
            baked_at=now,
        ))

    if not new_records and kept == baked_history.records:
        return baked_history
    return BakedObservedHistory(records=kept + tuple(new_records))


def _prune_retention(
    records: Sequence[BakedDayRecord],
    today_local: date,
) -> tuple[BakedDayRecord, ...]:
    """Drop records older than ``BAKED_OBSERVED_HISTORY_RETENTION_DAYS``.

    Malformed ``date_str`` values are kept (treated as "newer than cutoff") so
    a manual edit cannot accidentally wipe the store.

    Args:
        records: Existing baked records.
        today_local: Current local date used as the retention anchor.

    Returns:
        Tuple of records still inside the retention window.
    """
    cutoff_date = today_local - timedelta(days=BAKED_OBSERVED_HISTORY_RETENTION_DAYS)
    kept: list[BakedDayRecord] = []
    for r in records:
        try:
            d = date.fromisoformat(r.date_str)
        except ValueError:
            kept.append(r)
            continue
        if d >= cutoff_date:
            kept.append(r)
    return tuple(kept)


def baked_slots_by_date(
    baked_history: BakedObservedHistory,
    side_id: str,
) -> dict[str, BakedDayRecord]:
    """Index baked records of one side by ``date_str`` for fast lookup.

    Args:
        baked_history: Persisted baked history.
        side_id: Engine side identifier.

    Returns:
        Dict ``date_str → BakedDayRecord`` containing only records for this side.
    """
    return {r.date_str: r for r in baked_history.records if r.side_id == side_id}
