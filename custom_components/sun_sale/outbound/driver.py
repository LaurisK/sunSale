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
    a mode (``apply_mode``), keep holding it (``hold``), release driver-owned
    resources (``shutdown``), and read grid power. Nothing Solis-specific.
    ``InverterController`` (the Solis register reader/writer) conforms
    structurally.
  * :class:`InverterControlDriver` — adds the mode-composition + decoding the
    control module orchestrates against: ``spec_for`` /
    ``control_surface`` / ``decode_observed`` / ``observe`` /
    ``observed_raw_state``. All per-platform register knowledge lives behind
    these; the control module's verify/reconcile loop never names a register.

**Liveness is a driver concern.** Platforms differ in whether a commanded mode
persists at all: a written setting holds indefinitely, a Solis RC function
expires in ~5 minutes, a Remote Dispatch failsafe in whatever it was told. The
protocol therefore says only ``hold`` — "this is still the target" — and each
driver decides what that costs, running its own :class:`..outbound.heartbeat.Heartbeat` when its
hardware deadman is faster than the coordinator cycle. No keep-alive cadence,
flag, or vocabulary appears in the neutral surface.

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
from typing import Any, Literal, Protocol, runtime_checkable

from ..contract.models import InverterCapability, InverterModeReading, StorageMode

ControlStatus = Literal["match", "mismatch", "unknown", "no_target"]


@dataclass(frozen=True)
class ControlRow:
    """One platform-neutral intended-vs-observed control row for the panel.

    A control point is whatever the driver actuates — a Solis register, an entity
    ``number`` / ``select`` / ``switch``, etc. The row is the neutral shape the
    panel renders; the driver chooses the ``name`` / ``label`` (and may use its
    own vocabulary there, e.g. Solis ``reg_43110``).

    ``status`` is the authoritative four-state verdict. It exists because
    "the readback disagrees with the target" and "there is no readback" demand
    opposite responses from the control loop: the first is drift worth
    re-commanding, the second is an upstream comms gap where re-writing achieves
    nothing and a red badge is a lie. Collapsing both into ``match=False`` (as
    this row used to) wedges the verify loop for as long as an entity stays
    unavailable.

    Attributes:
        name: Stable machine key for the control point (driver-defined, e.g.
            ``reg_43110`` on Solis or ``work_mode_select`` on an entity driver).
        label: Human-readable label rendered in the panel.
        desired: The value the driver targets for this control point, or
            ``None`` when the commanded mode leaves it at the hardware default
            (an informational, no-target row).
        observed: The current readback, or ``None`` when unavailable.
        status: ``match`` — readback agrees within the driver's write tolerance;
            ``mismatch`` — readback was obtained and disagrees; ``unknown`` —
            targeted, but no readback available; ``no_target`` — ``desired`` is
            ``None``, nothing to compare.
    """

    name: str
    label: str
    desired: Any
    observed: Any
    status: ControlStatus

    @property
    def match(self) -> bool | None:
        """Return the legacy tri-state match flag derived from ``status``.

        Kept so the panel JS, sensor attributes and debug API keep their existing
        shape: ``True`` on match, ``None`` for a no-target row, ``False``
        otherwise. Callers that must distinguish *drift* from *unreadable* —
        the control module's drift check above all — read ``status`` instead.
        """
        if self.status == "no_target":
            return None
        return self.status == "match"

    @property
    def readable(self) -> bool:
        """Return whether a readback was obtained for this control point."""
        return self.status != "unknown"

    def as_dict(self) -> dict[str, Any]:
        """Return the row as the plain dict the sensor attributes / panel JS consume."""
        return {
            "name": self.name,
            "label": self.label,
            "desired": self.desired,
            "observed": self.observed,
            "match": self.match,
            "status": self.status,
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
    straight back to ``apply_mode`` / ``hold`` without inspecting it. Its
    concrete shape (Solis: a register tuple) lives in the driver's own module,
    never in ``contract/``.
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

    async def hold(self, mode: StorageMode, spec: Any) -> None:
        """Signal that ``mode`` is still the target; re-assert if the platform needs it.

        Called once per cycle on a holding tick — the control module's only
        statement about liveness, and deliberately its last word on the subject.
        **Whether holding costs anything is the platform's business:** a driver
        whose mode is a written setting no-ops here; one whose hold is volatile
        re-asserts, and if the cycle is too slow to beat its hardware deadman it
        runs its own :class:`..outbound.heartbeat.Heartbeat` at whatever
        cadence that deadman demands. Neither the cadence nor the existence of a keep-alive appears
        in this protocol.

        Must be safe to call on every cycle, including immediately after
        ``apply_mode``.

        Args:
            mode: The mode still being held.
            spec: Opaque per-platform spec for ``mode``.
        """
        ...

    def shutdown(self) -> None:
        """Release any driver-owned resources (timers, tasks). Idempotent.

        Called when the config entry unloads. A driver holding a
        :class:`..outbound.heartbeat.Heartbeat` stops it here so no re-assert
        write fires against a torn-down entry.
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
        """Return the opaque per-platform *effective* spec for ``mode``, or ``None``.

        The returned token is passed back to ``apply_mode`` / ``hold``
        uninspected by the generic layer.

        **Effective is part of the contract.** Every target in the returned spec
        must already be reduced to what the hardware advertises it will accept,
        because ``control_surface`` compares readbacks against these same values.
        A driver that returns an unreduced spec while its write path clamps
        produces a control point that can never verify — the write lands on the
        clamped value and the comparison expects the composed one. Drivers expose
        the unreduced composition as ``declared_spec`` for diagnostics.
        """
        ...

    def declared_spec(self, mode: StorageMode) -> Any | None:
        """Return the spec as composed from config, before capability reduction.

        Diagnostic counterpart to ``spec_for``: comparing the two shows where the
        hardware's advertised bounds fall short of what the planner asked for.
        Never handed to ``apply_mode``.
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

    def capability(self, now: datetime) -> InverterCapability:
        """Return what the hardware currently advertises it will accept, in kW.

        The planner-facing half of the capability seam: the same live bounds
        ``spec_for`` reduces against, projected into the neutral kW vocabulary so
        the DP can plan an envelope the dispatcher can actually command. Drivers
        that cannot resolve a bound report ``None`` for that field and name the
        control point in ``degraded``.

        Args:
            now: Cycle timestamp, recorded as ``resolved_at``.
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
