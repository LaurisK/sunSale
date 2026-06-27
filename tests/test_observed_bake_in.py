"""Tests for inbound/observer/bake_in.py — pure Python."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.const import (
    SOURCE_KIND_DEDICATED_SENSOR,
    SOURCE_KIND_FAILED_NO_SOURCE,
    SOURCE_KIND_SNAPSHOT,
)
from custom_components.sun_sale.contract.models import (
    BakedDayRecord,
    BakedObservedHistory,
    CounterSnapshotHistory,
    CounterSnapshotRecord,
    SlotKwh,
)
from custom_components.sun_sale.inbound.observer.bake_in import (
    baked_slots_by_date,
    try_bake_yesterday,
)
from custom_components.sun_sale.inbound.observer.engine import (
    ObservedSeriesEngine,
    Side,
)
from custom_components.sun_sale.inbound.yesterday_total_resolver import (
    DEDICATED_ENTITY_CONFIG_KEY,
)


LOCAL_TZ = timezone.utc


@dataclass(frozen=True)
class _PowerSample:
    """Synthetic power sample exposing kW magnitude + timestamp."""
    timestamp: datetime
    kw: float


@dataclass(frozen=True)
class _Slot:
    """Minimal price-grid slot exposing UTC start / end."""
    start: datetime
    end: datetime


@dataclass(frozen=True)
class _State:
    state: str


class _Hass:
    """Minimal hass stub exposing ``states.get``."""

    def __init__(self, states: dict[str, _State] | None = None) -> None:
        self.states = self  # type: ignore[assignment]
        self._states = states or {}

    def get(self, entity_id: str) -> _State | None:
        return self._states.get(entity_id)


def _gen_engine() -> ObservedSeriesEngine:
    """Single-side generation engine with kW extractor."""
    return ObservedSeriesEngine(
        [Side(id="generation", extract=lambda s: max(0.0, s.kw))],
        local_tz=LOCAL_TZ,
    )


def _grid_engine() -> ObservedSeriesEngine:
    """Two-side grid engine with signed-kW split."""
    return ObservedSeriesEngine(
        [
            Side(id="grid_import", extract=lambda s: max(0.0, s.kw)),
            Side(id="grid_export", extract=lambda s: max(0.0, -s.kw)),
        ],
        local_tz=LOCAL_TZ,
    )


def _hourly_slots(start: datetime, hours: int) -> list[_Slot]:
    """``hours`` consecutive 1-hour slots starting at ``start``."""
    return [
        _Slot(start + timedelta(hours=h), start + timedelta(hours=h + 1))
        for h in range(hours)
    ]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_does_not_re_bake_existing_record() -> None:
    """A record already present for (yesterday, generation) is preserved."""
    now = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
    yesterday = "2024-01-14"
    existing = BakedDayRecord(
        date_str=yesterday,
        side_id="generation",
        counter_total_used=5.0,
        source_kind=SOURCE_KIND_DEDICATED_SENSOR,
        baked_slots=(SlotKwh(datetime(2024, 1, 14, 10, 0, tzinfo=timezone.utc),
                              datetime(2024, 1, 14, 11, 0, tzinfo=timezone.utc),
                              5.0),),
        baked_sum=5.0,
        baked_at=datetime(2024, 1, 15, 0, 5, tzinfo=timezone.utc),
    )
    hass = _Hass({"sensor.fresh": _State("99.0")})
    raw_config = {DEDICATED_ENTITY_CONFIG_KEY["generation"]: "sensor.fresh"}

    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": [
            _PowerSample(datetime(2024, 1, 14, 10, 30, tzinfo=timezone.utc), kw=1.0),
        ]},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=(existing,)),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=hass,
        raw_config=raw_config,
        now=now,
        local_tz=LOCAL_TZ,
    )
    matching = [r for r in out.records if r.side_id == "generation" and r.date_str == yesterday]
    assert len(matching) == 1
    # Unchanged — counter_total_used still 5.0, not 99.0.
    assert matching[0].counter_total_used == 5.0


# ---------------------------------------------------------------------------
# Successful bake-in
# ---------------------------------------------------------------------------


def test_bake_with_dedicated_sensor() -> None:
    """Dedicated sensor reading produces a record with source_kind=dedicated_sensor."""
    now = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
    hass = _Hass({"sensor.gen_yest": _State("4.0")})
    raw_config = {DEDICATED_ENTITY_CONFIG_KEY["generation"]: "sensor.gen_yest"}

    # Yesterday samples: 2 kW for one hour → 2.0 kWh raw → factor = 2.0
    yest_start = datetime(2024, 1, 14, 10, tzinfo=timezone.utc)
    samples = [_PowerSample(yest_start + timedelta(minutes=30), kw=2.0)]

    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": samples},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=hass,
        raw_config=raw_config,
        now=now,
        local_tz=LOCAL_TZ,
    )
    rec = next(r for r in out.records if r.side_id == "generation")
    assert rec.source_kind == SOURCE_KIND_DEDICATED_SENSOR
    assert rec.counter_total_used == 4.0
    # Bake scaled 2.0 → 4.0
    assert abs(rec.baked_sum - 4.0) < 1e-6


def test_bake_with_snapshot_fallback() -> None:
    """When no dedicated sensor, the latest snapshot in target_date drives the bake."""
    now = datetime(2024, 1, 15, 1, 0, tzinfo=timezone.utc)
    yest = datetime(2024, 1, 14, 23, 45, tzinfo=timezone.utc)
    snaps = CounterSnapshotHistory(records=(
        CounterSnapshotRecord(side_id="generation", captured_at=yest, today_total_kwh=6.0),
    ))
    samples = [
        _PowerSample(datetime(2024, 1, 14, 10, 30, tzinfo=timezone.utc), kw=2.0),
        _PowerSample(datetime(2024, 1, 14, 11, 30, tzinfo=timezone.utc), kw=2.0),
    ]    # raw sum 4.0; counter 6.0; factor 1.5

    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": samples},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=snaps,
        hass=_Hass(),
        raw_config={},
        now=now,
        local_tz=LOCAL_TZ,
    )
    rec = next(r for r in out.records if r.side_id == "generation")
    assert rec.source_kind == SOURCE_KIND_SNAPSHOT
    assert rec.counter_total_used == 6.0
    assert abs(rec.baked_sum - 6.0) < 1e-6


def test_skips_when_price_grid_misses_target_day() -> None:
    """Source resolved but price grid misses yesterday → skip (retry), no 0-slot freeze.

    Reproduces the post-midnight race: the dedicated yesterday-counter resolves
    instantly at rollover, but the PriceSeries has not re-assembled the just-
    ended day yet, so the engine yields no slots for the target window.
    Freezing here would write a permanent 0-slot record that wipes the day from
    the observed series; the side must be skipped so a later cycle bakes it.
    """
    hass = _Hass({"sensor.gen_yest": _State("4.0")})
    raw_config = {DEDICATED_ENTITY_CONFIG_KEY["generation"]: "sensor.gen_yest"}
    samples = [_PowerSample(datetime(2024, 1, 14, 10, 30, tzinfo=timezone.utc), kw=2.0)]

    # 00:02, just after midnight: price grid covers only today + tomorrow,
    # NOT the 2024-01-14 target day.
    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": samples},
        price_slots=_hourly_slots(datetime(2024, 1, 15, tzinfo=timezone.utc), 48),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=hass,
        raw_config=raw_config,
        now=datetime(2024, 1, 15, 0, 2, tzinfo=timezone.utc),
        local_tz=LOCAL_TZ,
    )
    assert out.records == ()    # skipped, not frozen

    # Later the same day, once the grid covers yesterday, the bake succeeds.
    out2 = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": samples},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=hass,
        raw_config=raw_config,
        now=datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc),
        local_tz=LOCAL_TZ,
    )
    rec = next(r for r in out2.records if r.side_id == "generation")
    assert rec.source_kind == SOURCE_KIND_DEDICATED_SENSOR
    assert len(rec.baked_slots) == 24
    assert abs(rec.baked_sum - 4.0) < 1e-6


# ---------------------------------------------------------------------------
# Retry-until-source + hard cutoff
# ---------------------------------------------------------------------------


def test_skips_when_no_source_before_cutoff() -> None:
    """Before hard cutoff, no source → no record written (retry next cycle)."""
    now = datetime(2024, 1, 15, 1, 0, tzinfo=timezone.utc)  # 01:00 < 06:00 cutoff
    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": [
            _PowerSample(datetime(2024, 1, 14, 10, tzinfo=timezone.utc), kw=2.0),
        ]},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=_Hass(),
        raw_config={},
        now=now,
        local_tz=LOCAL_TZ,
    )
    assert out.records == ()


def test_records_failed_no_source_past_cutoff() -> None:
    """Past hard cutoff with no source → record failed_no_source, freeze."""
    now = datetime(2024, 1, 15, 6, 5, tzinfo=timezone.utc)  # 06:05 > 06:00 cutoff
    samples = [_PowerSample(datetime(2024, 1, 14, 10, 30, tzinfo=timezone.utc), kw=2.0)]

    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": samples},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=_Hass(),
        raw_config={},
        now=now,
        local_tz=LOCAL_TZ,
    )
    rec = next(r for r in out.records if r.side_id == "generation")
    assert rec.source_kind == SOURCE_KIND_FAILED_NO_SOURCE
    assert rec.counter_total_used == 0.0
    # Slots are the raw averaged ones (unmodified).
    assert abs(rec.baked_sum - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# Multi-side grid
# ---------------------------------------------------------------------------


def test_grid_independent_per_side_outcomes() -> None:
    """One side bakes from snapshot; the other has no source → independent records."""
    now = datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc)  # past cutoff
    yest = datetime(2024, 1, 14, 23, 45, tzinfo=timezone.utc)
    snaps = CounterSnapshotHistory(records=(
        # Only import has a snapshot.
        CounterSnapshotRecord(side_id="grid_import", captured_at=yest, today_total_kwh=3.0),
    ))
    # 1 h pure import @ 2 kW; no export samples.
    import_samples = [
        _PowerSample(datetime(2024, 1, 14, 10, m, tzinfo=timezone.utc), kw=2.0)
        for m in (10, 20, 30, 40, 50)
    ]

    out = try_bake_yesterday(
        engine=_grid_engine(),
        samples_by_side={"grid_import": import_samples, "grid_export": []},
        price_slots=_hourly_slots(datetime(2024, 1, 14, tzinfo=timezone.utc), 24),
        baked_history=BakedObservedHistory(records=()),
        snapshot_history=snaps,
        hass=_Hass(),
        raw_config={},
        now=now,
        local_tz=LOCAL_TZ,
    )
    imp = next(r for r in out.records if r.side_id == "grid_import")
    exp = next(r for r in out.records if r.side_id == "grid_export")
    assert imp.source_kind == SOURCE_KIND_SNAPSHOT
    assert imp.counter_total_used == 3.0
    assert exp.source_kind == SOURCE_KIND_FAILED_NO_SOURCE


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_drops_old_records() -> None:
    """Records older than the retention window are pruned from the output."""
    now = datetime(2024, 1, 15, 0, 5, tzinfo=timezone.utc)
    # 40 days ago is past the 35-day default retention.
    old = BakedDayRecord(
        date_str="2023-12-05",
        side_id="generation",
        counter_total_used=1.0,
        source_kind=SOURCE_KIND_DEDICATED_SENSOR,
        baked_slots=(),
        baked_sum=0.0,
        baked_at=now - timedelta(days=40),
    )
    out = try_bake_yesterday(
        engine=_gen_engine(),
        samples_by_side={"generation": []},
        price_slots=[],
        baked_history=BakedObservedHistory(records=(old,)),
        snapshot_history=CounterSnapshotHistory(records=()),
        hass=_Hass(),
        raw_config={},
        now=now,
        local_tz=LOCAL_TZ,
    )
    assert all(r.date_str != "2023-12-05" for r in out.records)


# ---------------------------------------------------------------------------
# baked_slots_by_date helper
# ---------------------------------------------------------------------------


def test_baked_slots_by_date_indexes_by_date_str() -> None:
    """The helper returns a date_str-keyed dict filtered by side_id."""
    rec_a = BakedDayRecord(
        date_str="2024-01-13", side_id="generation",
        counter_total_used=1.0, source_kind=SOURCE_KIND_DEDICATED_SENSOR,
        baked_slots=(), baked_sum=1.0,
        baked_at=datetime(2024, 1, 14, tzinfo=timezone.utc),
    )
    rec_b = BakedDayRecord(
        date_str="2024-01-14", side_id="generation",
        counter_total_used=2.0, source_kind=SOURCE_KIND_DEDICATED_SENSOR,
        baked_slots=(), baked_sum=2.0,
        baked_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
    )
    other_side = BakedDayRecord(
        date_str="2024-01-14", side_id="grid_import",
        counter_total_used=9.0, source_kind=SOURCE_KIND_DEDICATED_SENSOR,
        baked_slots=(), baked_sum=9.0,
        baked_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
    )
    history = BakedObservedHistory(records=(rec_a, rec_b, other_side))
    idx = baked_slots_by_date(history, "generation")
    assert set(idx.keys()) == {"2024-01-13", "2024-01-14"}
    assert idx["2024-01-14"].counter_total_used == 2.0
