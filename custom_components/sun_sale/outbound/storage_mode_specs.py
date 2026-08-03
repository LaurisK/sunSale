"""Storage-mode register specs and observed-state decoder.

The Solis hybrid inverter exposes a small set of register groups (see
``docs/solis_control.md``). Each high-level StorageMode is a *composition* of
target values across (43110 bitmask, export limit, charge current, discharge
current, RC active-power setpoint). This Solis-owned module (in the outbound
driver layer) owns:

  - ``StorageModeSpec``   — the concrete per-mode register targets. Solis
    hardware detail, kept out of ``contract/`` so the shared vocabulary stays
    platform-neutral; the generic ``InverterDriver`` treats it as an opaque token.
  - ``build_specs(...)``  — concrete StorageModeSpec for each StorageMode given
    the deployment's battery and inverter limits.
  - ``decode_mode(...)``  — best-effort decode of an observed register state to
    a StorageMode, used by ``SolisDriver``.

The pipeline scheduler (``pipeline/schedule.py``) picks StorageMode values
directly via slot physics — there is no separate planner-side decision
type, and it never touches a StorageModeSpec.

Pure Python: no Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contract.models import (
    BatteryConfig,
    StorageMode,
)


@dataclass(frozen=True)
class StorageModeSpec:
    """Concrete register targets for one StorageMode (Solis).

    Fields use ``None`` where the inverter's existing value should be left
    unchanged (e.g. TRACK leaves the export limit at the hardware default).
    """
    reg_43110_value: int               # bitmask target for Storage Control word
    export_limit_w: int | None         # None → leave hardware default
    charge_a: float | None             # None → leave hardware default
    discharge_a: float | None          # None → leave hardware default
    rc_setpoint_w: int                 # signed; + = export, − = charge, 0 = none


_CURRENT_EPSILON_A = 1e-3

# |RC setpoint| (W) at or below which the RC function reads as "not driving".
_RC_SETPOINT_EPSILON_W = 1


def _amps_from_kw(power_kw: float, voltage_v: float) -> float:
    """Convert a DC-side power limit to amps at the configured bus voltage.

    Args:
        power_kw: Power in kilowatts (≥0).
        voltage_v: DC bus voltage; falls back to 48 V when ≤0 for safety.

    Returns:
        Equivalent current in amps.
    """
    v = voltage_v if voltage_v > 0 else 48.0
    return (power_kw * 1000.0) / v


def build_specs(
    battery_config: BatteryConfig,
    export_max_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, StorageModeSpec]:
    """Return the concrete StorageModeSpec table for this deployment.

    ``UNKNOWN`` is intentionally omitted — it is only ever an observed-state
    label and is never applied to the hardware.

    Args:
        battery_config: Battery limits (used to derive max charge/discharge amps).
        export_max_w: Backflow / export power cap for FeedIn and SelfUse modes.
        inverter_max_power_w: Rated AC output, used as the RC setpoint magnitude
            for Discharge (force discharge to grid).

    Returns:
        Dict mapping each StorageMode to its concrete spec.
    """
    i_charge_max_a = _amps_from_kw(
        battery_config.max_charge_power_kw, battery_config.nominal_voltage_v
    )
    i_discharge_max_a = _amps_from_kw(
        battery_config.max_discharge_power_kw, battery_config.nominal_voltage_v
    )
    p_charge_max_w = int(battery_config.max_charge_power_kw * 1000)

    return {
        StorageMode.FeedIn: StorageModeSpec(
            reg_43110_value=64,
            export_limit_w=export_max_w,
            charge_a=i_charge_max_a,
            # Feed-in priority still serves household load from the battery;
            # a 0 A limit here forces grid imports while the battery sits idle.
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=0,
        ),
        StorageMode.SelfUse: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=export_max_w,
            charge_a=i_charge_max_a,
            # Self-use covers baseload from the battery (see
            # slot_physics._simulate_self_use) — 0 A here would physically
            # block the discharge the planner priced in.
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=0,
        ),
        StorageMode.NoExport: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=0,
            charge_a=i_charge_max_a,
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=0,
        ),
        StorageMode.Discharge: StorageModeSpec(
            # Written explicitly rather than inherited (None): a preceding
            # NoExport/GridCharge/StandBy slot leaves the backflow cap at 0,
            # which would clamp the dump to zero export.
            reg_43110_value=64,
            export_limit_w=export_max_w,
            charge_a=0.0,
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=+inverter_max_power_w,
        ),
        StorageMode.GridCharge: StorageModeSpec(
            reg_43110_value=33,
            export_limit_w=0,
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=-p_charge_max_w,
        ),
        StorageMode.StandBy: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=0,
            # Hold: only discharge is barred; charging from surplus stays enabled.
            charge_a=i_charge_max_a,
            discharge_a=0.0,
            rc_setpoint_w=0,
        ),
        StorageMode.TRACK: StorageModeSpec(
            reg_43110_value=1,
            export_limit_w=None,
            charge_a=i_charge_max_a,
            discharge_a=i_discharge_max_a,
            rc_setpoint_w=0,        # caller overrides per-tick
        ),
    }


def decode_mode(
    reg_43110_value: int | None,
    charge_a: float | None,
    discharge_a: float | None,
    rc_setpoint_w: int | None,
    backflow_power_w: int | None = None,
    rc_engaged: bool | None = None,
) -> StorageMode:
    """Best-effort decode of an observed inverter state to a StorageMode.

    The register-43110 bitmask picks the operating family; a single ancillary
    signal then separates the modes that share a bitmask:

      * **43110 = 33** (SelfUse | AllowGridCharge) is unique to GridCharge. The
        RC setpoint only sets the charge *rate*; the bitmask alone identifies
        the mode.
      * **43110 = 64** (FeedIn) covers FeedIn and Discharge. Both allow battery
        discharge, so ``discharge_a`` no longer separates them — the RC
        active-power function does. Discharge engages RC with a positive
        (export) setpoint; FeedIn leaves it released. ``rc_engaged`` (the
        register-43132 selector) gates this: the setpoint register retains its
        last value after the function expires, so a stale positive setpoint
        must not read as Discharge once the selector has dropped.
      * **43110 = 1** (SelfUse) covers StandBy, NoExport, and SelfUse.
        ``discharge_a`` ≈ 0 means the battery is barred from draining → StandBy
        (a battery *hold*; it may still charge from surplus, so ``charge_a`` is
        not part of this test). With discharge enabled, ``backflow_power_w``
        separates SelfUse (cap > 0) from NoExport (cap = 0) — Solis Cloud EMS
        suppresses export by writing that number entity to 0 while leaving
        register 43110 at 1, and ``apply_mode`` writes it the same way. ``None``
        (readback unavailable) falls back to SelfUse.

        An **unreadable** ``discharge_a`` under this bitmask yields ``UNKNOWN``.
        It is the sole discriminator between StandBy and the two SelfUse
        variants, so without it the mode genuinely cannot be told apart;
        defaulting the current to 0 (as this decoder used to) reports a
        confident StandBy for what may be an actively discharging inverter.

    This decoder produces the *observed* label that drives the chart history;
    the control module's *target* mode is the authoritative answer. Hardware
    states that map to no applied mode return ``UNKNOWN``.

    Args:
        reg_43110_value: Raw readback of the Storage Control word.
        charge_a: Configured charge current. Not a discriminator under the
            current taxonomy; accepted for provenance and signature stability.
        discharge_a: Configured discharge current. ``0`` under bitmask 1 means
            StandBy; ``None`` (unreadable) under bitmask 1 means the mode cannot
            be discriminated and yields ``UNKNOWN``.
        rc_setpoint_w: Signed RC active-power setpoint (W); positive = export.
            Separates Discharge from FeedIn under bitmask 64.
        backflow_power_w: Configured export-power cap in watts. ``0`` means
            export is suppressed (NoExport); ``None`` means "unknown — assume
            SelfUse" for backward compatibility.
        rc_engaged: Whether the RC selector (register 43132) is engaged at the
            AC grid port. ``False`` forces the bitmask-64 family to FeedIn
            regardless of a stale setpoint; ``None`` (selector unreadable) falls
            back to the setpoint sign.

    Returns:
        Best-fit StorageMode; ``UNKNOWN`` when ``reg_43110_value`` is ``None``,
        when the bitmask is not one of the recognised states, or when the signal
        that discriminates within the matched bitmask is unreadable.
    """
    del charge_a  # not a discriminator under the 2-bitmask taxonomy

    if reg_43110_value is None:
        return StorageMode.UNKNOWN

    if reg_43110_value == 33:
        return StorageMode.GridCharge
    if reg_43110_value == 64:
        if _rc_is_exporting(rc_setpoint_w, rc_engaged):
            return StorageMode.Discharge
        return StorageMode.FeedIn
    if reg_43110_value == 1:
        if discharge_a is None:
            return StorageMode.UNKNOWN
        if discharge_a <= _CURRENT_EPSILON_A:
            return StorageMode.StandBy
        if backflow_power_w is not None and backflow_power_w <= 0:
            return StorageMode.NoExport
        return StorageMode.SelfUse
    return StorageMode.UNKNOWN


def _rc_is_exporting(rc_setpoint_w: int | None, rc_engaged: bool | None) -> bool:
    """Return whether the RC function is actively forcing grid export.

    Separates Discharge (forced export) from FeedIn under the shared
    ``43110 = 64`` bitmask. The RC setpoint register (43128) retains its last
    value in RAM after the function expires, so the setpoint sign *alone* can
    read as exporting long after the inverter reverted to plain feed-in
    priority. ``rc_engaged`` (the 43132 selector) is the authoritative gate:

      * ``False`` → function released; the setpoint is inert → not exporting,
        regardless of the stale register value.
      * ``None`` → selector unreadable; fall back to the setpoint sign as a
        best-effort, mirroring the module's degrade-on-missing-entity policy.
      * ``True`` → trust the setpoint sign.

    Args:
        rc_setpoint_w: Signed RC active-power setpoint (W); positive = export.
        rc_engaged: Whether the RC selector is engaged at the AC grid port;
            ``None`` when the selector entity could not be read.

    Returns:
        ``True`` when the RC function is forcing grid export (Discharge).
    """
    if rc_setpoint_w is None or rc_setpoint_w <= _RC_SETPOINT_EPSILON_W:
        return False
    return rc_engaged is not False


