"""Per-platform sign / unit transforms — the pure telemetry codec.

A :class:`TelemetryCodec` maps a raw vendor reading ``(binding, raw, unit)`` to
its signal's canonical unit and sign (see :class:`..binding.TelemetrySignal`).
It is the *single source of truth* for vendor polarity. One instance — selected
once by :class:`..outbound.inverter.InverterController` from the platform and
threaded from there into the live read getters, the live-flow panel spec, and
the recorder resampler — serves every path, so none of them can drift. This
replaced the per-call ``is_solis`` / ``sign=-1 if solis`` flags that each path
used to carry independently.

Pure module: the only sunSale import is ``ha_state``'s unit-normalisation
helpers, which touch no Home Assistant state. Platform identity is the codec
*instance* (:class:`SolisCodec` vs :class:`GenericCodec`), never a parameter.

The transforms here are deliberately bit-for-bit faithful to the live readers
they back (``inbound/observer/recorder_resample.py`` already delegates here; the
live translators / controller are the next slice):

  * ``PV_POWER``      → ``PvPowerTranslator.parse``
  * ``BATTERY_POWER`` → ``InverterController.get_battery_power``
  * ``GRID_NET_POWER``→ ``InverterController.get_grid_power``
  * ``GRID_IMPORT/EXPORT`` → the directional observers
  * ``AC_PORT_POWER`` → ``AcPortPowerTranslator`` (sign preserved)
  * ``BACKUP_POWER``  → ``BackupPowerTranslator`` (clamped ≥ 0)
  * ``BATTERY_SOC``   → ``read_soc_fraction``
  * ``*_DAILY``       → ``normalize_energy_to_kwh``
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...ha_state import normalize_energy_to_kwh, normalize_power_to_kw
from .binding import SignalBinding, SignalRole, TelemetrySignal


def _pv_watts(raw: float, unit: str) -> float:
    """Return non-negative PV power in watts.

    Reproduces ``PvPowerTranslator.parse`` exactly: a ``W`` (or blank) unit is
    taken as watts, anything else is treated as kW and scaled ×1000. This is a
    narrower rule than :func:`normalize_power_to_kw` (no mW / MW handling) and is
    kept identical to the live reader on purpose.

    Args:
        raw: Raw sensor value.
        unit: HA ``unit_of_measurement`` (``""`` defaults to watts).

    Returns:
        PV power in watts, clamped to ≥ 0.
    """
    watts = raw if (unit or "W").upper() == "W" else raw * 1000.0
    return max(0.0, watts)


def _soc_fraction(raw: float, unit: str) -> float:
    """Return SoC as a 0.0–1.0 fraction. Mirrors ``read_soc_fraction``.

    A ``%`` unit divides by 100 unconditionally; a unit-less value uses the
    magnitude heuristic (divide only when > 1.0) so an already-normalised
    fraction passes through.

    Args:
        raw: Raw SoC value.
        unit: HA ``unit_of_measurement`` (``"%"`` or blank).

    Returns:
        SoC as a 0.0–1.0 fraction.
    """
    if unit.strip() == "%" or raw > 1.0:
        return raw / 100.0
    return raw


@runtime_checkable
class TelemetryCodec(Protocol):
    """Map a raw vendor reading to its signal's canonical unit and sign."""

    def to_canonical(self, binding: SignalBinding, raw: float, unit: str) -> float:
        """Return ``raw`` (with ``unit``) for ``binding`` in canonical terms.

        Args:
            binding: The resolved signal/entity/role this reading came from.
            raw: Raw numeric value as published by the entity.
            unit: The entity's ``unit_of_measurement`` (``""`` when absent).

        Returns:
            The value in the signal's canonical unit and sign.

        Raises:
            ValueError: When ``binding.signal`` is not a codec-handled scalar.
        """
        ...


class _BaseTelemetryCodec:
    """Shared unit/clamp transforms; vendor polarity is a subclass override.

    Subclasses set :attr:`_signed_battery_factor` and :attr:`_fallback_grid_factor`
    to ``-1.0`` for the roles whose vendor convention is inverted relative to
    sunSale; everything else — unit normalisation, directional clamps, the
    net→directional split, SoC and energy scaling — is platform-independent and
    lives here.
    """

    # Sign applied to a SIGNED_NET battery reading (vendor convention → sunSale
    # positive=charging). 1.0 = already correct; -1.0 = discharge-positive vendor.
    _signed_battery_factor: float = 1.0

    # Sign applied to a FALLBACK grid reading (vendor convention → sunSale
    # positive=import). 1.0 = already correct; -1.0 = inverter→grid-positive vendor.
    _fallback_grid_factor: float = 1.0

    def to_canonical(self, binding: SignalBinding, raw: float, unit: str) -> float:
        """Dispatch on the signal/role to the matching canonical transform."""
        signal, role = binding.signal, binding.role

        if signal is TelemetrySignal.PV_POWER:
            return _pv_watts(raw, unit)

        if signal is TelemetrySignal.BATTERY_SOC:
            return _soc_fraction(raw, unit)

        if signal in _DAILY_ENERGY_SIGNALS:
            return normalize_energy_to_kwh(raw, unit)

        if signal is TelemetrySignal.BACKUP_POWER:
            return max(0.0, normalize_power_to_kw(raw, unit))

        if signal is TelemetrySignal.AC_PORT_POWER:
            # Sign preserved (vendor convention: positive = inverter→grid).
            return normalize_power_to_kw(raw, unit)

        kw = normalize_power_to_kw(raw, unit)

        if signal is TelemetrySignal.BATTERY_POWER:
            return kw * self._signed_battery_factor if role is SignalRole.SIGNED_NET else kw

        if signal is TelemetrySignal.GRID_NET_POWER:
            return kw * self._fallback_grid_factor if role is SignalRole.FALLBACK else kw

        if signal is TelemetrySignal.GRID_IMPORT:
            # PRIMARY = directional import sensor; SIGNED_NET = import side of a
            # signed net (positive=import). Both clamp ≥ 0 → identical formula.
            return max(0.0, kw)

        if signal is TelemetrySignal.GRID_EXPORT:
            # SIGNED_NET projects the export side of an import-positive net.
            return max(0.0, -kw) if role is SignalRole.SIGNED_NET else max(0.0, kw)

        raise ValueError(f"codec has no transform for {signal!r}")


_DAILY_ENERGY_SIGNALS = frozenset(
    {
        TelemetrySignal.GENERATION_DAILY,
        TelemetrySignal.GRID_IMPORT_DAILY,
        TelemetrySignal.GRID_EXPORT_DAILY,
    }
)


def signed_polarity(
    codec: TelemetryCodec, signal: TelemetrySignal, role: SignalRole
) -> int:
    """Return the ±1 sign ``codec`` applies to a unit-magnitude reading.

    Probes the codec with ``+1 kW`` so a consumer that needs just the polarity
    — not the full transform — can stay codec-driven. Intended for the
    flip-only power roles (``BATTERY_POWER``/``SIGNED_NET``,
    ``GRID_NET_POWER``/``FALLBACK``) where the codec output is exactly ``±1``;
    it is **not** meaningful for clamped or split signals.

    Args:
        codec: The platform telemetry codec.
        signal: The signal whose polarity to probe.
        role: The role under which ``signal`` is read.

    Returns:
        ``-1`` when the codec flips the sign for ``(signal, role)``, else ``+1``.
    """
    return -1 if codec.to_canonical(SignalBinding(signal, "", role), 1.0, "kW") < 0 else 1


class GenericCodec(_BaseTelemetryCodec):
    """Codec for platforms whose sensors already match sunSale conventions.

    Applies unit normalisation and clamps but no sign flips — used for a manual
    entity mapping where the user is expected to supply sensors in sunSale's
    own directions (battery positive=charging, grid net positive=import).
    """


class SolisCodec(_BaseTelemetryCodec):
    """Codec for the Solis platform.

    Flips the two signed roles whose Solis convention is inverted relative to
    sunSale: the battery net sensor is discharge-positive, and the
    ``ac_grid_port_power`` fallback is inverter→grid-positive.
    """

    _signed_battery_factor = -1.0
    _fallback_grid_factor = -1.0
