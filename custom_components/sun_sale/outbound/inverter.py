"""Inverter control abstraction.

This module owns all platform-specific HA service calls that touch the
inverter, plus the read-side helpers used by translators to capture the
inverter's current state.

For Solis (the only fully-supported platform at the moment), the controller
exposes a single primitive — ``apply_mode(mode, spec)`` — that drives the
inverter into the target ``StorageMode`` by writing the bit switches that
compose register 43110 plus the number entities for export limit, charge /
discharge currents, and the Remote-Control active-power setpoint.

Every write is idempotent: the current readback is consulted first, and
the underlying HA service is only invoked when the value differs from the
target. This eliminates flash wear from no-op rewrites and keeps Modbus
traffic to a minimum.

Other platforms (Huawei, SolarEdge, GoodWe, generic) still produce the
state-read telemetry needed by the rest of the pipeline; their
``apply_mode`` is a logged no-op pending platform-specific implementations.
"""
from __future__ import annotations

import logging
from enum import Enum

from homeassistant.core import HomeAssistant

from ..contract.models import (
    BatteryConfig,
    Limit,
    StorageMode,
)
from ..ha_state import (
    available_state,
    normalize_energy_to_kwh,
    read_float_state,
    read_soc_fraction,
)
from ..inbound.telemetry import (
    GenericCodec,
    HaTelemetryReader,
    SolisCodec,
    TelemetryCodec,
    TelemetryReader,
    TelemetrySignal,
    resolve_bindings,
)
from .storage_mode_specs import StorageModeSpec

_LOGGER = logging.getLogger(__name__)


class InverterPlatform(Enum):
    """Supported HA inverter integrations.

    ``SOLIS`` has the original register-level write path (``InverterController``
    + ``SolisDriver``). ``HUAWEI_SOLAR``, ``SOLAX``, ``SUNGROW``, ``GOODWE`` and
    ``DEYE`` have entity-driven control drivers built on
    ``outbound/entity_control.py`` — implemented but **untested against
    hardware** (see each ``*_driver.py`` module's observations). ``FRONIUS``,
    ``SMA`` and ``KOSTAL`` are stubs carrying implementation notes only (their
    HA integrations are read-only or need raw-Modbus plumbing the driver layer
    does not yet have). The remaining values expose telemetry with a no-op
    ``apply_mode``.
    """

    HUAWEI_SOLAR = "huawei_solar"
    SOLAREDGE = "solaredge"
    GOODWE = "goodwe"
    SOLIS = "solis_modbus"
    SOLAX = "solax_modbus"
    SUNGROW = "sungrow"
    DEYE = "deye"
    FRONIUS = "fronius"
    SMA = "sma"
    KOSTAL = "kostal_plenticore"
    GENERIC = "generic"


# Tolerance (amps / watts) below which a number write is treated as a no-op.
_NUMBER_WRITE_EPSILON = 0.5

# Register 43110 bit map — see ``docs/solis_control.md`` §2.
_REG_43110_BIT_ROLES: dict[int, str] = {
    0: "self_use_switch",
    1: "tou_mode_switch",
    5: "allow_grid_charge_switch",
    6: "feed_in_priority_switch",
}

# Remote-control (RC) function — registers 43128 (setpoint) / 43132
# (function selector) / 43282 (deadman timeout). The setpoint only acts
# while the selector is engaged, and the inverter reverts the whole RC
# function when no RC write arrives within the timeout (1..30 min) — see
# docs/solis_control.md §3 caveats and Pho3niX90/solis_modbus#352.
_RC_ADJUSTMENT_OFF = "OFF"
_RC_ADJUSTMENT_AC_PORT = "Inverter AC Grid Port"
_RC_ADJUSTMENT_VALUES: dict[str, int] = {
    "OFF": 0,
    "System Grid Connection Point": 1,
    "Inverter AC Grid Port": 2,
}
# Register value of the engaged selector option — exported for the control
# module's register_status row.
RC_ADJUSTMENT_AC_PORT_VALUE = _RC_ADJUSTMENT_VALUES[_RC_ADJUSTMENT_AC_PORT]
# Deadman window written to 43282 (the entity max). The control module
# refreshes it every coordinator tick while an RC-backed mode is held, so
# the inverter only falls back to its base 43110 mode if sunSale stops
# dispatching for a full window.
RC_TIMEOUT_MINUTES = 30.0


class InverterController:
    """Platform-aware reader / writer for the configured inverter.

    For Solis the role-keyed ``entity_ids`` dict is populated by
    ``inbound/solis_entity_resolver.py`` or by the manual-mapping form
    in ``config_flow.py``. The required role keys are:

      Telemetry (always required):
        ``battery_soc``, ``battery_power``, ``grid_power``

      Storage Control word (register 43110):
        ``storage_control_readback`` (sensor)
        ``self_use_switch``          (bit 0)
        ``tou_mode_switch``          (bit 1)
        ``allow_grid_charge_switch`` (bit 5)
        ``feed_in_priority_switch``  (bit 6)

      Number entities:
        ``battery_max_charge_current``     (charge amps)
        ``battery_max_discharge_current``  (discharge amps)
        ``rc_setpoint``                    (RC active-power setpoint, W — reg 43128)
        ``rc_timeout``                     (RC deadman timeout, min — reg 43282)
        ``backflow_power``                 (export cap, W)

      Select entities:
        ``rc_grid_adjustment_select``      (RC function selector — reg 43132)

      Other switches:
        ``grid_feed_in_power_limit_switch``     (export-limit enable)
        ``allow_export_under_self_use_switch``  (master export gate)

    Missing entity IDs degrade gracefully — the write is logged as a warning
    and skipped, never raised.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        platform: InverterPlatform,
        entity_ids: dict[str, str],
        battery_config: BatteryConfig | None = None,
    ) -> None:
        """Initialise controller with platform, entity map, and optional battery config.

        Args:
            hass: Home Assistant instance for service calls and state reads.
            platform: Inverter platform enum determining the dispatch path.
            entity_ids: Platform-specific entity-ID map (see class docstring).
            battery_config: Battery parameters used to bound currents.
        """
        self._hass = hass
        self._platform = platform
        self._entity_ids = entity_ids
        self._battery_config = battery_config
        # Shared idempotent number/select write path. Imported lazily because
        # ``entity_control`` imports ``InverterPlatform`` from this module — a
        # top-level import here would close the cycle. ``log_prefix="inverter"``
        # keeps this controller's register-level diagnostics on their historical
        # "inverter:" tag.
        from .entity_control import EntityActuator
        self._actuator = EntityActuator(hass, entity_ids, log_prefix="inverter")
        # Platform telemetry codec — the single source of truth for the
        # battery / grid sign conventions. The controller is the platform
        # authority, so it owns the one selection; the coordinator threads
        # ``codec`` into the panel spec and the recorder resampler.
        self._codec: TelemetryCodec = (
            SolisCodec() if platform == InverterPlatform.SOLIS else GenericCodec()
        )
        # Live telemetry reader over the codec — owns the battery / grid-net
        # role/fallback chain that the getters below used to inline.
        self._reader: TelemetryReader = HaTelemetryReader(
            resolve_bindings(entity_ids), self._codec
        )

    @property
    def codec(self) -> TelemetryCodec:
        """Return the platform telemetry codec (battery / grid sign conventions)."""
        return self._codec

    # ------------------------------------------------------------------ #
    # State reads — telemetry (any platform)                              #
    # ------------------------------------------------------------------ #

    def get_battery_soc(self) -> float | None:
        """Return battery SoC as 0.0–1.0, or ``None`` when the sensor is unavailable.

        Unlike battery / grid power — where 0.0 is a benign degraded reading —
        SoC has no safe fabricated default: a guessed value would feed the
        scheduler a fictional battery state and, with automation on, dispatch
        against it. Returning ``None`` lets ``BatteryTranslator`` drop the whole
        ``BatteryReading`` so the pipeline degrades to ``no_target`` rather than
        planning on invented telemetry.

        Returns:
            SoC as a 0.0–1.0 fraction, or ``None`` when the SoC sensor is
            absent, unavailable, or unparseable.
        """
        return read_soc_fraction(self._hass, self._entity_ids.get("battery_soc", ""))

    def get_battery_power(self) -> float:
        """Return battery power in kW (positive = charging). 0.0 when unavailable.

        Prefers the signed ``battery_power_signed`` role. On Solis this is the
        derived ``battery_power_net`` sensor, whose convention is
        discharge-positive (negative while charging) — the opposite of
        sunSale's positive=charging — so its sign is flipped here. When the
        signed sensor is absent (older solis_modbus, generic platform, or a
        manual mapping), the controller falls back to the ``battery_power``
        role read as-is. That magnitude sensor carries no direction, so the
        battery would read as perpetually charging — the signed role exists
        precisely to avoid that.

        Returns:
            Battery power in kW, positive = charging, negative = discharging.
        """
        value = self._reader.read(self._hass, TelemetrySignal.BATTERY_POWER)
        return value if value is not None else 0.0

    def get_battery_charge_energy_today(self) -> float | None:
        """Return today's cumulative battery charge energy in kWh, or None.

        Daily-resetting AC-side counter feeding the capacity estimator. ``None``
        when the entity is unmapped / unavailable, so the estimator can tell
        "no counter" apart from "0 kWh charged so far today".

        Returns:
            Cumulative charge energy in kWh, or None when unavailable.
        """
        return self._read_energy_kwh_optional("battery_charge_energy_today")

    def get_battery_discharge_energy_today(self) -> float | None:
        """Return today's cumulative battery discharge energy in kWh, or None.

        Daily-resetting AC-side counter feeding the capacity estimator. ``None``
        when the entity is unmapped / unavailable.

        Returns:
            Cumulative discharge energy in kWh, or None when unavailable.
        """
        return self._read_energy_kwh_optional("battery_discharge_energy_today")

    def get_grid_power(self) -> float:
        """Return grid power in kW (positive = importing). 0.0 when unavailable.

        On Solis the primary auto-detected entity is the derived
        ``grid_power_net`` sensor, which already matches sunSale's
        positive=import convention so no sign-flip is applied. When the
        primary is missing (e.g. the Modbus meter chain dropping out), the
        reader falls back to ``ac_grid_port_power`` whose positive=inverter→grid
        convention the codec flips to sunSale's positive=import.
        """
        value = self._reader.read(self._hass, TelemetrySignal.GRID_NET_POWER)
        return value if value is not None else 0.0

    # ------------------------------------------------------------------ #
    # State reads — Solis state machine (consumed by InverterModeTranslator) #
    # ------------------------------------------------------------------ #

    def get_storage_control_word(self) -> int | None:
        """Return the current value of register 43110.

        Returns:
            Integer bitmask, or ``None`` when the readback sensor is
            absent / unavailable. The translator decodes this to a
            ``StorageMode`` via ``storage_mode_specs.decode_mode``.
        """
        raw = self._read_optional_float("storage_control_readback")
        return int(raw) if raw is not None else None

    def get_charge_current_a(self) -> float | None:
        """Return the configured battery max charge current in amps."""
        return self._read_optional_float("battery_max_charge_current")

    def get_discharge_current_a(self) -> float | None:
        """Return the configured battery max discharge current in amps."""
        return self._read_optional_float("battery_max_discharge_current")

    def get_rc_setpoint_w(self) -> int | None:
        """Return the Remote-Control AC active-power setpoint in watts."""
        raw = self._read_optional_float("rc_setpoint")
        return int(raw) if raw is not None else None

    def get_backflow_power_w(self) -> int | None:
        """Return the currently configured export (backflow) limit in watts."""
        raw = self._read_optional_float("backflow_power")
        return int(raw) if raw is not None else None

    def get_rc_adjustment_value(self) -> int | None:
        """Return the RC Grid Adjustment selector (register 43132) as its register value.

        Reads the select entity's state and maps the option label to the
        register value (0=OFF, 1=System Grid Connection Point, 2=Inverter AC
        Grid Port). The select holds its option optimistically on write, so
        this readback tracks sunSale's own writes without waiting for the
        next solis_modbus poll.

        Returns:
            Register value, or ``None`` when the select is unmapped,
            unavailable, or in an unrecognised state.
        """
        entity_id = self._entity_ids.get("rc_grid_adjustment_select", "")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return _RC_ADJUSTMENT_VALUES.get(state.state)

    def is_rc_engaged(self) -> bool | None:
        """Return whether the RC active-power function is engaged at the AC grid port.

        Compares the RC Grid Adjustment selector (register 43132) readback
        against the engaged value sunSale writes for RC-backed modes. This is
        the authoritative gate for whether the RC setpoint is actually acting:
        the setpoint register (43128) retains its last value in RAM after the
        function reverts, so the setpoint alone cannot tell a live Discharge
        from a stale one.

        Returns:
            ``True`` when the selector is engaged at the AC grid port, ``False``
            when it reads any other (released) value, and ``None`` when the
            selector entity is unmapped or unreadable.
        """
        value = self.get_rc_adjustment_value()
        if value is None:
            return None
        return value == RC_ADJUSTMENT_AC_PORT_VALUE

    # ------------------------------------------------------------------ #
    # Write side — apply_mode(StorageMode)                                 #
    # ------------------------------------------------------------------ #

    async def apply_mode(
        self,
        mode: StorageMode,
        spec: StorageModeSpec,
        force: bool = False,
    ) -> None:
        """Drive the inverter to the target StorageMode via the minimum set of writes.

        Each write step (bit switches, export limit, currents, RC setpoint)
        consults its current readback first and skips the write when the
        readback already matches the target. This makes consecutive calls
        with the same mode free of side effects.

        Every step goes through :class:`..outbound.entity_control.EntityActuator`,
        which clamps a number target into the entity's advertised range and
        logs (rather than raises) a failed service call — so a single rejected
        register can no longer skip the writes that follow it, the RC
        engage/release block in particular.

        On non-Solis platforms this is currently a no-op pending a
        platform-specific implementation; the call is logged so observability
        is not lost.

        Args:
            mode: Target StorageMode (used for logging only — the concrete
                register targets live in ``spec``).
            spec: Concrete register targets for the requested mode.
            force: When ``True``, skip the cached-readback comparison and
                always issue the underlying service call. Used by the
                control module's verify-loop on commanded-mode change and
                on retry-after-mismatch, where trusting the (possibly
                stale) solis_modbus cache would risk hiding a failed write.
        """
        if self._platform != InverterPlatform.SOLIS:
            _LOGGER.debug(
                "apply_mode(%s) — platform %s has no register-level implementation",
                mode.value, self._platform.value,
            )
            return
        _LOGGER.debug(
            "apply_mode(%s, force=%s) — reg_43110=0x%x charge_a=%s "
            "discharge_a=%s export_limit_w=%s rc_setpoint_w=%s",
            mode.value, force,
            spec.reg_43110_value,
            spec.charge_a,
            spec.discharge_a,
            spec.export_limit_w,
            spec.rc_setpoint_w,
        )

        await self._apply_43110_bits(spec.reg_43110_value, force=force)
        if spec.charge_a is not None:
            await self._set_number(
                "battery_max_charge_current",
                spec.charge_a,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        if spec.discharge_a is not None:
            await self._set_number(
                "battery_max_discharge_current",
                spec.discharge_a,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        if spec.export_limit_w is not None:
            await self._set_number(
                "backflow_power",
                float(spec.export_limit_w),
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
        # RC function (43132 selector / 43282 timeout / 43128 setpoint).
        # Write order matters on RC-backed modes: the inverter ignores the
        # setpoint while the selector is OFF, and the timeout only latches
        # when written *after* the function is enabled — otherwise it falls
        # back to a ~5-min default (Pho3niX90/solis_modbus#352).
        if spec.rc_setpoint_w != 0:
            if not self._entity_ids.get("rc_grid_adjustment_select"):
                _LOGGER.warning(
                    "apply_mode(%s): RC-backed mode but no rc_grid_adjustment "
                    "select is mapped — the RC setpoint will not engage",
                    mode.value,
                )
            await self._set_select(
                "rc_grid_adjustment_select", _RC_ADJUSTMENT_AC_PORT, force=force,
            )
            await self._set_number(
                "rc_timeout",
                RC_TIMEOUT_MINUTES,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
                renew=force,
            )
            await self._set_number(
                "rc_setpoint",
                float(spec.rc_setpoint_w),
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
                renew=force,
            )
        else:
            # Zero the setpoint while the function is still engaged, then
            # release the selector so a stale setpoint can never act again.
            await self._set_number(
                "rc_setpoint",
                0.0,
                tolerance_a=_NUMBER_WRITE_EPSILON,
                force=force,
            )
            await self._set_select(
                "rc_grid_adjustment_select", _RC_ADJUSTMENT_OFF, force=force,
            )

    async def refresh_rc(self, spec: StorageModeSpec) -> None:
        """Re-arm the inverter-side RC function while an RC-backed mode is held.

        The RC active-power function expires ~5 min after the last *engaging*
        write: register 43282's 30-min request does not latch unless the RC
        force-charge/discharge enable (43135) is written first
        (Pho3niX90/solis_modbus#352), and sunSale drives the AC-grid-port
        setpoint path instead — so the inverter falls back to its ~5-min
        default. A 2026-08-04 production capture measured export collapsing at
        exactly 5 min 08 s into a held discharge while 43110 / 43128 / 43132 all
        still read "engaged"; the registers themselves only reverted ~10 min in.

        So this rewrites the full engage sequence (selector → timeout →
        setpoint) on every keep-alive, exactly like ``apply_mode``'s RC block,
        with ``renew=True`` on the numbers: solis_modbus drops a ``number``
        write whose value equals the register's current one, which made every
        "forced" setpoint/timeout re-arm a silent no-op and left the selector
        write as the only one reaching the wire. That is also what the
        2026-06-15 capture actually showed — the setpoint rewrites it recorded
        as ineffective had never been sent. The RC registers are RAM-only, so
        the repeated writes cause no flash wear.

        Args:
            spec: Spec of the currently held mode; no-op unless it carries a
                non-zero RC setpoint.
        """
        if self._platform != InverterPlatform.SOLIS:
            return
        if spec.rc_setpoint_w == 0:
            return
        # Always re-engage the selector — re-issuing it (even to the same
        # value) is what re-arms the inverter-side function; skipping it when
        # the cached readback still shows "engaged" is precisely what let the
        # function expire underneath us.
        await self._set_select(
            "rc_grid_adjustment_select", _RC_ADJUSTMENT_AC_PORT, force=True,
        )
        await self._set_number(
            "rc_timeout",
            RC_TIMEOUT_MINUTES,
            tolerance_a=_NUMBER_WRITE_EPSILON,
            force=True,
            renew=True,
        )
        await self._set_number(
            "rc_setpoint",
            float(spec.rc_setpoint_w),
            tolerance_a=_NUMBER_WRITE_EPSILON,
            force=True,
            renew=True,
        )

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _apply_43110_bits(
        self, target_value: int, force: bool = False,
    ) -> None:
        """Toggle the bit switches whose desired state differs from the readback.

        Reads the current value of register 43110 via the ``storage_control_readback``
        sensor; for each known bit (0/1/5/6) compares the target's bit to the
        observed bit and turns the corresponding switch on / off when they differ.
        Bits whose role switch is not mapped in ``entity_ids`` are silently skipped.

        Args:
            target_value: Desired bitmask value for register 43110.
            force: When ``True``, skip the readback comparison and always
                call the underlying switch service. The solis_modbus state
                cache can lag a failed-or-pending write by up to a poll
                interval, so trusting it on commanded-mode change is unsafe.
        """
        current = self.get_storage_control_word()
        for bit, role in _REG_43110_BIT_ROLES.items():
            target_bit = (target_value >> bit) & 1
            current_bit = ((current >> bit) & 1) if current is not None else None
            entity_id = self._entity_ids.get(role, "")
            if not force and current_bit == target_bit:
                _LOGGER.debug(
                    "inverter: 43110 bit %d (%s) skip — cached_bit=%s "
                    "target_bit=%s cached_reg=%s",
                    bit, role, current_bit, target_bit, current,
                )
                continue
            if not entity_id:
                _LOGGER.debug(
                    "inverter: 43110 bit %d (%s) skip — no entity mapped "
                    "(cached_bit=%s target_bit=%s force=%s)",
                    bit, role, current_bit, target_bit, force,
                )
                continue
            service = "turn_on" if target_bit else "turn_off"
            _LOGGER.debug(
                "inverter: 43110 bit %d (%s) write — cached_bit=%s "
                "target_bit=%s service=switch.%s entity=%s force=%s",
                bit, role, current_bit, target_bit, service, entity_id, force,
            )
            # Routed through the actuator (force=True — the bit comparison
            # above already decided) so a rejected toggle is logged instead of
            # aborting the remaining bits and the RC block that follows.
            await self._actuator.set_switch(role, bool(target_bit), force=True)

    async def _set_number(
        self,
        role: str,
        target_value: float,
        tolerance_a: float,
        force: bool = False,
        renew: bool = False,
    ) -> None:
        """Write a number entity only when its readback differs by more than tolerance.

        Args:
            role: Entity-ID map key (e.g. ``battery_max_charge_current``).
            target_value: Desired value to set.
            tolerance_a: Absolute tolerance under which the write is skipped.
            force: When ``True``, skip the readback comparison and always
                issue the underlying ``number.set_value`` service call.
            renew: When ``True``, make the write reach the inverter even when
                the register already holds the target — solis_modbus drops a
                same-value ``number`` write, which silently voided every RC
                deadman re-arm. See ``EntityActuator._renew_nudge``.
        """
        await self._actuator.set_number(
            role, target_value, tolerance=tolerance_a, force=force, renew=renew,
        )

    async def _set_select(
        self,
        role: str,
        option: str,
        force: bool = False,
    ) -> None:
        """Select an option on a select entity only when its state differs.

        Args:
            role: Entity-ID map key (e.g. ``rc_grid_adjustment_select``).
            option: Target option label.
            force: When ``True``, skip the current-state comparison and always
                issue the underlying ``select.select_option`` service call.
        """
        await self._actuator.set_select(role, option, force=force)

    def raw_states(self, roles: tuple[str, ...]) -> dict[str, str | None]:
        """Return the current HA state string of each mapped role, for diagnostics.

        Unmapped roles are omitted; a mapped role whose entity is missing reads
        ``None``. Values are left as raw strings so a capture shows exactly what
        the integration published.

        Args:
            roles: Entity-ID map keys to capture.

        Returns:
            Role → state string (or ``None``).
        """
        out: dict[str, str | None] = {}
        for role in roles:
            entity_id = self._entity_ids.get(role, "")
            if not entity_id:
                continue
            state = self._hass.states.get(entity_id)
            out[role] = state.state if state is not None else None
        return out

    def limit_for(self, role: str) -> Limit:
        """Return the live writable bound advertised for one actuator ``role``.

        Pass-through to :meth:`..outbound.entity_control.EntityActuator.limit_for`
        — the single place upstream ``min``/``max`` attributes are read. Lets
        :class:`..outbound.solis_driver.SolisDriver` reduce a composed spec
        through the same bounds the write path clamps to.

        Args:
            role: Entity-ID map key (e.g. ``battery_max_charge_current``).

        Returns:
            The resolved :class:`Limit`; ``known=False`` when the role is
            unmapped or its entity is absent.
        """
        return self._actuator.limit_for(role)

    def _read_optional_float(self, key: str) -> float | None:
        """Read a numeric HA state; return ``None`` when absent or unparseable.

        Args:
            key: Entity-ID map key.

        Returns:
            Parsed float, or ``None``.
        """
        return read_float_state(self._hass, self._entity_ids.get(key, ""))

    def _read_energy_kwh_optional(self, key: str) -> float | None:
        """Read a cumulative-energy entity and normalise to kWh, or None.

        For energy counters an empty or
        unrecognised unit is treated as kWh (the canonical internal unit); a
        ``Wh`` / ``MWh`` sensor is rescaled. Returns None when the entity is
        unmapped, unavailable, or non-numeric.

        Args:
            key: Entity-ID map key (e.g. "battery_charge_energy_today").

        Returns:
            Cumulative energy in kWh, or None when unavailable.
        """
        state = available_state(self._hass, self._entity_ids.get(key, ""))
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = str(state.attributes.get("unit_of_measurement") or "").strip()
        return normalize_energy_to_kwh(value, unit)
