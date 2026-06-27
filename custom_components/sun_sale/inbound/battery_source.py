"""Battery telemetry source — decoupled from the inverter.

Battery state (SoC, signed power, today's charge/discharge energy) is what the
scheduler and the capacity estimator actually reason about. Historically it was
read straight off the inverter, but a dedicated battery BMS (JK-BMS or similar)
often reports SoC and pack power more accurately and at higher resolution than
the inverter's view. :class:`BatterySource` is the seam that lets that data come
from the BMS when available, with the inverter as a transparent fallback.

This phase ships a single implementation, :class:`InverterBatterySource`, which
delegates 1:1 to the inverter — so behaviour is identical to before the seam
existed. A future :class:`JkBmsBatterySource` plus a :class:`ChainedBatterySource`
(per-field BMS-primary / inverter-fallback) can be added without touching the
translator or the capacity estimator.

Sign conventions match the rest of the codebase: battery ``power_kw`` is
positive while charging, negative while discharging.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..ha_state import read_power_kw, read_soc_fraction
from ..outbound.inverter import InverterController


@runtime_checkable
class BatterySource(Protocol):
    """Platform-neutral battery telemetry surface.

    Every read returns ``None`` on unavailability so a partial source can be
    composed with a fallback *per field* (see :class:`ChainedBatterySource`) —
    a BMS that publishes only SoC still draws power / energy from the inverter.
    A ``None`` SoC in particular is load-bearing: the battery translator drops
    the whole reading rather than letting the scheduler plan against a
    fabricated state of charge. The translator applies the 0.0 default for a
    missing power reading at its edge.
    """

    def soc(self) -> float | None:
        """Return state of charge as a 0.0–1.0 fraction, or ``None`` when unavailable."""
        ...

    def power_kw(self) -> float | None:
        """Return battery power in kW (positive = charging), or ``None`` when unavailable."""
        ...

    def charge_energy_today_kwh(self) -> float | None:
        """Return today's cumulative charge energy in kWh, or ``None`` when unavailable."""
        ...

    def discharge_energy_today_kwh(self) -> float | None:
        """Return today's cumulative discharge energy in kWh, or ``None`` when unavailable."""
        ...


class InverterBatterySource:
    """Battery source backed entirely by the inverter's telemetry.

    Delegates each read to the inverter controller, preserving the exact
    semantics (sign flips, percentage normalisation, unit scaling, fallbacks)
    the inverter already implements. This is the default source and the fallback
    leg of any future BMS-primary chain.
    """

    def __init__(self, inverter: InverterController) -> None:
        """Wrap the inverter controller as a battery source.

        Args:
            inverter: The inverter controller to read battery telemetry from.
                Typed against the concrete :class:`InverterController` because
                the battery getters delegated to below are inverter telemetry,
                deliberately excluded from the platform-neutral
                :class:`InverterDriver` interface.
        """
        self._inverter = inverter

    def soc(self) -> float | None:
        """Return battery SoC from the inverter as a 0.0–1.0 fraction, or ``None``."""
        return self._inverter.get_battery_soc()

    def power_kw(self) -> float:
        """Return battery power in kW (positive = charging) from the inverter."""
        return self._inverter.get_battery_power()

    def charge_energy_today_kwh(self) -> float | None:
        """Return today's cumulative charge energy in kWh from the inverter, or ``None``."""
        return self._inverter.get_battery_charge_energy_today()

    def discharge_energy_today_kwh(self) -> float | None:
        """Return today's cumulative discharge energy in kWh from the inverter, or ``None``."""
        return self._inverter.get_battery_discharge_energy_today()


class JkBmsBatterySource:
    """Battery source backed by a JK-BMS (or similar) over its HA entities.

    A pack-level BMS typically reports SoC and pack power more accurately and at
    higher resolution than the inverter's AC-side view, so it is the *primary*
    leg of a :class:`ChainedBatterySource`. It does **not** expose the inverter's
    daily-resetting AC charge / discharge energy counters the capacity estimator
    consumes, so those reads return ``None`` here — the chain falls back to the
    inverter for them.

    Sign convention: the mapped power sensor is read as positive = charging
    (sunSale's convention). A BMS that reports discharge-positive should be
    mapped to a template sensor that flips the sign, or handled by a future
    polarity option.
    """

    def __init__(self, hass: Any, soc_entity: str, power_entity: str) -> None:
        """Wrap a JK-BMS exposed through HA entities.

        Args:
            hass: Home Assistant instance for state reads.
            soc_entity: SoC sensor entity ID (``%`` or 0.0–1.0); empty disables.
            power_entity: Signed pack-power sensor entity ID (positive =
                charging); empty disables the power read.
        """
        self._hass = hass
        self._soc_entity = soc_entity
        self._power_entity = power_entity

    def soc(self) -> float | None:
        """Return the BMS state of charge as a 0.0–1.0 fraction, or ``None``."""
        return read_soc_fraction(self._hass, self._soc_entity)

    def power_kw(self) -> float | None:
        """Return the BMS pack power in kW (positive = charging), or ``None``."""
        return read_power_kw(self._hass, self._power_entity)

    def charge_energy_today_kwh(self) -> float | None:
        """Return ``None`` — a BMS has no daily-resetting AC charge counter."""
        return None

    def discharge_energy_today_kwh(self) -> float | None:
        """Return ``None`` — a BMS has no daily-resetting AC discharge counter."""
        return None


class ChainedBatterySource:
    """Compose a primary battery source over a fallback, per field.

    Each field prefers the primary's value and falls back to the secondary only
    when the primary returns ``None``. This lets a BMS that publishes only SoC
    (and pack power) still source charge / discharge energy from the inverter —
    every field is resolved independently rather than all-or-nothing.
    """

    def __init__(self, primary: BatterySource, fallback: BatterySource) -> None:
        """Chain ``primary`` over ``fallback``.

        Args:
            primary: Preferred source (e.g. a BMS).
            fallback: Source consulted per field when the primary returns
                ``None`` (e.g. the inverter — the terminal source).
        """
        self._primary = primary
        self._fallback = fallback

    def soc(self) -> float | None:
        """Return the primary SoC, or the fallback's when the primary is unavailable."""
        value = self._primary.soc()
        return value if value is not None else self._fallback.soc()

    def power_kw(self) -> float | None:
        """Return the primary power, or the fallback's when the primary is unavailable."""
        value = self._primary.power_kw()
        return value if value is not None else self._fallback.power_kw()

    def charge_energy_today_kwh(self) -> float | None:
        """Return the primary charge energy, or the fallback's when unavailable."""
        value = self._primary.charge_energy_today_kwh()
        return value if value is not None else self._fallback.charge_energy_today_kwh()

    def discharge_energy_today_kwh(self) -> float | None:
        """Return the primary discharge energy, or the fallback's when unavailable."""
        value = self._primary.discharge_energy_today_kwh()
        return (
            value if value is not None
            else self._fallback.discharge_energy_today_kwh()
        )
