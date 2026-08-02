"""Live telemetry reader — the HA-coupled read surface over the pure codec.

This is the live counterpart to the recorder resampler: where the resampler
replays historical recorder rows through the codec, :class:`HaTelemetryReader`
reads the *current* HA state for a :class:`~.binding.TelemetrySignal` and runs
the same codec transform, so the live and historical paths share one definition
of vendor sign / unit conventions.

A signal maps to an ordered tuple of bindings whose meaning depends on the
signal's sourcing semantics, both encoded by :func:`resolve_bindings`:

  * **Fallback chain** (battery / grid net power) — the controller getters try
    the preferred role, then fall back to the next on *runtime* unavailability.
    The reader walks the chain and returns the first usable reading.
  * **Single config-pick** (PV, directional grid, ac-port, backup) — the
    translator reads exactly the one entity its config selected; a one-element
    chain means the reader never substitutes another source at runtime.

Reads are freshness-aware: a ``max_age_s`` window rejects a state whose
``last_updated`` has frozen (a silently-stalled Modbus chain), matching
``ha_state.read_power_kw``. ``hass`` is passed per call so one reader instance
serves both the hass-bound controller and the per-cycle observer translators.

Only this module is HA-coupled (it reads ``hass.states``); :mod:`.binding` and
:mod:`.codec` stay pure.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ...ha_state import available_state
from .binding import SignalBinding, SignalRole, TelemetrySignal
from .codec import TelemetryCodec

_NO_MAX_AGE = float("inf")


def _read_raw(
    hass: Any,
    entity_id: str,
    *,
    now: datetime | None = None,
    max_age_s: float = _NO_MAX_AGE,
) -> tuple[float | None, str]:
    """Return ``(value, unit)`` for ``entity_id``, or ``(None, "")`` when unusable.

    Applies the same not-a-reading guard as every HA-edge reader (unmapped /
    absent / ``unavailable`` / ``unknown`` / non-numeric) via
    :func:`..ha_state.available_state`, then — when ``max_age_s`` is finite —
    the same staleness check as ``ha_state.read_power_kw``: a tz-aware
    ``last_updated`` older than the window is rejected, while a missing or naive
    timestamp is kept.

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID to read; empty yields ``(None, "")``.
        now: Reference instant for the freshness check; defaults to UTC now.
        max_age_s: Maximum age (seconds) of ``last_updated``; ``inf`` disables.

    Returns:
        ``(float_value, unit_string)`` when usable, else ``(None, "")``.
    """
    state = available_state(hass, entity_id)
    if state is None:
        return None, ""
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None, ""
    if max_age_s != _NO_MAX_AGE:
        last_updated = getattr(state, "last_updated", None)
        if last_updated is not None and getattr(last_updated, "tzinfo", None) is not None:
            ref = now or datetime.now(UTC)
            if (ref - last_updated).total_seconds() > max_age_s:
                return None, ""
    unit = str((state.attributes or {}).get("unit_of_measurement") or "").strip()
    return value, unit


@runtime_checkable
class TelemetryReader(Protocol):
    """Read the current canonical value of a telemetry signal, or ``None``."""

    def read(
        self,
        hass: Any,
        signal: TelemetrySignal,
        *,
        now: datetime | None = None,
        max_age_s: float = _NO_MAX_AGE,
    ) -> float | None:
        """Return ``signal``'s current value in canonical units, or ``None``."""
        ...


class HaTelemetryReader:
    """Live reader: walk a signal's binding chain and codec-transform the first hit.

    Holds the per-signal binding chains and the platform codec; ``hass`` is
    supplied per :meth:`read` so one instance serves the hass-bound controller
    and the per-cycle DAG translators alike.
    """

    def __init__(
        self,
        bindings: dict[TelemetrySignal, tuple[SignalBinding, ...]],
        codec: TelemetryCodec,
    ) -> None:
        """Initialise with the per-signal binding chains and the platform codec.

        Args:
            bindings: Ordered chain per signal (see :func:`resolve_bindings`).
            codec: The platform telemetry codec applying sign / unit transforms.
        """
        self._bindings = bindings
        self._codec = codec

    def read(
        self,
        hass: Any,
        signal: TelemetrySignal,
        *,
        now: datetime | None = None,
        max_age_s: float = _NO_MAX_AGE,
    ) -> float | None:
        """Return ``signal``'s canonical value from the first readable binding.

        Args:
            hass: Home Assistant instance for the state read.
            signal: The telemetry signal to read.
            now: Reference instant for the freshness check.
            max_age_s: Freshness window in seconds; ``inf`` disables it.

        Returns:
            The canonical value of the first binding in ``signal``'s chain that
            yields a usable (and, when ``max_age_s`` is finite, fresh) reading,
            or ``None`` when none do.
        """
        for binding in self._bindings.get(signal, ()):
            value, unit = _read_raw(hass, binding.entity_id, now=now, max_age_s=max_age_s)
            if value is not None:
                return self._codec.to_canonical(binding, value, unit)
        return None


def _chain(*candidates: tuple[TelemetrySignal, str, SignalRole]) -> tuple[SignalBinding, ...]:
    """Return bindings for the mapped candidates, in order; unmapped ones dropped."""
    return tuple(
        SignalBinding(signal, entity_id, role)
        for (signal, entity_id, role) in candidates
        if entity_id
    )


def _directional_chain(
    signal: TelemetrySignal, directional: str, signed: str
) -> tuple[SignalBinding, ...]:
    """Return the single config-pick binding for a directional grid signal.

    Mirrors ``_DirectionalPowerObserver``: prefer the directional sensor
    (``PRIMARY``), else project the signed net sensor onto this side
    (``SIGNED_NET``). At most one binding — there is **no** runtime fallback
    from a mapped-but-stale directional sensor to the signed one.
    """
    if directional:
        return _chain((signal, directional, SignalRole.PRIMARY))
    return _chain((signal, signed, SignalRole.SIGNED_NET))


def resolve_bindings(
    entity_ids: dict[str, str],
    *,
    pv_power: str = "",
    ac_port_power: str = "",
    backup_power: str = "",
    grid_import_power: str = "",
    grid_export_power: str = "",
    signed_grid_power: str = "",
) -> dict[TelemetrySignal, tuple[SignalBinding, ...]]:
    """Build the per-signal binding chains for the live reader.

    Each signal's chain encodes its sourcing semantics:

      * **Battery power** — signed net (``battery_power_signed``, ``SIGNED_NET``)
        then magnitude (``battery_power``, ``PRIMARY``); a *runtime* fallback.
      * **Grid net power** — primary net (``grid_power``, ``PRIMARY``) then
        ac-port fallback (``grid_power_fallback``, ``FALLBACK``); runtime.
      * **Directional grid** (import / export) — the directional sensor, else
        the signed net projected onto that side; a single *config* pick.
      * **PV / ac-port / backup** — the one configured sensor (``PRIMARY``).

    Unmapped entities are dropped, so a chain may be empty when its signal has
    no source. The battery / grid-net entities come from ``entity_ids`` (the
    controller's role map); the observer entities are passed explicitly.

    Args:
        entity_ids: The controller's role→entity-ID map (battery / grid net).
        pv_power: Instantaneous PV power sensor.
        ac_port_power: Signed AC grid-port power sensor.
        backup_power: Backup-port output power sensor.
        grid_import_power: Directional grid-import sensor.
        grid_export_power: Directional grid-export sensor.
        signed_grid_power: Signed net grid sensor (positive=import) used to
            split a direction when its directional sensor is unmapped.

    Returns:
        Mapping of each telemetry signal to its ordered binding chain.
    """
    return {
        TelemetrySignal.BATTERY_POWER: _chain(
            (TelemetrySignal.BATTERY_POWER,
             entity_ids.get("battery_power_signed", ""), SignalRole.SIGNED_NET),
            (TelemetrySignal.BATTERY_POWER,
             entity_ids.get("battery_power", ""), SignalRole.PRIMARY),
        ),
        TelemetrySignal.GRID_NET_POWER: _chain(
            (TelemetrySignal.GRID_NET_POWER,
             entity_ids.get("grid_power", ""), SignalRole.PRIMARY),
            (TelemetrySignal.GRID_NET_POWER,
             entity_ids.get("grid_power_fallback", ""), SignalRole.FALLBACK),
        ),
        TelemetrySignal.PV_POWER: _chain(
            (TelemetrySignal.PV_POWER, pv_power, SignalRole.PRIMARY),
        ),
        TelemetrySignal.AC_PORT_POWER: _chain(
            (TelemetrySignal.AC_PORT_POWER, ac_port_power, SignalRole.PRIMARY),
        ),
        TelemetrySignal.BACKUP_POWER: _chain(
            (TelemetrySignal.BACKUP_POWER, backup_power, SignalRole.PRIMARY),
        ),
        TelemetrySignal.GRID_IMPORT: _directional_chain(
            TelemetrySignal.GRID_IMPORT, grid_import_power, signed_grid_power,
        ),
        TelemetrySignal.GRID_EXPORT: _directional_chain(
            TelemetrySignal.GRID_EXPORT, grid_export_power, signed_grid_power,
        ),
    }
