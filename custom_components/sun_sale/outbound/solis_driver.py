"""Solis inverter control driver — the platform-specific control surface.

This is the concrete :class:`..outbound.driver.InverterControlDriver` for Solis.
It owns everything the generic control module must *not* know:

  * the StorageMode → register spec table (``build_specs``),
  * the always-populated intended-vs-observed register comparison the panel
    renders (``control_surface``), and
  * decoding the live register readbacks back to a ``StorageMode``
    (``decode_observed``).

It wraps an :class:`..outbound.inverter.InverterController` for the actual
register reads / writes (so the write idempotency, RC sequencing and unit
handling stay in one place) and adds the Solis-specific *semantics* on top. The
control module orchestrates verify / reconcile against this surface without
referencing a single register number — so a new platform is a sibling driver
selected in :func:`..outbound.driver_factory.make_inverter_driver`, not a fork
of the control module.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from ..contract.models import (
    BatteryConfig,
    InverterCapability,
    InverterModeReading,
    Limit,
    StorageMode,
)
from .driver import ControlRow, ControlStatus
from .heartbeat import Heartbeat
from .inverter import (
    _NUMBER_WRITE_EPSILON,
    RC_ADJUSTMENT_AC_PORT_VALUE,
    InverterController,
)
from .storage_mode_specs import StorageModeSpec, build_specs, decode_mode

_LOGGER = logging.getLogger(__name__)

# RC keep-alive cadence. The Solis RC active-power function expires ~5 min after
# the last engaging write — register 43282's 30-min request does not latch on S6
# firmware (Pho3niX90/solis_modbus#352), so the inverter uses its ~5-min default.
# A 2026-08-04 production capture measured grid export collapsing at exactly
# 5 min 08 s into a held discharge, which the 5-min coordinator cycle is too slow
# to beat — hence a driver-owned heartbeat at 3 min, ~2 min of margin under the
# expiry. This cadence is Solis's business and appears nowhere in the neutral
# driver protocol.
_RC_KEEPALIVE_INTERVAL = timedelta(seconds=180)

# Spec field → actuator role. The mapping that lets a composed StorageModeSpec be
# reduced through the live entity bounds: each writable spec field names the role
# whose advertised min/max governs it. ``reg_43110_value`` is deliberately absent
# — it is a switch-bit composition, not a bounded number.
_ROLE_CHARGE_CURRENT = "battery_max_charge_current"
_ROLE_DISCHARGE_CURRENT = "battery_max_discharge_current"
_ROLE_EXPORT_LIMIT = "backflow_power"
_ROLE_RC_SETPOINT = "rc_setpoint"

# Readbacks captured by ``diagnostic_snapshot`` — everything that describes what
# the inverter is doing, including the undocumented 33122 working mode.
_DIAGNOSTIC_ROLES: tuple[str, ...] = (
    "operating_mode",
    "dispatch_running_status",
    "storage_control_readback",
    "battery_max_charge_current",
    "battery_max_discharge_current",
    "backflow_power",
    "rc_setpoint",
    "rc_grid_adjustment_select",
    "battery_soc",
    "battery_power_signed",
    "grid_power",
    "pv_power",
)


def _amps_to_kw(amps: float | None, voltage_v: float) -> float | None:
    """Convert a DC current ceiling to kW at ``voltage_v`` (``None`` passes through).

    Args:
        amps: Current ceiling in amps, or ``None`` when unbounded/unresolved.
        voltage_v: DC bus voltage; falls back to 48 V when non-positive.

    Returns:
        The equivalent power ceiling in kW, or ``None``.
    """
    if amps is None:
        return None
    v = voltage_v if voltage_v > 0 else 48.0
    return (amps * v) / 1000.0


def _watts_to_kw(watts: float | None) -> float | None:
    """Return ``watts`` as kW, or ``None`` when unbounded/unresolved."""
    return None if watts is None else watts / 1000.0


def _binding(*caps: float | None) -> float | None:
    """Return the smallest non-``None`` cap, or ``None`` when none constrain.

    Args:
        *caps: Candidate ceilings in a common unit; ``None`` entries do not
            constrain.

    Returns:
        The binding (smallest) ceiling, or ``None`` when every candidate is
        ``None``.
    """
    known = [c for c in caps if c is not None]
    return min(known) if known else None


class SolisDriver:
    """Solis control driver: spec composition, control surface, and decoding.

    Implements :class:`..outbound.driver.InverterControlDriver` structurally.
    Register reads / writes are delegated to the wrapped
    :class:`InverterController`; the spec table and register layout knowledge
    live here.
    """

    def __init__(
        self,
        inverter: InverterController,
        battery_config: BatteryConfig,
        export_limit_w: int,
        inverter_max_power_w: int,
        hass: HomeAssistant | None = None,
    ) -> None:
        """Build the deployment's spec table and wrap the inverter.

        Args:
            inverter: The register read / write source.
            battery_config: Battery limits — used to derive charge/discharge amps.
            export_limit_w: Backflow / export cap for SELL / STORE modes.
            inverter_max_power_w: Rated AC output used as the RC active-power
                magnitude when discharging to grid.
            hass: Home Assistant instance, used only to run this driver's RC
                heartbeat. ``None`` (unit tests, telemetry-only platforms)
                leaves the driver dependent on ``hold`` alone — correct for a
                platform with no volatile hold, and degraded but harmless
                otherwise.
        """
        self._inverter = inverter
        self._export_limit_w = export_limit_w
        # Serialises this driver's own writes: ``apply_mode`` spans several
        # awaits, and the heartbeat fires from a timer outside the control
        # module's dispatch lock, so without this a beat could interleave with
        # a mode change and re-arm the RC function the change just released.
        self._write_lock = asyncio.Lock()
        # The mode currently held, captured on each ``apply_mode`` so a beat
        # re-arms what is actually commanded rather than a stale spec.
        self._held_spec: StorageModeSpec | None = None
        self._rc_heartbeat = Heartbeat(
            hass, _RC_KEEPALIVE_INTERVAL, self._on_rc_beat, "solis RC",
        )
        # Retained for capability(): the amps↔kW conversion the specs were
        # composed with, so the projection back to kW is the exact inverse.
        self._nominal_voltage_v = battery_config.nominal_voltage_v
        self._specs: dict[StorageMode, StorageModeSpec] = build_specs(
            battery_config,
            export_max_w=export_limit_w,
            inverter_max_power_w=inverter_max_power_w,
        )

    @property
    def control_path(self) -> str:
        """Return which write path drives the forced modes — a panel diagnostic.

        ``rc`` means the Remote-Control register sequence (43132 / 43282 /
        43128) with its driver-owned heartbeat. See
        :class:`..outbound.solis_dispatch_driver.SolisDispatchDriver` for the
        alternative and the gate that chooses between them.
        """
        return "rc"

    # --- Spec table ----------------------------------------------------- #

    @property
    def specs(self) -> dict[StorageMode, StorageModeSpec]:
        """Return the full StorageMode → spec table for this deployment."""
        return self._specs

    @property
    def export_limit_w(self) -> int:
        """Return the configured deployment-wide export-power cap in watts."""
        return self._export_limit_w

    def spec_for(self, mode: StorageMode) -> StorageModeSpec | None:
        """Return the *effective* spec for ``mode``, or ``None`` when unsupported.

        Effective = every target reduced to what the hardware currently
        advertises it will accept. This is the spec the control module hands to
        ``apply_mode`` and the one ``control_surface`` compares readbacks
        against, so the value written and the value verified are always the same
        number. See :meth:`effective_spec`; :meth:`declared_spec` returns the
        unreduced composition.
        """
        return self.effective_spec(mode)

    def declared_spec(self, mode: StorageMode) -> StorageModeSpec | None:
        """Return the spec as composed from config, before capability reduction.

        The planner's intent, useful for diagnosing a capability shortfall
        (declared vs effective). Not what gets written.
        """
        return self._specs.get(mode)

    def effective_spec(self, mode: StorageMode) -> StorageModeSpec | None:
        """Return ``mode``'s spec with each target reduced to the live bound.

        Resolves every writable control point's :class:`Limit` through the
        actuator and clamps the composed target into it. Without this the write
        path clamps (``EntityActuator._clamp_to_entity_range``) while the verify
        path compares against the unclamped composition, so any target above an
        upstream bound can never verify — which is exactly what solis_modbus
        v4.2.0's 0–200 A ceiling on 43117/43118 produced against a 292.97 A
        composed target.

        The 43110 bitmask is not reduced: it is a switch composition, not a
        bounded number.

        Args:
            mode: Mode whose spec to resolve.

        Returns:
            The reduced :class:`StorageModeSpec`, or ``None`` when ``mode`` has
            no spec on this platform.
        """
        spec = self._specs.get(mode)
        if spec is None:
            return None
        return StorageModeSpec(
            reg_43110_value=spec.reg_43110_value,
            export_limit_w=self._reduce_int(_ROLE_EXPORT_LIMIT, spec.export_limit_w),
            charge_a=self._reduce_float(_ROLE_CHARGE_CURRENT, spec.charge_a),
            discharge_a=self._reduce_float(_ROLE_DISCHARGE_CURRENT, spec.discharge_a),
            rc_setpoint_w=self._reduce_int(_ROLE_RC_SETPOINT, spec.rc_setpoint_w) or 0,
        )

    def _reduce_float(self, role: str, target: float | None) -> float | None:
        """Return ``target`` clamped into ``role``'s live bound (``None`` passes through)."""
        if target is None:
            return None
        return self._inverter.limit_for(role).clamp(target)

    def _reduce_int(self, role: str, target: int | None) -> int | None:
        """Return ``target`` clamped into ``role``'s live bound, kept integral."""
        if target is None:
            return None
        return int(round(self._inverter.limit_for(role).clamp(float(target))))

    def limit_for(self, role: str) -> Limit:
        """Return the live writable bound for one actuator ``role``.

        Exposed so :meth:`capability` and the panel can reason about the same
        bounds the write path uses.
        """
        return self._inverter.limit_for(role)

    def capability(self, now: datetime) -> InverterCapability:
        """Project the live control-point bounds into the planner's kW vocabulary.

        The read path that lets the DP plan against the hardware's real envelope
        instead of the configured one. Each leg resolves from the same
        :meth:`limit_for` the write path clamps to, so "what we plan" and "what
        we can command" cannot drift apart.

        Discharge-to-grid is bounded by **three independent** control points and
        takes the binding one — the battery's DC current limit converted at the
        configured bus voltage, the RC active-power setpoint, and the export cap.
        On the reference install those are 200 A x 51.2 V = 10.24 kW,
        +/-10,000 W, and 20,000 W respectively, so 10.0 kW binds. Lifting any one
        alone would change nothing.

        Args:
            now: Cycle timestamp recorded as ``resolved_at``.

        Returns:
            The resolved :class:`InverterCapability`. Fields are ``None`` where
            no bound could be resolved; ``degraded`` names those control points.
        """
        return self._capability(now, include_rc_leg=True)

    def capability_without_rc_leg(self, now: datetime) -> InverterCapability:
        """Return the capability with the RC active-power leg dropped.

        For a caller that commands power through a path which does **not** go
        through the RC ``number`` entity — the Remote Dispatch service writes
        raw registers — that entity's advertised ±10 kW is not a bound on
        anything, and including it would under-report the envelope to the
        planner. The physical legs (battery current, export cap) still apply.

        Args:
            now: Cycle timestamp recorded as ``resolved_at``.

        Returns:
            The resolved :class:`InverterCapability` without the RC leg.
        """
        return self._capability(now, include_rc_leg=False)

    def _capability(
        self, now: datetime, include_rc_leg: bool
    ) -> InverterCapability:
        """Resolve every leg, optionally excluding the RC active-power ceiling.

        Args:
            now: Cycle timestamp recorded as ``resolved_at``.
            include_rc_leg: Whether the RC setpoint's advertised bound
                constrains grid discharge (see :meth:`capability_without_rc_leg`).

        Returns:
            The resolved :class:`InverterCapability`.
        """
        v = self._nominal_voltage_v
        charge = self.limit_for(_ROLE_CHARGE_CURRENT)
        discharge = self.limit_for(_ROLE_DISCHARGE_CURRENT)
        export = self.limit_for(_ROLE_EXPORT_LIMIT)
        rc = self.limit_for(_ROLE_RC_SETPOINT)

        charge_kw = _amps_to_kw(charge.high, v)
        discharge_kw = _amps_to_kw(discharge.high, v)
        export_kw = _watts_to_kw(export.high)
        # The RC setpoint is signed: export uses the positive side, so its
        # magnitude ceiling is ``high``. A limit with no positive side cannot
        # drive export at all.
        rc_kw = _watts_to_kw(rc.high)

        legs = [
            (_ROLE_CHARGE_CURRENT, charge),
            (_ROLE_DISCHARGE_CURRENT, discharge),
            (_ROLE_EXPORT_LIMIT, export),
        ]
        if include_rc_leg:
            legs.append((_ROLE_RC_SETPOINT, rc))
        degraded = tuple(role for role, limit in legs if not limit.known)
        grid_discharge_kw = _binding(
            discharge_kw, rc_kw if include_rc_leg else None, export_kw,
        )
        return InverterCapability(
            max_battery_charge_kw=charge_kw,
            max_battery_discharge_kw=discharge_kw,
            max_export_kw=export_kw,
            max_grid_discharge_kw=grid_discharge_kw,
            resolved_at=now,
            degraded=degraded,
        )

    @staticmethod
    def _is_rc_backed(spec: StorageModeSpec) -> bool:
        """Return whether ``spec`` drives the volatile RC function.

        RC-backed modes (non-zero active-power setpoint) expire inverter-side
        and must be re-armed; passive modes hold in written registers and do
        not. Private: the control module never asks.
        """
        return spec.rc_setpoint_w != 0

    # --- Write side + readbacks (delegated to the inverter) ------------- #

    async def apply_mode(
        self, mode: StorageMode, spec: StorageModeSpec, force: bool = False
    ) -> None:
        """Drive the inverter to ``mode`` and (re)phase the RC heartbeat.

        An RC-backed target gets a fresh heartbeat anchored to this write; any
        other target stops the heartbeat a prior RC mode left running — the RC
        function is released by ``apply_mode`` itself, so re-arming it
        afterwards would resurrect a mode nobody commanded.

        Args:
            mode: Target storage mode.
            spec: Effective register spec for ``mode``.
            force: Skip the per-register readback comparison.
        """
        async with self._write_lock:
            await self._inverter.apply_mode(mode, spec, force=force)
            self._held_spec = spec
            if self._is_rc_backed(spec):
                self._rc_heartbeat.start()
            else:
                self._rc_heartbeat.stop()

    async def hold(self, mode: StorageMode, spec: StorageModeSpec) -> None:
        """Re-assert the RC function at the cycle boundary (no-op for passive modes).

        The secondary re-arm: the heartbeat above is what actually beats the
        ~5-min expiry, but a cycle-boundary refresh costs nothing on RAM-only
        registers and re-anchors the hold after any tick that found the mode
        unchanged.

        Deliberately unconditional on the control module's verify verdict — a
        deadman must keep running exactly when the loop is least sure of
        itself, and an RC selector reading back wrong *is* the expiry this
        repairs.

        Args:
            mode: The mode still being held (logging only).
            spec: Effective spec for ``mode``.
        """
        async with self._write_lock:
            self._held_spec = spec
            await self._inverter.refresh_rc(spec)

    def shutdown(self) -> None:
        """Stop the RC heartbeat so no re-arm fires after a config-entry unload."""
        self._rc_heartbeat.stop()

    async def _on_rc_beat(self) -> None:
        """Re-arm the RC function on the heartbeat, using the mode last applied."""
        async with self._write_lock:
            spec = self._held_spec
            if spec is None or not self._is_rc_backed(spec):
                return
            _LOGGER.debug("solis_driver: RC heartbeat re-arming setpoint=%s W",
                          spec.rc_setpoint_w)
            await self._inverter.refresh_rc(spec)

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing)."""
        return self._inverter.get_grid_power()

    def diagnostic_snapshot(self) -> dict[str, str | None]:
        """Return the raw readbacks that describe what the inverter is doing.

        Captured by the battery-export guard at a trip, before anything is
        written, so an undocumented state (e.g. 33122 = 4096) is recorded as the
        integration published it.
        """
        return self._inverter.raw_states(_DIAGNOSTIC_ROLES)

    def observed_raw_state(self) -> int | None:
        """Return the live register 43110 readback (the ``raw_state`` code), or ``None``.

        The verify loop's lightweight "what raw state did we just read" probe.
        The individual Solis register readbacks (charge/discharge current, RC
        setpoint/selector, backflow) stay on the wrapped ``InverterController``
        and are consumed internally by ``control_surface`` / ``observe`` /
        ``decode_observed`` — they are not part of the platform-neutral driver
        interface.
        """
        return self._inverter.get_storage_control_word()

    # --- Semantics: control surface + decode ---------------------------- #

    def decode_observed(self) -> StorageMode:
        """Decode the inverter's current StorageMode from live register readbacks."""
        return decode_mode(
            self._inverter.get_storage_control_word(),
            self._inverter.get_charge_current_a(),
            self._inverter.get_discharge_current_a(),
            self._inverter.get_rc_setpoint_w(),
            self._inverter.get_backflow_power_w(),
            self._inverter.is_rc_engaged(),
        )

    def observe(self, now: datetime) -> InverterModeReading:
        """Read every mode-defining register once and package an InverterModeReading.

        Reads the registers a single time and decodes them from those exact
        values, so the reading's raw fields and its decoded ``mode`` are always
        consistent (no re-read between capture and decode). This is the
        per-cycle observation consumed by the mode-history log and the
        diagnostic sensor.

        Args:
            now: Cycle timestamp recorded as ``InverterModeReading.timestamp``.

        Returns:
            The current observed state; unreadable fields are ``None`` and the
            decoded mode collapses to ``StorageMode.UNKNOWN``.
        """
        reg = self._inverter.get_storage_control_word()
        charge_a = self._inverter.get_charge_current_a()
        discharge_a = self._inverter.get_discharge_current_a()
        rc_w = self._inverter.get_rc_setpoint_w()
        backflow_w = self._inverter.get_backflow_power_w()
        rc_engaged = self._inverter.is_rc_engaged()
        return InverterModeReading(
            timestamp=now,
            raw_state=reg,
            mode=decode_mode(reg, charge_a, discharge_a, rc_w, backflow_w, rc_engaged),
            charge_a=charge_a,
            discharge_a=discharge_a,
            rc_setpoint_w=rc_w,
            backflow_power_w=backflow_w,
            rc_engaged=rc_engaged,
        )

    def control_surface(self, commanded: StorageMode | None) -> list[ControlRow]:
        """Build the always-populated canonical register comparison for the panel.

        Reads the live value of every canonical register and pairs it with
        ``commanded``'s spec target — or ``None`` (a no-target row) when the
        mode leaves that register at the hardware default, or when no mode is
        commanded. This lets the panel show observed register values in every
        case, including when the observed bitmask maps to no named mode.

        Args:
            commanded: The last-commanded mode, or ``None`` for an
                observed-only readout.

        Targets come from :meth:`effective_spec`, so a row compares the readback
        against the value ``apply_mode`` actually wrote rather than the unreduced
        composition — otherwise a target above an upstream bound reports a
        permanent mismatch the control loop can never resolve.

        Returns:
            Canonical register rows in ``apply_mode`` write order: ``reg_43110``,
            ``charge_a``, ``discharge_a``, ``export_limit_w``, an optional
            ``rc_enable`` (only when ``commanded`` is RC-backed), and
            ``rc_setpoint_w``.
        """
        return self.control_surface_for_spec(
            self.effective_spec(commanded) if commanded is not None else None,
        )

    def control_surface_for_spec(
        self, spec: StorageModeSpec | None
    ) -> list[ControlRow]:
        """Build the register comparison against an explicitly supplied spec.

        The comparison must describe what was *written*, not what the mode's
        table says in the abstract. A wrapping driver that drives part of a mode
        through some other mechanism applies a modified spec (see
        :class:`..outbound.solis_dispatch_driver.SolisDispatchDriver`, which
        zeroes the RC setpoint because Remote Dispatch holds the power target
        instead) and must compare against that same modified spec — otherwise
        the untouched control points report a mismatch nothing will ever
        resolve.

        Args:
            spec: The spec actually applied, or ``None`` for an observed-only
                readout.

        Returns:
            Canonical register rows in ``apply_mode`` write order.
        """
        rows: list[ControlRow] = [
            self._register_row(
                "reg_43110", "Storage control word (43110)",
                spec.reg_43110_value if spec else None,
                self._inverter.get_storage_control_word(),
                exact=True,
            ),
            self._register_row(
                "charge_a", "Charge current (A)",
                spec.charge_a if spec else None,
                self._inverter.get_charge_current_a(),
            ),
            self._register_row(
                "discharge_a", "Discharge current (A)",
                spec.discharge_a if spec else None,
                self._inverter.get_discharge_current_a(),
            ),
            self._register_row(
                "export_limit_w", "Export limit (W)",
                spec.export_limit_w if spec else None,
                self._inverter.get_backflow_power_w(),
            ),
        ]
        # The RC function selector (43132) is only meaningful when the commanded
        # mode drives the RC setpoint — the setpoint is inert while it reads
        # anything but the engaged value, so the row makes a silently-dropped RC
        # function visible. Omitted for passive modes to keep the grid compact.
        if spec is not None and spec.rc_setpoint_w != 0:
            rows.append(self._register_row(
                "rc_enable", "RC enable (43132)",
                RC_ADJUSTMENT_AC_PORT_VALUE,
                self._inverter.get_rc_adjustment_value(),
                exact=True,
            ))
        rows.append(self._register_row(
            "rc_setpoint_w", "RC setpoint (W)",
            spec.rc_setpoint_w if spec else None,
            self._inverter.get_rc_setpoint_w(),
        ))
        return rows

    @staticmethod
    def _register_row(
        name: str,
        label: str,
        desired: Any,
        observed: Any,
        exact: bool = False,
    ) -> ControlRow:
        """Build one register comparison row.

        Args:
            name: Stable machine key for the register (e.g. ``reg_43110``).
            label: Human-readable label rendered in the panel.
            desired: The value ``apply_mode`` targets for this register, or
                ``None`` when the commanded mode leaves it at the hardware
                default (an informational, no-target row).
            observed: The current readback, or ``None`` when unavailable.
            exact: When ``True``, require an exact integer match (the register
                bitmask). Otherwise allow ``_NUMBER_WRITE_EPSILON`` slack — the
                same tolerance ``apply_mode`` uses to decide a number write, so
                "match" here means "no write would be issued".

        Returns:
            A :class:`ControlRow` whose ``status`` is ``no_target`` when
            ``desired`` is ``None``, ``unknown`` when the readback is missing,
            and ``match`` / ``mismatch`` otherwise. An unreadable register is
            deliberately *not* ``mismatch`` — nothing is known to be wrong, so
            re-commanding would be pointless.
        """
        status: ControlStatus
        if desired is None:
            status = "no_target"
        elif observed is None:
            status = "unknown"
        elif exact:
            status = "match" if int(observed) == int(desired) else "mismatch"
        elif abs(float(observed) - float(desired)) <= _NUMBER_WRITE_EPSILON:
            status = "match"
        else:
            status = "mismatch"
        return ControlRow(
            name=name, label=label, desired=desired, observed=observed, status=status,
        )
