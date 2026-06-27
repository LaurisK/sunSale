"""Declarative registry for the coordinator's rolling sample-history stores.

Every observed series the pipeline accumulates — generation, PV power, the two
grid-power directions, the two grid today-total counters, and the derived
cross-stream sample — persists as a rolling list of timestamped readings,
serialised identically (one ``{"ts": …, <value keys>}`` row per sample) and
injected back into the DAG each cycle as a ``*History`` primary. This module
captures that shape once in :class:`HistoryStoreSpec`, so adding a new series
costs one table entry instead of a hand-written serialize/deserialize pair plus
a bespoke append-and-trim block in ``_async_update_data``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..contract.const import (
    DERIVED_POWER_HISTORY_RETENTION_DAYS,
    GENERATION_HISTORY_RETENTION_DAYS,
    GRID_EXPORT_TOTAL_HISTORY_RETENTION_DAYS,
    GRID_IMPORT_TOTAL_HISTORY_RETENTION_DAYS,
    GRID_POWER_HISTORY_RETENTION_DAYS,
    PV_POWER_HISTORY_RETENTION_DAYS,
    STORAGE_KEY_DERIVED_POWER,
    STORAGE_KEY_GENERATION,
    STORAGE_KEY_GRID_EXPORT_POWER,
    STORAGE_KEY_GRID_EXPORT_TOTAL,
    STORAGE_KEY_GRID_IMPORT_POWER,
    STORAGE_KEY_GRID_IMPORT_TOTAL,
    STORAGE_KEY_PV_POWER,
)
from ..contract.models import (
    DerivedPowerHistory,
    DerivedPowerSample,
    GenerationHistory,
    GenerationReading,
    GridExportPowerHistory,
    GridExportPowerReading,
    GridExportTodayHistory,
    GridExportTodayReading,
    GridImportPowerHistory,
    GridImportPowerReading,
    GridImportTodayHistory,
    GridImportTodayReading,
    PvPowerHistory,
    PvPowerReading,
)
from .persistent_store import PersistentStore


@dataclass(frozen=True)
class HistoryStoreSpec:
    """Declarative spec for one rolling sample-history persistent store.

    A sample is any frozen dataclass with a ``timestamp`` field plus a fixed
    set of scalar value fields; the history is its tuple-wrapping container
    (``samples=(…)``). ``field_map`` lists ``(json_key, reading_attr)`` pairs
    for the value fields — the timestamp is handled implicitly under the
    ``ts`` key — and doubles as the constructor keyword mapping on load.

    Attributes:
        storage_key: HA storage key for the underlying ``PersistentStore``.
        reading_type: Per-sample dataclass (e.g. ``GenerationReading``).
        history_type: Tuple-wrapping container dataclass (e.g.
            ``GenerationHistory``), constructed as ``history_type(samples=…)``.
        retention_days: Samples older than this many days are trimmed on
            append.
        field_map: ``(json_key, reading_attr)`` pairs for the value fields.
    """

    storage_key: str
    reading_type: type
    history_type: type
    retention_days: int
    field_map: tuple[tuple[str, str], ...]

    def serialize(self, samples: list) -> dict:
        """Serialise a list of readings to the ``{"samples": [...]}`` layout."""
        return {
            "samples": [
                {
                    "ts": s.timestamp.isoformat(),
                    **{jk: getattr(s, attr) for jk, attr in self.field_map},
                }
                for s in samples
            ]
        }

    def deserialize(self, payload: dict) -> list:
        """Reconstruct the list of readings from a stored payload dict."""
        return [
            self.reading_type(
                timestamp=datetime.fromisoformat(s["ts"]),
                **{attr: s[jk] for jk, attr in self.field_map},
            )
            for s in payload.get("samples", [])
        ]

    def build_history(self, store: PersistentStore | None):
        """Wrap a store's current sample list in this spec's history type."""
        samples = (store.value or []) if store is not None else []
        return self.history_type(samples=tuple(samples))


# --- Spec registry --------------------------------------------------------
# Order is cosmetic — each spec injects an independent primary key — but kept
# matching the historical hand-written sequence for reviewability.

GENERATION_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_GENERATION,
    reading_type=GenerationReading,
    history_type=GenerationHistory,
    retention_days=GENERATION_HISTORY_RETENTION_DAYS,
    field_map=(("kwh", "today_total_kwh"),),
)

PV_POWER_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_PV_POWER,
    reading_type=PvPowerReading,
    history_type=PvPowerHistory,
    retention_days=PV_POWER_HISTORY_RETENTION_DAYS,
    field_map=(("w", "power_w"),),
)

GRID_IMPORT_POWER_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_GRID_IMPORT_POWER,
    reading_type=GridImportPowerReading,
    history_type=GridImportPowerHistory,
    retention_days=GRID_POWER_HISTORY_RETENTION_DAYS,
    field_map=(("kw", "power_kw"),),
)

GRID_EXPORT_POWER_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_GRID_EXPORT_POWER,
    reading_type=GridExportPowerReading,
    history_type=GridExportPowerHistory,
    retention_days=GRID_POWER_HISTORY_RETENTION_DAYS,
    field_map=(("kw", "power_kw"),),
)

GRID_IMPORT_TOTAL_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_GRID_IMPORT_TOTAL,
    reading_type=GridImportTodayReading,
    history_type=GridImportTodayHistory,
    retention_days=GRID_IMPORT_TOTAL_HISTORY_RETENTION_DAYS,
    field_map=(("kwh", "today_total_kwh"),),
)

GRID_EXPORT_TOTAL_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_GRID_EXPORT_TOTAL,
    reading_type=GridExportTodayReading,
    history_type=GridExportTodayHistory,
    retention_days=GRID_EXPORT_TOTAL_HISTORY_RETENTION_DAYS,
    field_map=(("kwh", "today_total_kwh"),),
)

DERIVED_POWER_SPEC = HistoryStoreSpec(
    storage_key=STORAGE_KEY_DERIVED_POWER,
    reading_type=DerivedPowerSample,
    history_type=DerivedPowerHistory,
    retention_days=DERIVED_POWER_HISTORY_RETENTION_DAYS,
    field_map=(
        ("ac", "ac_port_kw_signed"),
        ("bu", "backup_kw"),
        ("gn", "grid_net_kw_signed"),
        ("sol", "solar_kw"),
        ("bat", "battery_kw_signed"),
    ),
)

# Specs whose per-cycle reading is a plain ``primary.get(reading_type)`` — the
# coordinator drives these in one loop. The derived sample is composed from
# several primaries first, so it is appended explicitly by the coordinator and
# is intentionally excluded here.
SAMPLE_HISTORY_SPECS: tuple[HistoryStoreSpec, ...] = (
    GENERATION_SPEC,
    PV_POWER_SPEC,
    GRID_IMPORT_POWER_SPEC,
    GRID_EXPORT_POWER_SPEC,
    GRID_IMPORT_TOTAL_SPEC,
    GRID_EXPORT_TOTAL_SPEC,
)

# Every spec that owns a store — used to build the store registry at setup.
ALL_HISTORY_SPECS: tuple[HistoryStoreSpec, ...] = (
    *SAMPLE_HISTORY_SPECS,
    DERIVED_POWER_SPEC,
)


async def append_and_inject(
    spec: HistoryStoreSpec,
    store: PersistentStore | None,
    primary: dict,
    current: Any,
    now: datetime,
) -> None:
    """Append ``current`` to ``store`` and inject the rolling window as a primary.

    When ``current`` is present it is appended to ``store`` and entries older
    than the spec's retention window are trimmed; either way the (possibly
    just-updated) sample list is wrapped in the spec's history type and stored
    under that type in ``primary`` for the DAG to consume.

    Args:
        spec: The history spec describing the store's shape and retention.
        store: The backing store, or ``None`` before setup completes.
        primary: The cycle's primary-input dict to inject the history into.
        current: This cycle's reading, or ``None`` when unavailable.
        now: Current cycle timestamp; the retention cutoff is measured from it.
    """
    if current is not None and store is not None:
        cutoff = now - timedelta(days=spec.retention_days)
        await store.append_and_trim(current, cutoff, lambda s: s.timestamp)
    primary[spec.history_type] = spec.build_history(store)
