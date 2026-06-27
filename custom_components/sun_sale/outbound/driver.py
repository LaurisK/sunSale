"""Inverter driver interface — the platform-neutral control seam.

This module defines the contract every inverter platform must satisfy so the
rest of sunSale can dispatch a semantic :class:`~..contract.models.StorageMode`
without knowing how a particular inverter realises it (Solis register writes,
a future Huawei/SolarEdge service call, …).

Design intent
-------------
The pipeline scheduler already speaks in semantic ``StorageMode`` values; only
the *adapter* layer knows about registers. The concrete driver today is
:class:`..outbound.solis_driver.SolisDriver`, selected by
:func:`..outbound.driver_factory.make_inverter_driver`.

The interface is split in two:

  * :class:`InverterDriver` — the minimal, fully platform-neutral surface: drive
    a mode (``apply_mode``), keep a volatile mode alive (``refresh_rc``), and
    read grid power. Nothing Solis-specific. ``InverterController`` (the Solis
    register reader/writer) conforms structurally.
  * :class:`InverterControlDriver` — adds the mode-composition + decoding the
    control module orchestrates against: ``spec_for`` / ``needs_keepalive`` /
    ``control_surface`` / ``decode_observed`` / ``observe`` /
    ``observed_raw_state``. All per-platform register knowledge lives behind
    these; the control module's verify/reconcile loop never names a register.

Both interfaces deliberately exclude **battery telemetry** (SoC, battery power,
charge/discharge energy) — that lives in
:class:`..inbound.battery_source.BatterySource` so the battery can be sourced
from a dedicated BMS (JK-BMS or similar) with the inverter as fallback. Grid
power stays here because it is an inverter-side measurement, not a battery one.

:class:`ControlRow` is the platform-neutral row ``control_surface`` returns;
the panel renders it (today via :meth:`ControlRow.as_dict`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..contract.models import InverterModeReading, StorageMode


@dataclass(frozen=True)
class ControlRow:
    """One platform-neutral intended-vs-observed control row for the panel.

    A control point is whatever the driver actuates — a Solis register, an entity
    ``number`` / ``select`` / ``switch``, etc. The row is the neutral shape the
    panel renders; the driver chooses the ``name`` / ``label`` (and may use its
    own vocabulary there, e.g. Solis ``reg_43110``).

    Attributes:
        name: Stable machine key for the control point (driver-defined, e.g.
            ``reg_43110`` on Solis or ``work_mode_select`` on an entity driver).
        label: Human-readable label rendered in the panel.
        desired: The value the driver targets for this control point, or
            ``None`` when the commanded mode leaves it at the hardware default
            (an informational, no-target row).
        observed: The current readback, or ``None`` when unavailable.
        match: Tri-state — ``True`` on match, ``False`` when observed is missing
            or differs beyond the driver's write tolerance, and ``None`` when
            ``desired`` is ``None`` (no target to compare against).
    """

    name: str
    label: str
    desired: Any
    observed: Any
    match: bool | None

    def as_dict(self) -> dict[str, Any]:
        """Return the row as the plain dict the sensor attributes / panel JS consume."""
        return {
            "name": self.name,
            "label": self.label,
            "desired": self.desired,
            "observed": self.observed,
            "match": self.match,
        }


@runtime_checkable
class InverterDriver(Protocol):
    """Platform-neutral inverter control + grid-telemetry surface.

    Any concrete driver (Solis today; Huawei/SolarEdge/GoodWe later) implements
    this. :class:`..outbound.inverter.InverterController` already satisfies it
    structurally. Battery telemetry is intentionally absent — see
    :class:`..inbound.battery_source.BatterySource`.

    All reads degrade to ``None`` (or a documented fallback) on
    entity-unavailability rather than raising, so the pipeline can fall through
    to ``no_target`` instead of crashing a cycle.

    ``spec`` is an **opaque** per-platform token (typed ``Any``): the generic
    layer obtains one from :meth:`InverterControlDriver.spec_for` and hands it
    straight back to ``apply_mode`` / ``refresh_rc`` / ``needs_keepalive``
    without inspecting it. Its concrete shape (Solis: a register tuple) lives in
    the driver's own module, never in ``contract/``.
    """

    # --- Write side ----------------------------------------------------- #

    async def apply_mode(
        self, mode: StorageMode, spec: Any, force: bool = False
    ) -> None:
        """Drive the inverter to ``mode`` using the minimum set of writes.

        Args:
            mode: Target storage mode (semantic).
            spec: Opaque per-platform spec for ``mode`` (from ``spec_for``).
            force: Skip the cached-readback comparison and always issue the
                underlying write — used by the verify loop on commanded change.
        """
        ...

    async def refresh_rc(self, spec: Any) -> None:
        """Re-arm any volatile keep-alive the held mode needs (no-op if none).

        On Solis this re-issues the Remote-Control function before its deadman
        timeout expires; platforms without a volatile setpoint implement this
        as a no-op.

        Args:
            spec: Opaque per-platform spec of the currently held mode.
        """
        ...

    # --- Grid telemetry (inverter-side measurement) --------------------- #

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing); 0.0 when unavailable."""
        ...


@runtime_checkable
class InverterControlDriver(InverterDriver, Protocol):
    """An :class:`InverterDriver` that also owns mode composition + decoding.

    This is the surface the control module orchestrates against — everything
    platform-specific about *which* control points a mode touches, how to
    compare them, and how to decode the live hardware state lives behind these
    three methods, so the control module's verify / reconcile state machine is
    platform-neutral. Adding a new platform means writing one of these and
    selecting it in :func:`..outbound.driver_factory.make_inverter_driver`.
    """

    def spec_for(self, mode: StorageMode) -> Any | None:
        """Return the opaque per-platform spec for ``mode``, or ``None``.

        The returned token is passed back to ``apply_mode`` / ``refresh_rc`` /
        ``needs_keepalive`` uninspected by the generic layer.
        """
        ...

    def needs_keepalive(self, spec: Any) -> bool:
        """Return whether holding ``spec`` requires a periodic volatile keep-alive.

        Lets the control module gate its keep-alive timer without inspecting any
        platform-specific spec field. Solis returns ``True`` for RC-backed modes;
        platforms with no volatile state return ``False``.

        Args:
            spec: Opaque per-platform spec (from ``spec_for``).
        """
        ...

    def control_surface(self, commanded: StorageMode | None) -> list[ControlRow]:
        """Return the always-populated intended-vs-observed rows for the panel.

        Reads every control point live and pairs it with ``commanded``'s target
        (or ``None`` — a no-target row — when the mode leaves it at the hardware
        default, or when ``commanded`` is ``None``).

        Args:
            commanded: The mode whose targets to compare against, or ``None``
                for an observed-only readout.

        Returns:
            One :class:`ControlRow` per control point, in apply-mode write order.
        """
        ...

    def decode_observed(self) -> StorageMode:
        """Decode the inverter's current StorageMode from live readbacks.

        Returns:
            Best-fit StorageMode; ``UNKNOWN`` when the state maps to no mode.
        """
        ...

    def observe(self, now: datetime) -> InverterModeReading:
        """Read the mode-defining state once and package an InverterModeReading.

        The reading's raw fields and decoded ``mode`` are captured from the same
        read, so they stay consistent. Consumed by the mode-history log and the
        diagnostic sensor.

        Args:
            now: Cycle timestamp recorded as the reading's timestamp.

        Returns:
            The current observed state; unreadable fields are ``None``.
        """
        ...

    def observed_raw_state(self) -> int | None:
        """Return the live raw mode-state code (the ``raw_state`` value), or ``None``.

        A lightweight single read used by the control module's verify loop as a
        diagnostic ("what raw state did we read this verify-tick"), distinct from
        the full ``observe`` snapshot. Platform-defined encoding (Solis: register
        43110); ``None`` when unreadable.
        """
        ...
