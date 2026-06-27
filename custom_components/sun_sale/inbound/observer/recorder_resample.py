"""Recorder-backed full-resolution resampling of the observer power streams.

The per-cycle translators capture a single instantaneous reading per ~5-min
coordinator tick, so the observer engine averages only ~3 samples per 15-min
slot. This module rebuilds the four observer sample histories
(``PvPowerHistory``, ``GridImport/ExportPowerHistory``, ``DerivedPowerHistory``)
at the inverter's native update rate by replaying the Home Assistant recorder's
state changes for the underlying sensors over a recent window, re-deriving the
**same readings the live translators / controller produce**. The unit
normalisation and the Solis battery/grid sign-flips run through the shared
:class:`~..telemetry.codec.TelemetryCodec` (the single source of truth for
vendor sign/unit transforms); only the role/fallback chain selection stays here,
mirroring ``InverterController.get_*_power``.

The coordinator merges the resampled samples into each cycle's rolling-history
primary before the DAG runs, so per-slot averages reflect every data point
solis_modbus published — not just the per-tick samples. The lightweight
persistent stores are left untouched (they still hold the coarse per-tick
samples for restart fallback, the live-flow tip, and the baseload bootstrap);
the recorder is the source of resolution. When the recorder is unavailable the
builders return nothing and the coordinator falls back to the coarse primary.

Sign / unit conventions come from the shared platform codec rather than being
re-implemented here, so the recorder path and the (future) live reader cannot
drift; ``tests/test_telemetry_codec.py`` pins the codec transforms, and
``tests/test_recorder_resample.py`` covers the resampler's composition (timeline
union, forward-fill, all-or-nothing gate, merge, and the incremental window).

One deliberate difference from the live path: the freshness guard (live
readers drop a state whose ``last_updated`` is older than ~180 s, to reject a
silently-frozen Modbus chain) is **not** applied here. The recorder only
stores rows on state *change*, so a frozen chain and a value that is genuinely
flat but still polling are indistinguishable after the fact — applying a
staleness cap would wrongly drop long flat-but-valid stretches (e.g. overnight
zero grid flow). Forward-fill therefore holds the last recorded value across
gaps; given solis_modbus's sub-minute update rate this is accurate in the
common case, and the coarse per-tick samples (which *do* carry the live
freshness verdict) are merged in alongside.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from ...contract.models import (
    DerivedPowerSample,
    GridExportPowerReading,
    GridImportPowerReading,
    PvPowerReading,
)
from ..telemetry import (
    SignalBinding,
    SignalRole,
    TelemetryCodec,
    TelemetrySignal,
)

_LOGGER = logging.getLogger(__name__)


# One recorder state reduced to the fields the transforms need: the change
# time, the numeric value, and the unit string (for power normalisation).
_Series = list[tuple[datetime, float, str]]


@dataclass(frozen=True)
class ResampleSources:
    """Entity IDs + platform needed to rebuild every observer stream from recorder.

    Built once at coordinator setup from the resolved inverter entities. The
    role map mirrors ``InverterController``'s ``entity_ids`` so the battery /
    grid-net reconstruction follows the same preference + fallback chain.

    Attributes:
        pv_power: Instantaneous PV power sensor (W or kW).
        ac_port_power: Signed AC grid-port power sensor (positive=inverter→grid).
        backup_power: Backup-port output power sensor.
        grid_import_power: Directional grid-import sensor, or "" when only a
            signed net sensor is available.
        grid_export_power: Directional grid-export sensor, or "".
        signed_grid_power: Signed net grid sensor (positive=import) used to
            split import/export when the directional sensors are absent.
        entity_ids: The controller's full role→entity map; the battery and
            grid-net reconstruction read ``battery_power_signed`` /
            ``battery_power`` and ``grid_power`` / ``grid_power_fallback`` /
            ``battery_soc`` from it.
        codec: The platform telemetry codec (owned by ``InverterController``)
            that applies the vendor sign/unit transforms — the same instance
            the live readers use, so the resampled and live paths cannot drift.
    """

    pv_power: str
    ac_port_power: str
    backup_power: str
    grid_import_power: str
    grid_export_power: str
    signed_grid_power: str
    entity_ids: dict[str, str]
    codec: TelemetryCodec


# --- Recorder fetch ---------------------------------------------------------

async def fetch_series(
    hass: Any, entity_id: str, start: datetime, end: datetime,
) -> _Series:
    """Return recorder state changes for ``entity_id`` as ``(ts, value, unit)``.

    Numeric states only; unavailable / unknown / non-float states are dropped.
    The recorder's ``include_start_time_state`` seeds a value at ``start`` so a
    signal that last changed before the window still has a forward-fill anchor.

    Args:
        hass: Home Assistant instance.
        entity_id: Source sensor entity ID; empty disables the fetch.
        start: Inclusive UTC window start.
        end: Inclusive UTC window end.

    Returns:
        Time-sorted list of ``(last_updated, float_value, unit_str)``. Empty
        when the entity is unset, the recorder is unavailable, or no numeric
        states fall in the window.
    """
    if not entity_id:
        return []
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import (
            state_changes_during_period,
        )
    except ImportError:
        return []

    try:
        instance = get_instance(hass)
        states_dict = await instance.async_add_executor_job(
            state_changes_during_period, hass, start, end, entity_id,
        )
    except Exception:    # pragma: no cover — recorder may be unavailable
        _LOGGER.warning(
            "Recorder resample fetch failed for %s", entity_id, exc_info=True,
        )
        return []

    states = states_dict.get(entity_id, []) if states_dict else []
    out: _Series = []
    for s in states:
        raw = getattr(s, "state", None)
        if raw in (None, "unavailable", "unknown", ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        ts = getattr(s, "last_updated", None) or getattr(s, "last_changed", None)
        if ts is None:
            continue
        unit = str((getattr(s, "attributes", {}) or {}).get("unit_of_measurement") or "").strip()
        out.append((ts, value, unit))
    out.sort(key=lambda r: r[0])
    return out


# --- Signal bindings (entity_id is irrelevant to the transform) -------------
#
# The codec keys off (signal, role) only, so these module-level bindings carry a
# placeholder entity_id. Role encodes the fallback chain (signed-net vs
# magnitude, directional vs net-split); the platform sign-flips live in the
# codec instance, not here.

_PV = SignalBinding(TelemetrySignal.PV_POWER, "")
_AC_PORT = SignalBinding(TelemetrySignal.AC_PORT_POWER, "")
_BACKUP = SignalBinding(TelemetrySignal.BACKUP_POWER, "")
_GRID_IMPORT_DIRECTIONAL = SignalBinding(TelemetrySignal.GRID_IMPORT, "")
_GRID_EXPORT_DIRECTIONAL = SignalBinding(TelemetrySignal.GRID_EXPORT, "")
_GRID_IMPORT_NET = SignalBinding(TelemetrySignal.GRID_IMPORT, "", SignalRole.SIGNED_NET)
_GRID_EXPORT_NET = SignalBinding(TelemetrySignal.GRID_EXPORT, "", SignalRole.SIGNED_NET)


# --- Stream builders --------------------------------------------------------

def build_pv_readings(series: _Series, codec: TelemetryCodec) -> list[PvPowerReading]:
    """Return one PvPowerReading per recorder state change (watts, via the codec)."""
    return [
        PvPowerReading(power_w=codec.to_canonical(_PV, v, u), timestamp=ts)
        for (ts, v, u) in series
    ]


def build_grid_readings(
    import_series: _Series,
    export_series: _Series,
    signed_series: _Series,
    *,
    has_directional: bool,
    codec: TelemetryCodec,
) -> tuple[list[GridImportPowerReading], list[GridExportPowerReading]]:
    """Return per-direction grid power readings, mirroring the directional observers.

    When directional sensors are mapped each is normalised and clamped ≥ 0
    independently; otherwise both directions are split per-sample from the
    signed net series (``import = max(0, net)``, ``export = max(0, −net)``),
    matching ``_DirectionalPowerObserver``. Both the normalisation and the split
    are the codec's ``GRID_IMPORT`` / ``GRID_EXPORT`` transforms (``PRIMARY`` for
    a directional sensor, ``SIGNED_NET`` for the net split).

    Args:
        import_series: Directional import recorder series (used when
            ``has_directional``).
        export_series: Directional export recorder series.
        signed_series: Signed net recorder series (used otherwise).
        has_directional: True when directional sensors are mapped.
        codec: The platform telemetry codec.

    Returns:
        ``(import_readings, export_readings)``.
    """
    if has_directional:
        imports = [
            GridImportPowerReading(
                power_kw=codec.to_canonical(_GRID_IMPORT_DIRECTIONAL, v, u), timestamp=ts,
            )
            for (ts, v, u) in import_series
        ]
        exports = [
            GridExportPowerReading(
                power_kw=codec.to_canonical(_GRID_EXPORT_DIRECTIONAL, v, u), timestamp=ts,
            )
            for (ts, v, u) in export_series
        ]
        return imports, exports

    imports = [
        GridImportPowerReading(power_kw=codec.to_canonical(_GRID_IMPORT_NET, v, u), timestamp=ts)
        for (ts, v, u) in signed_series
    ]
    exports = [
        GridExportPowerReading(power_kw=codec.to_canonical(_GRID_EXPORT_NET, v, u), timestamp=ts)
        for (ts, v, u) in signed_series
    ]
    return imports, exports


def _forward_fill(series: _Series, cursors: dict[int, int], key: int, t: datetime):
    """Advance ``cursors[key]`` to the last sample ≤ ``t`` and return it, or None.

    Args:
        series: The signal's time-sorted ``(ts, value, unit)`` samples.
        cursors: Per-signal pointer cache (mutated in place); keyed by ``key``.
        key: Stable identifier for this signal's cursor.
        t: Timeline instant to forward-fill to.

    Returns:
        The ``(ts, value, unit)`` in effect at ``t``, or ``None`` when no
        sample has occurred yet.
    """
    i = cursors.get(key, -1)
    while i + 1 < len(series) and series[i + 1][0] <= t:
        i += 1
    cursors[key] = i
    return series[i] if i >= 0 else None


def build_derived_samples(
    solar: _Series,
    ac_port: _Series,
    backup: _Series,
    battery: _Series,
    grid_net: _Series,
    soc: _Series,
    *,
    battery_is_signed_role: bool,
    grid_is_fallback: bool,
    codec: TelemetryCodec,
) -> list[DerivedPowerSample]:
    """Compose full-resolution DerivedPowerSamples over the union timeline.

    A sample is emitted at every instant any contributing signal changes, with
    each other signal forward-filled to its last value (zero-order hold). The
    instant is skipped until **all** contributing signals — plus battery SoC,
    which gates ``BatteryReading`` in the live path — have a value, mirroring
    ``build_derived_power_sample``'s all-or-nothing rule. Every per-signal
    sign/unit transform runs through ``codec``; the roles below select the
    battery / grid-net fallback variant.

    Args:
        solar: PV power series.
        ac_port: Signed AC-port power series.
        backup: Backup-port power series.
        battery: Battery power series (signed-role or magnitude fallback).
        grid_net: Grid net series (primary or signed fallback).
        soc: Battery SoC series — presence-gates the sample like the live
            ``BatteryReading`` (which is dropped when SoC is unavailable).
        battery_is_signed_role: Whether ``battery`` is the signed role.
        grid_is_fallback: Whether ``grid_net`` is the AC-port fallback role.
        codec: The platform telemetry codec (carries the vendor sign-flips).

    Returns:
        Time-sorted list of DerivedPowerSample.
    """
    if not (solar and ac_port and backup and battery and grid_net and soc):
        return []

    battery_binding = SignalBinding(
        TelemetrySignal.BATTERY_POWER, "",
        SignalRole.SIGNED_NET if battery_is_signed_role else SignalRole.PRIMARY,
    )
    grid_binding = SignalBinding(
        TelemetrySignal.GRID_NET_POWER, "",
        SignalRole.FALLBACK if grid_is_fallback else SignalRole.PRIMARY,
    )

    timeline = sorted({r[0] for s in (solar, ac_port, backup, battery, grid_net, soc) for r in s})
    cursors: dict[int, int] = {}
    out: list[DerivedPowerSample] = []
    for t in timeline:
        sol = _forward_fill(solar, cursors, 0, t)
        acp = _forward_fill(ac_port, cursors, 1, t)
        bku = _forward_fill(backup, cursors, 2, t)
        bat = _forward_fill(battery, cursors, 3, t)
        grd = _forward_fill(grid_net, cursors, 4, t)
        soc_s = _forward_fill(soc, cursors, 5, t)
        if sol is None or acp is None or bku is None or bat is None or grd is None or soc_s is None:
            continue
        out.append(
            DerivedPowerSample(
                timestamp=t,
                ac_port_kw_signed=codec.to_canonical(_AC_PORT, acp[1], acp[2]),
                backup_kw=codec.to_canonical(_BACKUP, bku[1], bku[2]),
                grid_net_kw_signed=codec.to_canonical(grid_binding, grd[1], grd[2]),
                solar_kw=codec.to_canonical(_PV, sol[1], sol[2]) / 1000.0,
                battery_kw_signed=codec.to_canonical(battery_binding, bat[1], bat[2]),
            )
        )
    return out


def merge_by_timestamp(existing: Sequence, resampled: Sequence) -> tuple:
    """Return ``existing`` ∪ ``resampled`` deduplicated by timestamp, time-sorted.

    The coarse per-tick samples and the recorder samples carry different
    timestamps (cycle time vs. state ``last_updated``), so both survive the
    merge — the recorder fills between the coarse points and the coarse tip
    covers the latest instant the recorder may not have committed yet.

    Args:
        existing: The coarse per-tick samples already on the primary.
        resampled: The full-resolution recorder samples.

    Returns:
        A tuple of samples sorted by ``timestamp`` with exact-timestamp
        duplicates removed (first occurrence wins).
    """
    seen: set[datetime] = set()
    merged: list = []
    for sample in (*existing, *resampled):
        if sample.timestamp in seen:
            continue
        seen.add(sample.timestamp)
        merged.append(sample)
    merged.sort(key=lambda s: s.timestamp)
    return tuple(merged)


# How far back of already-seen time each incremental fetch re-reads. The
# steady-state fetch is just "since the last update" (≈ one cycle), but a
# transient recorder failure returns no rows for that window; re-reading a
# short overlap each cycle heals such a gap on the next successful fetch
# (the dedup in ``_merge_series`` absorbs the re-read rows). Downtime longer
# than this is still covered: ``_last_end`` only advances when ``update`` runs,
# so the gap after a restart/pause is fetched from the stale ``_last_end``.
_RESAMPLE_REFETCH_OVERLAP = timedelta(minutes=10)


@dataclass(frozen=True)
class _Roles:
    """Resolved derived/grid role entity IDs + flags for one source set."""

    battery: str
    battery_is_signed: bool
    grid: str
    grid_is_fallback: bool
    soc: str
    has_directional: bool


def _resolve_roles(sources: ResampleSources) -> _Roles:
    """Resolve the battery / grid-net / SoC roles, mirroring ``InverterController``."""
    ids = sources.entity_ids
    battery_signed = ids.get("battery_power_signed", "")
    grid_primary = ids.get("grid_power", "")
    return _Roles(
        battery=battery_signed or ids.get("battery_power", ""),
        battery_is_signed=bool(battery_signed),
        grid=grid_primary or ids.get("grid_power_fallback", ""),
        grid_is_fallback=not grid_primary,
        soc=ids.get("battery_soc", ""),
        has_directional=bool(sources.grid_import_power or sources.grid_export_power),
    )


def _merge_series(existing: _Series, new: _Series, window_start: datetime) -> _Series:
    """Return ``existing`` ∪ ``new``, deduped by timestamp, trimmed to the window.

    Args:
        existing: The entity's buffered raw samples.
        new: Freshly-fetched raw samples (may overlap ``existing``).
        window_start: Drop samples older than this (the observer window floor).

    Returns:
        Time-sorted ``(ts, value, unit)`` list with ``ts >= window_start`` and
        exact-timestamp duplicates removed (existing wins).
    """
    seen: set[datetime] = set()
    merged: _Series = []
    for row in (*existing, *new):
        ts = row[0]
        if ts < window_start or ts in seen:
            continue
        seen.add(ts)
        merged.append(row)
    merged.sort(key=lambda r: r[0])
    return merged


class RecorderResampler:
    """Stateful incremental recorder resampler for the observer streams.

    Holds an in-memory raw-state buffer per source entity. Each ``update``
    fetches only the recorder rows since the previous call (plus a short
    re-fetch overlap to self-heal a transient recorder failure), merges and
    trims them to the observer window, then rebuilds the four reading/sample
    lists from the buffers via the pure builders above. Only the recorder I/O
    is incremental — the pure rebuild runs over the trimmed in-memory buffer,
    which is cheap.

    Not persisted: a restart starts with empty buffers, so the first update
    after boot bootstraps the whole window in one fetch, then goes incremental.
    """

    def __init__(self, sources: ResampleSources) -> None:
        """Initialise with the resolved source entities; buffers start empty.

        Args:
            sources: Resolved entity IDs + platform flags (see ``ResampleSources``).
        """
        self._sources = sources
        self._roles = _resolve_roles(sources)
        self._codec: TelemetryCodec = sources.codec
        self._buffers: dict[str, _Series] = {}
        self._last_end: datetime | None = None

    def _entities(self) -> set[str]:
        """Return the unique, non-empty source entity IDs to fetch."""
        s = self._sources
        r = self._roles
        return {
            e for e in (
                s.pv_power, s.ac_port_power, s.backup_power,
                s.grid_import_power, s.grid_export_power, s.signed_grid_power,
                r.battery, r.grid, r.soc,
            ) if e
        }

    async def update(
        self, hass: Any, window_start: datetime, now: datetime,
    ) -> dict[str, list]:
        """Fetch the new recorder rows, refresh the buffers, and rebuild the streams.

        Args:
            hass: Home Assistant instance.
            window_start: Observer-window floor (older samples are trimmed).
            now: Cycle timestamp; the inclusive fetch/window end.

        Returns:
            Mapping with any of ``"pv"``, ``"grid_import"``, ``"grid_export"``,
            ``"derived"`` that could be rebuilt; each maps to a list of the
            corresponding reading/sample type. Streams with no buffered data
            are omitted.
        """
        if self._last_end is None:
            fetch_start = window_start
        else:
            fetch_start = max(window_start, self._last_end - _RESAMPLE_REFETCH_OVERLAP)
        for eid in self._entities():
            new = await fetch_series(hass, eid, fetch_start, now)
            self._buffers[eid] = _merge_series(
                self._buffers.get(eid, []), new, window_start,
            )
        self._last_end = now
        return self._build()

    def _build(self) -> dict[str, list]:
        """Rebuild the four observer streams from the current buffers."""
        s = self._sources
        r = self._roles
        buf = self._buffers
        out: dict[str, list] = {}

        pv_readings = build_pv_readings(buf.get(s.pv_power, []), self._codec)
        if pv_readings:
            out["pv"] = pv_readings

        imports, exports = build_grid_readings(
            buf.get(s.grid_import_power, []),
            buf.get(s.grid_export_power, []),
            buf.get(s.signed_grid_power, []),
            has_directional=r.has_directional,
            codec=self._codec,
        )
        if imports:
            out["grid_import"] = imports
        if exports:
            out["grid_export"] = exports

        derived = build_derived_samples(
            buf.get(s.pv_power, []),
            buf.get(s.ac_port_power, []),
            buf.get(s.backup_power, []),
            buf.get(r.battery, []),
            buf.get(r.grid, []),
            buf.get(r.soc, []),
            battery_is_signed_role=r.battery_is_signed,
            grid_is_fallback=r.grid_is_fallback,
            codec=self._codec,
        )
        if derived:
            out["derived"] = derived

        return out
