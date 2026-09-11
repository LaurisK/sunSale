"""Shared entity-driven control layer for non-Solis inverter drivers.

Solis realises a :class:`~..contract.models.StorageMode` as a composition of
*register* writes (see ``storage_mode_specs.py``). Every other platform sunSale
targets — Huawei, SolaX, Sungrow, GoodWe, Deye — instead exposes its control
surface as ordinary Home Assistant **entities** (a mode ``select``, a handful of
``number`` setpoints, a few ``switch`` toggles) and, for some, integration
**services** (e.g. ``huawei_solar.forcible_charge``). They differ only in *which*
entities/services a given mode touches, not in *how* they are driven.

This module factors out that "how":

  * :class:`EntityWrite` / :class:`ServiceWrite` — the two primitive actions a
    mode realisation is built from.
  * :class:`ControlPlan` — the opaque per-mode spec token an
    :class:`EntityControlDriver` hands back to ``apply_mode`` (the cross-platform
    analogue of Solis's ``StorageModeSpec``).
  * :class:`EntityActuator` — idempotent ``number`` / ``select`` / ``switch``
    writes + readbacks over a role→entity-id map, generalising the private
    helpers on :class:`..outbound.inverter.InverterController`.
  * :class:`EntityControlDriver` — a complete
    :class:`..outbound.driver.InverterControlDriver` built from a plan table, a
    mode-decode map, and a grid-power source. Each concrete platform driver is
    then a thin module that supplies *data* (the plan table + decode map +
    role names + observations), not control-flow.

Untested-by-design note
-----------------------
None of the platform drivers built on this layer have been validated against
real hardware (sunSale's author runs Solis). The actuator degrades safely on
any unmapped/unavailable role — it logs and skips rather than raising — so an
incompletely-resolved platform yields a telemetry-only install rather than a
crash. The verify loop in ``inverter_control_module.py`` is platform-neutral
(it compares :meth:`EntityControlDriver.control_surface` rows), so a mode that
only partially applies surfaces as a ``mismatch`` badge rather than silently
diverging.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from homeassistant.core import HomeAssistant

from ..contract.models import (
    UNKNOWN_LIMIT,
    InverterCapability,
    InverterModeReading,
    Limit,
    StorageMode,
)
from ..ha_state import available_state, read_float_state
from .driver import ControlRow, ControlStatus
from .heartbeat import Heartbeat
from .inverter import InverterPlatform

_LOGGER = logging.getLogger(__name__)

# Re-assert cadence for ``keepalive`` plans — a timed forcible charge/discharge
# that the inverter drops if not renewed. Kept under the shortest such timeout
# these platforms are documented to use, and well under the 5-min coordinator
# cycle. Platform detail: it appears nowhere in the neutral driver protocol.
_KEEPALIVE_INTERVAL = timedelta(seconds=180)

# Absolute tolerance (in a number entity's own unit) under which a write is a
# no-op. Mirrors ``inverter._NUMBER_WRITE_EPSILON`` but expressed per-write so a
# coarse setpoint (watts) and a fine one (amps / percent) can differ.
_DEFAULT_NUMBER_TOLERANCE = 0.5

EntityKind = Literal["number", "select", "switch"]


@dataclass(frozen=True)
class InverterContext:
    """The platform-neutral HA environment an entity-driven driver is built from.

    Decouples the entity-driven drivers from the Solis-flavoured
    :class:`..outbound.inverter.InverterController`: a driver needs only a HA
    handle, the resolved role→entity-id map, and a grid-power reader — not the
    register read/write machinery. The coordinator assembles one of these from
    whatever it has wired (today the resolver's entity map plus the controller's
    ``get_grid_power``), so ``InverterController`` no longer doubles as a generic
    handle/map holder for non-Solis platforms.

    Attributes:
        hass: Home Assistant instance for service calls and state reads.
        entity_ids: Role→entity-id map from the platform's resolver or the manual
            mapping form.
        get_grid_power: Callable returning grid power in kW (positive = import),
            ``0.0`` when unavailable.
        grid_charge_permitted: Callable returning the operator's "allow grid
            charging" choice, for drivers that pass it on to the hardware.
            ``None`` when the coordinator does not wire it.
    """

    hass: HomeAssistant
    entity_ids: Mapping[str, str]
    get_grid_power: Callable[[], float]
    grid_charge_permitted: Callable[[], bool] | None = None


@dataclass(frozen=True)
class EntityWrite:
    """One idempotent write to a controllable HA entity, with its readback.

    Attributes:
        role: Key into the driver's ``entity_ids`` map (e.g. ``work_mode_select``).
        kind: HA platform of the target entity — ``number``, ``select``, or
            ``switch``.
        value: The target value. ``float`` for ``number``, the option label
            (``str``) for ``select``, ``bool`` for ``switch``.
        label: Human-readable label rendered in the control-surface panel row.
        tolerance: For ``number`` writes, the absolute slack under which the
            write is skipped and a readback counts as matching. Ignored for
            ``select`` / ``switch``.
    """

    role: str
    kind: EntityKind
    value: Any
    label: str
    tolerance: float = _DEFAULT_NUMBER_TOLERANCE


def number_write(
    role: str, value_w: float, label: str, tolerance: float = 5.0
) -> EntityWrite:
    """Build a watt-valued ``number`` :class:`EntityWrite`.

    Factors out the per-driver closure (``export_cap`` / ``sell_power`` /
    ``eco_power`` / ``forced_power``) that every entity-driven platform repeats
    to wrap a wattage in a ``number`` setpoint write. The default ``tolerance``
    is the coarse watt slack those setpoints share (a finer one suits amp /
    percent writes, hence the per-write override on :class:`EntityWrite`).

    Args:
        role: Target ``entity_ids`` role.
        value_w: Setpoint value in watts, coerced to ``float``.
        label: Panel/log label for the write.
        tolerance: Absolute slack (watts) under which the write is a no-op.

    Returns:
        An :class:`EntityWrite` targeting a ``number`` entity.
    """
    return EntityWrite(role, "number", float(value_w), label, tolerance=tolerance)


@dataclass(frozen=True)
class ServiceWrite:
    """One integration service call used to actuate a mode (no readback).

    Service-based actions (e.g. Huawei's ``forcible_charge``) have no entity to
    read back, so they never appear as a control-surface row and are *not*
    covered by the verify loop. A plan that relies on one should pair it with an
    :class:`EntityWrite` (the working-mode select, or the resulting power
    number) so verify still has something observable to latch onto.

    Attributes:
        domain: Service domain (e.g. ``huawei_solar``).
        service: Service name (e.g. ``forcible_charge``).
        data: Static service-data payload merged into the call.
        target_role: Optional ``entity_ids`` role whose resolved entity id is
            injected as the call's ``entity_id`` (or ``device_id`` via
            ``target_key``). ``None`` for hub-level services.
        target_key: The data key under which ``target_role`` is injected.
        label: Human-readable description for logging.
    """

    domain: str
    service: str
    data: Mapping[str, Any] = field(default_factory=dict)
    target_role: str | None = None
    target_key: str = "entity_id"
    label: str = ""


@dataclass(frozen=True)
class ControlPlan:
    """The opaque per-mode spec an :class:`EntityControlDriver` realises.

    Cross-platform analogue of Solis's ``StorageModeSpec``: an ordered set of
    entity writes plus optional service calls that, applied together, drive the
    inverter into one :class:`StorageMode`. The generic driver hands this token
    back to ``apply_mode`` / ``hold`` without the
    control module ever inspecting it.

    Attributes:
        writes: Entity writes in apply order. Order matters where one setting
            gates another (mode select before its dependent setpoints).
        services: Service calls issued after the entity writes on every
            ``apply_mode`` (and re-issued on ``hold`` / the driver's
            heartbeat when ``keepalive`` is set).
        keepalive: Whether holding this mode needs a periodic re-issue (a
            volatile/timed command). ``False`` for the persistent-setting modes
            that make up most non-Solis platforms.
    """

    writes: tuple[EntityWrite, ...] = ()
    services: tuple[ServiceWrite, ...] = ()
    keepalive: bool = False


class EntityActuator:
    """Idempotent number/select/switch writer + readback over a role→entity map.

    Generalises the private ``_set_number`` / ``_set_select`` / switch helpers on
    :class:`..outbound.inverter.InverterController` so every entity-driven
    platform shares one battle-tested write path. Every read degrades to ``None``
    and every write to a logged skip when the role is unmapped or the entity is
    unavailable, so a partially-resolved platform never raises mid-cycle.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_ids: Mapping[str, str],
        log_prefix: str = "entity_control",
    ) -> None:
        """Store the HA handle and the role→entity-id map.

        Args:
            hass: Home Assistant instance for service calls and state reads.
            entity_ids: Role→entity-id map produced by the platform's entity
                resolver (or the manual mapping form).
            log_prefix: Leading tag on every debug line. The Solis
                :class:`..outbound.inverter.InverterController` passes
                ``"inverter"`` so its register-level diagnostics keep their
                historical prefix; entity-driven platforms keep the default.
        """
        self._hass = hass
        self._entity_ids = dict(entity_ids)
        self._log_prefix = log_prefix
        # role → last raw target that was warned about, so a clamp that recurs
        # every force-write cycle logs once rather than on every dispatch.
        self._clamp_warned: dict[str, float] = {}

    # --- Reads ---------------------------------------------------------- #

    def read_float(self, role: str) -> float | None:
        """Return a numeric entity's value, or ``None`` when absent/unparseable.

        Args:
            role: Role key into the entity map.

        Returns:
            Parsed float, or ``None``.
        """
        return read_float_state(self._hass, self._entity_ids.get(role, ""))

    def read_state(self, role: str) -> str | None:
        """Return a string entity's state, or ``None`` when absent/unavailable.

        Args:
            role: Role key into the entity map.

        Returns:
            The state string, or ``None``.
        """
        state = available_state(self._hass, self._entity_ids.get(role, ""))
        return state.state if state is not None else None

    def has_role(self, role: str) -> bool:
        """Return whether ``role`` maps to a non-empty entity id."""
        return bool(self._entity_ids.get(role))

    # --- Writes --------------------------------------------------------- #

    async def apply_write(self, write: EntityWrite, force: bool = False) -> None:
        """Apply one :class:`EntityWrite`, skipping no-op rewrites.

        Args:
            write: The target entity write.
            force: When ``True``, skip the readback comparison and always issue
                the service call (the verify loop's commanded-change path, where
                a stale cache must not mask a failed write).
        """
        if write.kind == "number":
            await self.set_number(write.role, float(write.value), write.tolerance, force)
        elif write.kind == "select":
            await self.set_select(write.role, str(write.value), force)
        elif write.kind == "switch":
            await self.set_switch(write.role, bool(write.value), force)
        else:  # pragma: no cover - guarded by the EntityKind Literal
            _LOGGER.warning("%s: unknown write kind %r", self._log_prefix, write.kind)

    async def set_number(
        self,
        role: str,
        target_value: float,
        tolerance: float = _DEFAULT_NUMBER_TOLERANCE,
        force: bool = False,
        renew: bool = False,
    ) -> None:
        """Write a ``number`` entity only when its readback differs beyond tolerance.

        The single idempotent number-write path shared by the entity-driven
        :meth:`apply_write` and the Solis ``InverterController`` register writes.
        Degrades to a logged skip when the role is unmapped.

        Args:
            role: Role key into the entity map.
            target_value: Desired value to set.
            tolerance: Absolute slack under which an in-range readback skips the write.
            force: When ``True``, skip the readback comparison and always issue
                the underlying ``number.set_value`` call.
            renew: When ``True``, guarantee the write reaches the *device* even
                when the entity already holds the target — see
                :meth:`_renew_nudge`. Implies ``force``. Only meaningful for a
                deadman re-arm, where the write itself (not the resulting value)
                is the point.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            _LOGGER.debug(
                "%s: number(%s) skip — no entity mapped (target=%g force=%s)",
                self._log_prefix, role, target_value, force,
            )
            return
        # Clamp *before* the readback comparison: an out-of-range target would
        # otherwise never match the (clamped) readback, so every cycle would
        # re-issue a write HA is going to reject anyway.
        target_value = self._clamp_to_entity_range(role, entity_id, target_value)
        current = self.read_float(role)
        if (
            not force
            and not renew
            and current is not None
            and abs(current - target_value) <= tolerance
        ):
            _LOGGER.debug(
                "%s: number(%s) skip — cached=%g target=%g tol=%g entity=%s",
                self._log_prefix, role, current, target_value, tolerance, entity_id,
            )
            return
        if renew and current is not None and current == float(target_value):
            nudge = self._renew_nudge(role, entity_id, target_value)
            if nudge is not None:
                _LOGGER.debug(
                    "%s: number(%s) renew — nudging via %g so the unchanged "
                    "target=%g still reaches the device (entity=%s)",
                    self._log_prefix, role, nudge, target_value, entity_id,
                )
                await self._call_service(
                    "number", "set_value",
                    {"entity_id": entity_id, "value": float(nudge)},
                    what=f"number({role}) renew-nudge",
                )
        _LOGGER.debug(
            "%s: number(%s) write — cached=%s target=%g entity=%s force=%s",
            self._log_prefix, role, current, target_value, entity_id, force,
        )
        await self._call_service(
            "number", "set_value",
            {"entity_id": entity_id, "value": float(target_value)},
            what=f"number({role})",
        )

    def _renew_nudge(
        self, role: str, entity_id: str, target_value: float
    ) -> float | None:
        """Return an in-range value differing from ``target_value``, or ``None``.

        Exists because ``number.set_value`` is not guaranteed to reach the
        hardware: an integration may short-circuit a write whose value equals
        what the entity already holds. solis_modbus does exactly this
        (``SolisNumberEntity.set_native_value`` returns early on an unchanged
        value), so sunSale's ``force`` — which only bypasses *sunSale's* own
        readback comparison — was silently dropped one layer down. For an
        ordinary setpoint that is correct and desirable; for a **deadman re-arm**
        it is fatal, because the write is the keep-alive signal and the value
        being unchanged is precisely the steady state. Writing a neighbouring
        value first makes the following target write a genuine change.

        The nudge moves one step toward zero (falling back to away-from-zero),
        so it stays inside the advertised bound that ``target_value`` already
        satisfies. A transient one-step excursion is harmless on the control
        points this is used for (an RC power setpoint and a timeout in minutes).

        Args:
            role: Role key, used to resolve the advertised bound.
            entity_id: Resolved target entity id, for the step attribute.
            target_value: The already-clamped target.

        Returns:
            A value inside the entity's bounds that differs from
            ``target_value``, or ``None`` when no such value exists (a
            pinned single-value range) — the caller then writes the target
            alone and accepts that the integration may drop it.
        """
        state = self._hass.states.get(entity_id)
        step = 1.0
        if state is not None:
            try:
                raw_step = state.attributes.get("step")
                if raw_step is not None and float(raw_step) > 0:
                    step = float(raw_step)
            except (TypeError, ValueError):
                pass
        limit = self.limit_for(role)
        toward_zero = -step if target_value > 0 else step
        for candidate in (target_value + toward_zero, target_value - toward_zero):
            if limit.known and limit.clamp(candidate) != candidate:
                continue
            return candidate
        return None

    def limit_for(self, role: str) -> Limit:
        """Return the live writable bound a ``number`` role advertises.

        **The single place sunSale reads an upstream entity's ``min``/``max``.**
        Everything that needs to know what the hardware will accept — the write
        clamp, the driver's effective spec, the capability projection the planner
        consumes — resolves it through here, so there is one answer per cycle
        rather than one per consumer.

        Resolved on every call rather than cached: solis_modbus v4.2.2 resolves
        these bounds from BMS mirror registers via a live property, so they can
        change between cycles as the BMS reports new limits.

        Args:
            role: Role key into the entity map.

        Returns:
            The resolved :class:`Limit`, or ``UNKNOWN_LIMIT`` when the role is
            unmapped, absent from the state machine, or advertises bounds that
            cannot be parsed as floats.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            return UNKNOWN_LIMIT
        state = self._hass.states.get(entity_id)
        if state is None:
            return UNKNOWN_LIMIT
        try:
            raw_low = state.attributes.get("min")
            raw_high = state.attributes.get("max")
            low = None if raw_low is None else float(raw_low)
            high = None if raw_high is None else float(raw_high)
        except (TypeError, ValueError):
            return UNKNOWN_LIMIT
        return Limit(low=low, high=high, known=True)

    def _clamp_to_entity_range(
        self, role: str, entity_id: str, target_value: float
    ) -> float:
        """Clamp a number target into the entity's advertised ``min``/``max``.

        Home Assistant's ``number.set_value`` handler rejects an out-of-range
        value with ``ServiceValidationError`` *before* the integration ever sees
        it, and that exception would abort the rest of the calling write
        sequence (on Solis: the export limit and the whole RC engage/release
        block). Integrations also change these bounds under us — solis_modbus
        v4.2.0 started honouring the declared 0–200 A ceiling on registers
        43117/43118 that its inverter-rating derivation had been discarding —
        so the ceiling is treated as authoritative rather than trusted to match
        the configured battery limits.

        This is the write path's second line of defence. Callers that compose a
        spec (the Solis driver's ``effective_spec``) already reduce their targets
        through :meth:`limit_for`, so a clamp here normally finds nothing left to
        do; it still fires for direct writers and for a bound that moved between
        composition and write.

        Args:
            role: Role key, used to resolve the bound and for logging.
            entity_id: Resolved target entity id, for the warning message.
            target_value: Desired value in the entity's own unit.

        Returns:
            ``target_value`` clamped to ``[min, max]``, or unchanged when the
            entity is missing from the state machine or advertises no usable
            bounds.
        """
        state = self._hass.states.get(entity_id)
        clamped = self.limit_for(role).clamp(target_value)
        if state is None or clamped == target_value:
            self._clamp_warned.pop(role, None)
            return target_value
        if self._clamp_warned.get(role) != target_value:
            self._clamp_warned[role] = target_value
            _LOGGER.warning(
                "%s: number(%s) target %g is outside %s's advertised range "
                "[%s, %s] — clamping to %g. The hardware will not deliver what "
                "the planner assumed; check the configured battery/inverter "
                "limits against the integration's entity bounds.",
                self._log_prefix, role, target_value, entity_id,
                state.attributes.get("min"), state.attributes.get("max"), clamped,
            )
        return clamped

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any], what: str
    ) -> None:
        """Issue one blocking service call, logging (not raising) on failure.

        A mode is realised as a *sequence* of writes, and the ones that matter
        most tend to come last (the Solis RC engage/release block). Letting a
        single rejected write raise would skip every later write — leaving the
        inverter half-configured, and in the RC case leaving a stale forced
        discharge running. The verify loop in ``inverter_control_module`` is the
        designed detector for a write that did not take: it compares the
        control surface and raises a ``mismatch`` badge.

        Args:
            domain: Service domain (``number`` / ``select`` / ``switch`` / …).
            service: Service name.
            data: Service payload, including the resolved target entity id.
            what: Short description of the write, for the error log.
        """
        try:
            await self._hass.services.async_call(
                domain, service, data, blocking=True,
            )
        except Exception:  # noqa: BLE001 — one bad write must not abort the sequence
            _LOGGER.error(
                "%s: %s — %s.%s(%s) failed; continuing with the remaining "
                "writes (the verify loop will flag the mismatch)",
                self._log_prefix, what, domain, service, data,
                exc_info=True,
            )

    async def set_select(self, role: str, option: str, force: bool = False) -> None:
        """Select an option on a ``select`` entity only when its state differs.

        Args:
            role: Role key into the entity map.
            option: Target option label.
            force: When ``True``, skip the current-state comparison and always
                issue the underlying ``select.select_option`` call.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            _LOGGER.debug(
                "%s: select(%s) skip — no entity mapped (target=%s force=%s)",
                self._log_prefix, role, option, force,
            )
            return
        current = self.read_state(role)
        if not force and current == option:
            _LOGGER.debug(
                "%s: select(%s) skip — current=%s entity=%s",
                self._log_prefix, role, current, entity_id,
            )
            return
        _LOGGER.debug(
            "%s: select(%s) write — current=%s target=%s entity=%s force=%s",
            self._log_prefix, role, current, option, entity_id, force,
        )
        await self._call_service(
            "select", "select_option",
            {"entity_id": entity_id, "option": option},
            what=f"select({role})",
        )

    async def set_switch(self, role: str, want_on: bool, force: bool = False) -> None:
        """Toggle a ``switch`` entity only when its state differs from the target.

        Args:
            role: Role key into the entity map.
            want_on: Desired switch state.
            force: When ``True``, skip the current-state comparison and always
                issue the underlying ``switch.turn_on`` / ``turn_off`` call.
        """
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            _LOGGER.debug(
                "%s: switch(%s) skip — no entity mapped (target=%s force=%s)",
                self._log_prefix, role, want_on, force,
            )
            return
        want = "on" if want_on else "off"
        if not force and self.read_state(role) == want:
            _LOGGER.debug(
                "%s: switch(%s) skip — current=%s entity=%s",
                self._log_prefix, role, want, entity_id,
            )
            return
        await self._call_service(
            "switch", "turn_on" if want_on else "turn_off",
            {"entity_id": entity_id},
            what=f"switch({role})",
        )

    async def call_service(self, action: ServiceWrite) -> None:
        """Issue one :class:`ServiceWrite`, injecting the resolved target id.

        Args:
            action: The service call to issue. Skipped (logged) when its
                ``target_role`` is mapped to an empty entity id.
        """
        data: dict[str, Any] = dict(action.data)
        if action.target_role is not None:
            entity_id = self._entity_ids.get(action.target_role, "")
            if not entity_id:
                _LOGGER.debug(
                    "%s: service(%s.%s) skip — target role %s unmapped",
                    self._log_prefix, action.domain, action.service, action.target_role,
                )
                return
            data[action.target_key] = entity_id
        await self._call_service(
            action.domain, action.service, data,
            what=f"service({action.domain}.{action.service})",
        )


class EntityControlDriver:
    """An entity-driven :class:`..outbound.driver.InverterControlDriver`.

    Realises StorageModes by applying :class:`ControlPlan` tokens through an
    :class:`EntityActuator`, decodes the observed mode from a single mode-state
    entity, and renders the per-write intended-vs-observed control surface the
    panel + verify loop consume. Concrete platforms supply only the plan table,
    the decode map, the mode-state role, and a grid-power source — no
    control-flow.

    Conforms structurally to :class:`..outbound.driver.InverterControlDriver`.
    """

    def __init__(
        self,
        platform: InverterPlatform,
        actuator: EntityActuator,
        grid_power: Callable[[], float],
        plans: Mapping[StorageMode, ControlPlan],
        mode_role: str,
        decode_map: Mapping[str, StorageMode],
        mode_label: str = "Operating mode",
        sub_decode_role: str | None = None,
        sub_decode_map: Mapping[str, StorageMode] | None = None,
        hass: HomeAssistant | None = None,
    ) -> None:
        """Wire the generic driver to one platform's data.

        Args:
            platform: The configured platform (for diagnostics/logging).
            actuator: The shared entity write/read helper.
            grid_power: Callable returning grid power in kW (positive = import);
                typically ``InverterController.get_grid_power``.
            plans: StorageMode → :class:`ControlPlan` table for this deployment.
            mode_role: Role of the entity whose state decodes to a StorageMode
                (usually the platform's working-mode ``select``).
            decode_map: Maps that entity's state string → StorageMode.
            mode_label: Panel label for the mode-state row.
            sub_decode_role: Optional role of a secondary entity that
                disambiguates the forced/manual sub-state when the primary
                ``mode_role`` reads a value left out of ``decode_map`` (e.g. a
                "Manual Mode" / "Forced mode" umbrella whose direction lives in a
                separate ``select``). ``None`` for platforms whose mode select
                fully determines the mode, or that expose no readable sub-state.
            sub_decode_map: Maps the sub-state entity's string → StorageMode.
                Consulted only when the primary decode is ``UNKNOWN`` (see
                :meth:`decode_observed`).
            hass: Home Assistant instance, used only to run this driver's
                keep-alive heartbeat for ``keepalive`` plans. ``None`` leaves
                the driver dependent on ``hold`` at the cycle boundary.
        """
        self._platform = platform
        self._actuator = actuator
        self._grid_power = grid_power
        self._plans = dict(plans)
        self._mode_role = mode_role
        self._decode_map = dict(decode_map)
        self._mode_label = mode_label
        self._sub_decode_role = sub_decode_role
        self._sub_decode_map = dict(sub_decode_map or {})
        # Serialises this driver's writes against its own heartbeat, which
        # fires outside the control module's dispatch lock.
        self._write_lock = asyncio.Lock()
        self._held_plan: ControlPlan | None = None
        self._keepalive = Heartbeat(
            hass, _KEEPALIVE_INTERVAL, self._on_keepalive_beat,
            f"{platform.value} forced-mode",
        )

    @classmethod
    def from_context(
        cls,
        context: InverterContext,
        platform: InverterPlatform,
        plans: Mapping[StorageMode, ControlPlan],
        mode_role: str,
        decode_map: Mapping[str, StorageMode],
        mode_label: str = "Operating mode",
        sub_decode_role: str | None = None,
        sub_decode_map: Mapping[str, StorageMode] | None = None,
    ) -> EntityControlDriver:
        """Build a driver from a neutral context, owning actuator construction.

        Absorbs the boilerplate every platform's ``make_*_driver`` repeated —
        constructing the :class:`EntityActuator` from the context and threading
        ``get_grid_power`` — so a concrete driver supplies only its *data* (plan
        table + decode maps + role names).

        Args:
            context: HA handle, role→entity-id map, and grid-power reader.
            platform: The configured platform.
            plans: StorageMode → :class:`ControlPlan` table for this deployment.
            mode_role: Role of the mode-state entity (see :meth:`__init__`).
            decode_map: Maps that entity's state string → StorageMode.
            mode_label: Panel label for the mode-state row.
            sub_decode_role: Optional secondary mode-state role (see :meth:`__init__`).
            sub_decode_map: Maps the sub-state entity's string → StorageMode.

        Returns:
            A configured :class:`EntityControlDriver`.
        """
        return cls(
            platform=platform,
            actuator=EntityActuator(context.hass, context.entity_ids),
            grid_power=context.get_grid_power,
            plans=plans,
            mode_role=mode_role,
            decode_map=decode_map,
            mode_label=mode_label,
            sub_decode_role=sub_decode_role,
            sub_decode_map=sub_decode_map,
            hass=context.hass,
        )

    # --- Role contract -------------------------------------------------- #

    def referenced_roles(self) -> frozenset[str]:
        """Return every ``entity_ids`` role this driver reads or writes.

        The union of all plan entity-write roles, plan service target roles, the
        mode-decode role, and the sub-decode role (if any). It is the *demand*
        side of the role contract: a platform's resolver / profile must be able
        to supply every one of these, or the corresponding write degrades to a
        logged skip. Cross-checked against the profile's declared roles by
        :func:`..inbound.platform_profiles.unbacked_roles` so a renamed/typo'd
        role surfaces as a test failure rather than a silent no-op.

        Returns:
            The set of role keys the driver depends on.
        """
        roles: set[str] = {self._mode_role}
        if self._sub_decode_role:
            roles.add(self._sub_decode_role)
        for plan in self._plans.values():
            roles.update(write.role for write in plan.writes)
            roles.update(
                action.target_role for action in plan.services
                if action.target_role is not None
            )
        return frozenset(roles)

    # --- Spec table ----------------------------------------------------- #

    def spec_for(self, mode: StorageMode) -> ControlPlan | None:
        """Return the *effective* plan for ``mode``, or ``None`` if unsupported.

        See :meth:`effective_spec`; :meth:`declared_spec` returns the unreduced
        plan.
        """
        return self.effective_spec(mode)

    def declared_spec(self, mode: StorageMode) -> ControlPlan | None:
        """Return the plan as composed from config, before capability reduction."""
        return self._plans.get(mode)

    def effective_spec(self, mode: StorageMode) -> ControlPlan | None:
        """Return ``mode``'s plan with each ``number`` write reduced to its live bound.

        ``set_number`` clamps a target into the entity's advertised range, so a
        plan value above that range is written as the clamped value. Reducing the
        plan here keeps the control surface (and therefore the verify loop)
        comparing readbacks against what was actually written — without it, any
        such write reports a permanent mismatch.

        ``select`` / ``switch`` writes and service actions pass through unchanged:
        they carry no numeric bound.

        Args:
            mode: Mode whose plan to resolve.

        Returns:
            The reduced :class:`ControlPlan`, or ``None`` when ``mode`` has no
            plan on this platform.
        """
        plan = self._plans.get(mode)
        if plan is None:
            return None
        return replace(
            plan,
            writes=tuple(self._reduce_write(write) for write in plan.writes),
        )

    def _reduce_write(self, write: EntityWrite) -> EntityWrite:
        """Return ``write`` with a ``number`` target clamped into its live bound."""
        if write.kind != "number":
            return write
        clamped = self._actuator.limit_for(write.role).clamp(float(write.value))
        if clamped == float(write.value):
            return write
        return replace(write, value=clamped)

    def shutdown(self) -> None:
        """Stop the keep-alive heartbeat so no re-issue survives an unload."""
        self._keepalive.stop()

    async def _on_keepalive_beat(self) -> None:
        """Re-issue the held plan's actuation on the heartbeat."""
        async with self._write_lock:
            plan = self._held_plan
            if plan is None or not plan.keepalive:
                return
            await self._reissue(plan)

    def capability(self, now: datetime) -> InverterCapability:
        """Return an unconstrained capability — entity platforms don't map roles to legs yet.

        Every field is ``None`` (nothing resolved), which leaves the planner on
        its configured caps: exactly the behaviour these platforms had before the
        capability seam existed, so nothing changes for them.

        The reduction that *does* apply on these platforms is per-write, in
        :meth:`effective_spec` — that is what keeps their verify loop honest. What
        is missing here is the platform-specific knowledge of which role bounds
        which physical leg (battery charge vs discharge vs export), and unlike
        Solis there is no uniform role vocabulary across Huawei / SolaX / Sungrow
        / GoodWe / Deye to derive it from. Returning ``None`` is deliberate:
        inventing a mapping for six drivers that have never been hardware-verified
        would feed the DP numbers nobody has checked. Per-platform wiring is the
        extension point — declare the role for each leg and project it here.

        Args:
            now: Cycle timestamp recorded as ``resolved_at``.

        Returns:
            An :class:`InverterCapability` with no resolved bounds.
        """
        return InverterCapability(
            max_battery_charge_kw=None,
            max_battery_discharge_kw=None,
            max_export_kw=None,
            max_grid_discharge_kw=None,
            resolved_at=now,
        )

    # --- Write side ----------------------------------------------------- #

    async def apply_mode(
        self, mode: StorageMode, spec: ControlPlan, force: bool = False
    ) -> None:
        """Drive the inverter to ``mode`` via the plan's writes then services.

        Args:
            mode: Target mode (logging only — the concrete targets live in ``spec``).
            spec: The plan to apply.
            force: Skip the cached-readback comparison on each entity write.
        """
        _LOGGER.debug(
            "entity_control[%s]: apply_mode(%s, force=%s) — %d writes, %d services",
            self._platform.value, mode.value, force, len(spec.writes), len(spec.services),
        )
        async with self._write_lock:
            for write in spec.writes:
                await self._actuator.apply_write(write, force=force)
            for action in spec.services:
                await self._actuator.call_service(action)
            self._held_plan = spec
            # Phase the heartbeat to this write; a persistent plan stops one a
            # prior timed mode left running.
            if spec.keepalive:
                self._keepalive.start()
            else:
                self._keepalive.stop()

    async def hold(self, mode: StorageMode, spec: ControlPlan) -> None:
        """Re-issue a timed mode's actuation at the cycle boundary; no-op otherwise.

        Only plans flagged ``keepalive`` re-run — their service calls (and
        forced entity writes) are reissued to re-arm a timed command before it
        lapses. Persistent-setting platforms hold their mode in non-volatile
        config and need nothing here.

        Args:
            mode: The mode still being held (logging only).
            spec: The held plan.
        """
        async with self._write_lock:
            self._held_plan = spec
            if not spec.keepalive:
                return
            await self._reissue(spec)

    async def _reissue(self, plan: ControlPlan) -> None:
        """Force-apply every write and service call in ``plan``.

        The re-arm body shared by ``hold`` and the heartbeat. Callers hold
        ``_write_lock``.
        """
        for write in plan.writes:
            await self._actuator.apply_write(write, force=True)
        for action in plan.services:
            await self._actuator.call_service(action)

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing); 0.0 when unavailable."""
        return self._grid_power()

    # --- Semantics: control surface + decode ---------------------------- #

    def decode_observed(self) -> StorageMode:
        """Decode the inverter's current StorageMode from the mode-state entity.

        Reads the primary ``mode_role`` first. When it maps to a named mode that
        wins. When it reads a value the ``decode_map`` deliberately omits — the
        platform's "Manual Mode" / "Forced mode" umbrella, whose direction is not
        in the primary select — and a ``sub_decode_role`` was supplied, the
        secondary entity is consulted to recover GridCharge vs Discharge vs
        StandBy. The sub-state is only trusted when the primary state was read but
        unmapped (not when it is missing), so a stale sub-select can never
        manufacture a forced mode the primary select contradicts.

        Returns:
            Best-fit StorageMode; ``UNKNOWN`` when neither the primary nor (where
            applicable) the secondary entity maps to a mode.
        """
        state = self._actuator.read_state(self._mode_role)
        if state is None:
            return StorageMode.UNKNOWN
        base = self._decode_map.get(state, StorageMode.UNKNOWN)
        if base is StorageMode.UNKNOWN and self._sub_decode_role:
            sub_state = self._actuator.read_state(self._sub_decode_role)
            if sub_state is not None:
                return self._sub_decode_map.get(sub_state, StorageMode.UNKNOWN)
        return base

    def observe(self, now: datetime) -> InverterModeReading:
        """Read the mode-state entity once and package an InverterModeReading.

        Entity-driven platforms expose mode as a string, not an integer register
        code, so ``raw_state`` and the Solis-specific numeric fields are ``None``;
        only the decoded ``mode`` is populated.

        Args:
            now: Cycle timestamp recorded as the reading's timestamp.

        Returns:
            The current observed state.
        """
        return InverterModeReading(
            timestamp=now,
            raw_state=None,
            mode=self.decode_observed(),
            charge_a=None,
            discharge_a=None,
            rc_setpoint_w=None,
        )

    def observed_raw_state(self) -> int | None:
        """Return ``None`` — these platforms have no integer raw mode-state code."""
        return None

    def control_surface(self, commanded: StorageMode | None) -> list[ControlRow]:
        """Return intended-vs-observed rows for the commanded plan's writes.

        Builds one row per :class:`EntityWrite` in the commanded plan, pairing
        the target value with the live readback. When no mode is commanded (or it
        has no plan), returns a single observed-only row for the mode-state
        entity so the panel still shows where the inverter actually is.

        Service-based actions produce no row (no readback); see
        :class:`ServiceWrite`.

        Args:
            commanded: The mode whose plan targets to compare against, or ``None``.

        Returns:
            One :class:`ControlRow` per controllable write, in apply order.
        """
        plan = self.effective_spec(commanded) if commanded is not None else None
        if plan is None:
            observed = self._actuator.read_state(self._mode_role)
            return [ControlRow(
                name=self._mode_role, label=self._mode_label,
                desired=None, observed=observed, status="no_target",
            )]
        return [self._row_for(write) for write in plan.writes]

    def _row_for(self, write: EntityWrite) -> ControlRow:
        """Build one control-surface row comparing a write's target to its readback.

        Args:
            write: The entity write to render.

        Returns:
            A :class:`ControlRow` whose ``status`` is ``unknown`` when the
            readback is missing (an upstream comms gap, not drift) and
            ``match`` / ``mismatch`` otherwise.
        """
        if write.kind == "number":
            observed: Any = self._actuator.read_float(write.role)
            desired: Any = float(write.value)
            if observed is None:
                return ControlRow(write.role, write.label, desired, None, "unknown")
            return ControlRow(
                write.role, write.label, desired, observed,
                "match" if abs(observed - desired) <= write.tolerance else "mismatch",
            )
        if write.kind == "switch":
            observed = self._actuator.read_state(write.role)
            want = "on" if bool(write.value) else "off"
            return ControlRow(
                write.role, write.label, want, observed,
                self._compare_state(observed, want),
            )
        # select
        observed = self._actuator.read_state(write.role)
        desired = str(write.value)
        return ControlRow(
            write.role, write.label, desired, observed,
            self._compare_state(observed, desired),
        )

    @staticmethod
    def _compare_state(observed: str | None, desired: str) -> ControlStatus:
        """Return the status of a string-valued control point against its target."""
        if observed is None:
            return "unknown"
        return "match" if observed == desired else "mismatch"
