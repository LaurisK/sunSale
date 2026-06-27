"""Telemetry signal vocabulary and the resolved (signal → entity) binding.

Pure module: imports nothing from Home Assistant or the rest of sunSale, so it
can be shared by the pure codec, the future live reader, and the entity
resolver without creating an import cycle.

A :class:`TelemetrySignal` is a physical quantity the pipeline consumes, named
once and independent of any vendor. A :class:`SignalBinding` pairs a signal with
the concrete entity that carries it plus the :class:`SignalRole` that says *how*
to interpret that entity's raw value — the role replaces the
``is_signed_role`` / ``is_fallback`` / ``has_directional`` booleans currently
threaded through ``inbound/observer/recorder_resample.py``. The platform's
:class:`~.codec.TelemetryCodec` turns ``(binding, raw, unit)`` into the signal's
canonical unit and sign.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class TelemetrySignal(Enum):
    """A physical telemetry quantity the pipeline consumes, in canonical terms.

    Canonical units / signs (the codec's output contract):

      * ``PV_POWER``                      — watts, ≥ 0.
      * ``BATTERY_POWER``                 — kW, positive = charging.
      * ``GRID_NET_POWER``                — kW, positive = import.
      * ``GRID_IMPORT`` / ``GRID_EXPORT`` — kW, ≥ 0 (directional).
      * ``AC_PORT_POWER``                 — kW, positive = inverter→grid (raw;
        ``derived.py`` flips it). Carried in vendor convention deliberately.
      * ``BACKUP_POWER``                  — kW magnitude, ≥ 0.
      * ``BATTERY_SOC``                   — 0.0–1.0 fraction.
      * ``GENERATION_DAILY`` / ``GRID_IMPORT_DAILY`` / ``GRID_EXPORT_DAILY``
        — kWh, daily-resetting counters.

    Non-scalar signals (the inverter clock, mode-state registers) are parsed
    elsewhere and are intentionally absent — the codec handles scalar
    sign/unit transforms only.
    """

    PV_POWER = auto()
    BATTERY_POWER = auto()
    GRID_NET_POWER = auto()
    GRID_IMPORT = auto()
    GRID_EXPORT = auto()
    AC_PORT_POWER = auto()
    BACKUP_POWER = auto()
    BATTERY_SOC = auto()
    GENERATION_DAILY = auto()
    GRID_IMPORT_DAILY = auto()
    GRID_EXPORT_DAILY = auto()


class SignalRole(Enum):
    """How a bound entity's raw value maps onto its signal.

    A signal can be sourced in more than one shape; the role tells the codec
    which transform to apply so vendor polarity is decided once at resolution
    rather than re-derived per sample:

      * ``PRIMARY``    — the canonical sensor for the signal, already in the
        signal's own direction (a directional import sensor; the Solis derived
        ``grid_power_net`` which is already import-positive).
      * ``SIGNED_NET`` — a signed net sensor projected onto one side. Battery:
        the inverter's discharge-positive net power. Grid import/export: split
        from a single import-positive net sensor.
      * ``FALLBACK``   — a secondary derivation used when ``PRIMARY`` is absent
        (the Solis ``ac_grid_port_power``, inverter→grid-positive, stood in for
        grid net).
    """

    PRIMARY = auto()
    SIGNED_NET = auto()
    FALLBACK = auto()


@dataclass(frozen=True)
class SignalBinding:
    """One resolved mapping of a telemetry signal to an HA entity.

    Attributes:
        signal: The physical quantity this entity carries.
        entity_id: The HA entity ID; empty string means "unmapped".
        role: How to interpret the entity's raw value (see :class:`SignalRole`).
    """

    signal: TelemetrySignal
    entity_id: str
    role: SignalRole = SignalRole.PRIMARY
