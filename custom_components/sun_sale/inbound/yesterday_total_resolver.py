"""Yesterday-total resolver — picks a counter total source for the bake-in.

The bake-in (Phase 3) needs an authoritative ``yesterday_total_kwh`` per side
to compute its proportional correction. Two sources are supported, in order
of preference:

1. **Dedicated yesterday-total HA entity** — a sensor exposing the
   already-finalised yesterday total (some inverter integrations expose this
   directly, e.g. ``sensor.solis_solar_yesterday``). Robust to clock skew and
   reset timing.
2. **Pre-rollover snapshot** — the latest record captured by
   ``inbound/pre_rollover_snapshot.py`` during target-date 23:30 → 23:59
   local. Used when no dedicated sensor is configured for that side.

When neither source produces a value, the resolver returns ``None`` and the
bake-in marks the day ``failed_no_source`` for that side.
"""
from __future__ import annotations

from datetime import date
from datetime import tzinfo as TzInfo
from typing import Any

from ..contract.const import (
    SOURCE_KIND_DEDICATED_SENSOR,
    SOURCE_KIND_SNAPSHOT,
)
from ..contract.models import CounterSnapshotHistory
from ..ha_state import read_float_state

# Per-side config key for the dedicated yesterday-total entity mapping. The
# coordinator populates ``raw_config`` from the config entry; missing keys
# (the common case today) simply fall back to the snapshot path.
DEDICATED_ENTITY_CONFIG_KEY: dict[str, str] = {
    "generation":  "inverter_entity_generation_yesterday",
    "grid_import": "inverter_entity_grid_import_yesterday",
    "grid_export": "inverter_entity_grid_export_yesterday",
}


def resolve_yesterday_total(
    side_id: str,
    target_date_local: date,
    hass: Any,
    raw_config: dict,
    snapshot_history: CounterSnapshotHistory,
    local_tz: TzInfo,
) -> tuple[float, str] | None:
    """Return ``(counter_total_kwh, source_kind)`` for the side, or ``None``.

    Args:
        side_id: Engine side identifier (e.g. ``"generation"``,
            ``"grid_import"``, ``"grid_export"``).
        target_date_local: Local date the bake-in is finalising (i.e.
            yesterday relative to the current local date).
        hass: Home Assistant instance, used for reading the dedicated entity.
            May be ``None`` in tests — the resolver then skips path 1.
        raw_config: Raw config-entry dict. Looked up via
            ``DEDICATED_ENTITY_CONFIG_KEY[side_id]``.
        snapshot_history: Rolling snapshot history persisted by the snapshot
            module.
        local_tz: Local timezone, used to bound the snapshot search window.

    Returns:
        ``(value_kwh, SOURCE_KIND_DEDICATED_SENSOR)`` when a dedicated sensor
        is set and yields a parseable numeric state; otherwise
        ``(value_kwh, SOURCE_KIND_SNAPSHOT)`` when the snapshot history holds
        a record captured on ``target_date_local`` for the side; otherwise
        ``None``.
    """
    dedicated = _read_dedicated_sensor(hass, raw_config, side_id)
    if dedicated is not None:
        return (dedicated, SOURCE_KIND_DEDICATED_SENSOR)

    snapshot_value = _latest_snapshot_for(
        snapshot_history, side_id, target_date_local, local_tz,
    )
    if snapshot_value is not None:
        return (snapshot_value, SOURCE_KIND_SNAPSHOT)

    return None


def _read_dedicated_sensor(
    hass: Any, raw_config: dict, side_id: str,
) -> float | None:
    """Read the dedicated yesterday-total entity for the side, if mapped.

    Args:
        hass: Home Assistant instance. ``None`` skips the read entirely.
        raw_config: Raw config-entry dict.
        side_id: Engine side identifier.

    Returns:
        Parsed numeric kWh value, or ``None`` when no entity is mapped, no
        ``hass`` is provided, or the state is missing/non-numeric.
    """
    if hass is None:
        return None
    key = DEDICATED_ENTITY_CONFIG_KEY.get(side_id)
    if not key:
        return None
    entity_id = raw_config.get(key)
    if not entity_id:
        return None
    return read_float_state(hass, entity_id)


def _latest_snapshot_for(
    snapshot_history: CounterSnapshotHistory,
    side_id: str,
    target_date_local: date,
    local_tz: TzInfo,
) -> float | None:
    """Return the latest snapshot value for the side on the target local date.

    Snapshot ``captured_at`` is UTC; this helper converts to local time to test
    membership in ``target_date_local``. The latest snapshot (max
    ``captured_at``) wins so we always use the closest-to-midnight reading.

    Args:
        snapshot_history: Rolling snapshot history.
        side_id: Engine side identifier.
        target_date_local: Local date the snapshot must fall in.
        local_tz: Local timezone for date comparison.

    Returns:
        ``today_total_kwh`` of the matching record, or ``None`` when none
        exist.
    """
    matches = [
        r for r in snapshot_history.records
        if r.side_id == side_id
        and r.captured_at.astimezone(local_tz).date() == target_date_local
    ]
    if not matches:
        return None
    latest = max(matches, key=lambda r: r.captured_at)
    return latest.today_total_kwh
