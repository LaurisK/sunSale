"""Tests for inbound/yesterday_total_resolver.py — pure Python."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from custom_components.sun_sale.contract.const import (
    SOURCE_KIND_DEDICATED_SENSOR,
    SOURCE_KIND_SNAPSHOT,
)
from custom_components.sun_sale.contract.models import (
    CounterSnapshotHistory,
    CounterSnapshotRecord,
)
from custom_components.sun_sale.inbound.yesterday_total_resolver import (
    DEDICATED_ENTITY_CONFIG_KEY,
    resolve_yesterday_total,
)


LOCAL_TZ = timezone.utc
TARGET = date(2024, 1, 14)


@dataclass(frozen=True)
class _State:
    state: str


class _Hass:
    """Minimal hass stub exposing only ``states.get``."""

    def __init__(self, states: dict[str, _State] | None = None) -> None:
        self.states = self  # type: ignore[assignment]
        self._states = states or {}

    def get(self, entity_id: str) -> _State | None:
        return self._states.get(entity_id)


def _snapshot(side_id: str, day: date, value: float, hour: int = 23, minute: int = 45) -> CounterSnapshotRecord:
    """Build a CounterSnapshotRecord captured at the given local date / time."""
    captured = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    return CounterSnapshotRecord(
        side_id=side_id, captured_at=captured, today_total_kwh=value,
    )


def _empty_snapshots() -> CounterSnapshotHistory:
    return CounterSnapshotHistory(records=())


# ---------------------------------------------------------------------------
# Dedicated sensor preferred
# ---------------------------------------------------------------------------


def test_dedicated_sensor_preferred_when_set() -> None:
    """A configured dedicated entity wins over any snapshot."""
    hass = _Hass({"sensor.solis_solar_yesterday": _State("13.5")})
    raw_config = {DEDICATED_ENTITY_CONFIG_KEY["generation"]: "sensor.solis_solar_yesterday"}
    snaps = CounterSnapshotHistory(records=(_snapshot("generation", TARGET, 99.9),))

    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=hass,
        raw_config=raw_config,
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result == (13.5, SOURCE_KIND_DEDICATED_SENSOR)


def test_dedicated_sensor_unparseable_falls_back_to_snapshot() -> None:
    """A non-numeric dedicated state triggers the snapshot fallback."""
    hass = _Hass({"sensor.gen_yesterday": _State("unavailable")})
    raw_config = {DEDICATED_ENTITY_CONFIG_KEY["generation"]: "sensor.gen_yesterday"}
    snaps = CounterSnapshotHistory(records=(_snapshot("generation", TARGET, 10.0),))

    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=hass,
        raw_config=raw_config,
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result == (10.0, SOURCE_KIND_SNAPSHOT)


def test_no_dedicated_no_snapshot_returns_none() -> None:
    """Without either source the resolver returns None."""
    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=_Hass(),
        raw_config={},
        snapshot_history=_empty_snapshots(),
        local_tz=LOCAL_TZ,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Snapshot path
# ---------------------------------------------------------------------------


def test_snapshot_returned_when_no_dedicated_configured() -> None:
    """Snapshot fallback returns the snapshot value with source_kind=snapshot."""
    snaps = CounterSnapshotHistory(records=(_snapshot("grid_import", TARGET, 4.2),))
    result = resolve_yesterday_total(
        side_id="grid_import",
        target_date_local=TARGET,
        hass=_Hass(),
        raw_config={},
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result == (4.2, SOURCE_KIND_SNAPSHOT)


def test_snapshot_latest_in_target_date_wins() -> None:
    """When multiple snapshots exist on the target date, the latest is used."""
    early = _snapshot("generation", TARGET, 10.0, hour=23, minute=30)
    late = _snapshot("generation", TARGET, 11.0, hour=23, minute=55)
    snaps = CounterSnapshotHistory(records=(early, late))
    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=_Hass(),
        raw_config={},
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result == (11.0, SOURCE_KIND_SNAPSHOT)


def test_snapshot_for_other_date_ignored() -> None:
    """A snapshot dated other than target_date_local is not used."""
    other = _snapshot("generation", date(2024, 1, 13), 99.0)
    snaps = CounterSnapshotHistory(records=(other,))
    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=_Hass(),
        raw_config={},
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result is None


def test_snapshot_for_other_side_ignored() -> None:
    """A snapshot for a different side_id is not used."""
    other = _snapshot("grid_export", TARGET, 5.0)
    snaps = CounterSnapshotHistory(records=(other,))
    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=_Hass(),
        raw_config={},
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result is None


def test_hass_none_skips_dedicated_path() -> None:
    """``hass=None`` (test-only) skips path 1 cleanly and goes to snapshot."""
    snaps = CounterSnapshotHistory(records=(_snapshot("generation", TARGET, 8.0),))
    result = resolve_yesterday_total(
        side_id="generation",
        target_date_local=TARGET,
        hass=None,
        raw_config={DEDICATED_ENTITY_CONFIG_KEY["generation"]: "sensor.never_read"},
        snapshot_history=snaps,
        local_tz=LOCAL_TZ,
    )
    assert result == (8.0, SOURCE_KIND_SNAPSHOT)
