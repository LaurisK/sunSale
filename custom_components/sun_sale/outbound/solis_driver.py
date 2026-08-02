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

from datetime import datetime
from typing import Any

from ..contract.models import (
    BatteryConfig,
    InverterModeReading,
    StorageMode,
)
from .driver import ControlRow
from .inverter import (
    _NUMBER_WRITE_EPSILON,
    RC_ADJUSTMENT_AC_PORT_VALUE,
    InverterController,
)
from .storage_mode_specs import StorageModeSpec, build_specs, decode_mode


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
    ) -> None:
        """Build the deployment's spec table and wrap the inverter.

        Args:
            inverter: The register read / write source.
            battery_config: Battery limits — used to derive charge/discharge amps.
            export_limit_w: Backflow / export cap for SELL / STORE modes.
            inverter_max_power_w: Rated AC output used as the RC active-power
                magnitude when discharging to grid.
        """
        self._inverter = inverter
        self._export_limit_w = export_limit_w
        self._specs: dict[StorageMode, StorageModeSpec] = build_specs(
            battery_config,
            export_max_w=export_limit_w,
            inverter_max_power_w=inverter_max_power_w,
        )

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
        """Return the concrete spec for ``mode``, or ``None`` when unsupported."""
        return self._specs.get(mode)

    def needs_keepalive(self, spec: StorageModeSpec) -> bool:
        """Return whether holding ``spec`` requires a volatile keep-alive.

        Solis RC-backed modes (non-zero active-power setpoint) expire their RC
        function and must be periodically re-armed; passive modes do not. Lets
        the control module gate its keep-alive timer without inspecting any
        Solis-specific spec field.
        """
        return spec.rc_setpoint_w != 0

    # --- Write side + readbacks (delegated to the inverter) ------------- #

    async def apply_mode(
        self, mode: StorageMode, spec: StorageModeSpec, force: bool = False
    ) -> None:
        """Drive the inverter to ``mode`` (delegates to the controller)."""
        await self._inverter.apply_mode(mode, spec, force=force)

    async def refresh_rc(self, spec: StorageModeSpec) -> None:
        """Re-arm the RC keep-alive for ``spec`` (delegates to the controller)."""
        await self._inverter.refresh_rc(spec)

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing)."""
        return self._inverter.get_grid_power()

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

        Returns:
            Canonical register rows in ``apply_mode`` write order: ``reg_43110``,
            ``charge_a``, ``discharge_a``, ``export_limit_w``, an optional
            ``rc_enable`` (only when ``commanded`` is RC-backed), and
            ``rc_setpoint_w``.
        """
        spec = self._specs.get(commanded) if commanded is not None else None
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
            A :class:`ControlRow` whose ``match`` is tri-state: ``None`` when
            ``desired`` is ``None`` (no target), ``False`` when ``observed`` is
            ``None`` or differs beyond tolerance, ``True`` on match.
        """
        if desired is None:
            match: bool | None = None
        elif observed is None:
            match = False
        elif exact:
            match = int(observed) == int(desired)
        else:
            match = abs(float(observed) - float(desired)) <= _NUMBER_WRITE_EPSILON
        return ControlRow(
            name=name, label=label, desired=desired, observed=observed, match=match,
        )
