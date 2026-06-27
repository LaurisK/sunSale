"""SolaX (Gen4+) control driver (entity-driven).

Targets the community **wills106/homeassistant-solax-modbus** integration. That
integration also drives several rebadged platforms (Growatt / Sofar / Solinteg /
SRNE / Swatten and even Solis), so this driver is potentially reusable for them
with a different option-label map.

=============================  OBSERVATIONS — UNTESTED  =========================
Implemented from the integration's documented control surface; **not validated
against hardware**.

Why SolaX is a clean fit
------------------------
The "Manual Mode" force-charge/discharge writes on Gen4+ go to RAM, **not
EEPROM** (unlike the normal setting registers), so they can be rewritten as
often as we like with no flash-wear — exactly what a per-slot dispatcher needs.
The integration also offers a ``remotecontrol_*`` power-setpoint path with an
``autorepeat_duration`` that the integration itself re-issues; we use the
simpler Manual-Mode select here and leave the remotecontrol path as a future
refinement for true power-following (TRACK).

Control surface used
--------------------
  * ``use_mode_select`` — ``select.*_charger_use_mode``: "Self Use Mode",
    "Feedin Priority", "Back Up Mode", "Manual Mode".
  * ``manual_mode_select`` — ``select.*_manual_mode_select``: "Stop Charge and
    Discharge", "Force Charge", "Force Discharge". Only meaningful while
    ``use_mode`` = "Manual Mode".
  * ``export_limit`` — ``number.*_export_control_user_limit`` (W). 0 = NoExport.

Known gaps / weak spots
-----------------------
  * **EEPROM wear on ``export_limit``.** The export-control user limit *is* an
    EEPROM register on many models — writing it every NoExport slot risks wear.
    The actuator's idempotent skip mitigates (only writes on change), but prefer
    leaving it pinned. CONFIRM whether your model stores it in RAM.
  * **Manual sub-states are decoded via ``manual_mode_select``.** When
    ``use_mode`` reads "Manual Mode" (omitted from ``_DECODE``), the secondary
    ``manual_mode_select`` decode (``_SUB_DECODE``) recovers Force Charge →
    GridCharge / Force Discharge → Discharge / Stop → StandBy, so both the verify
    loop (control-surface row) and the history readout separate the directions.
    Confirm the manual-mode option labels on hardware.
  * **Force Discharge export rate** is governed by the inverter's own manual
    discharge power register, not modelled here; we only lift the export limit.
  * Entity resolution for the integration's config entry is **not yet wired**;
    fill roles via the manual mapping form until a resolver exists.
================================================================================
"""
from __future__ import annotations

from ..contract.models import BatteryConfig, StorageMode
from .entity_control import (
    ControlPlan,
    EntityControlDriver,
    EntityWrite,
    InverterContext,
    number_write,
)
from .inverter import InverterPlatform

_USE_SELF = "Self Use Mode"
_USE_FEEDIN = "Feedin Priority"
_USE_MANUAL = "Manual Mode"

_MANUAL_STOP = "Stop Charge and Discharge"
_MANUAL_FORCE_CHARGE = "Force Charge"
_MANUAL_FORCE_DISCHARGE = "Force Discharge"


def _build_plans(
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, ControlPlan]:
    """Return the StorageMode → :class:`ControlPlan` table for SolaX.

    Args:
        battery_config: Battery limits (unused today — manual mode uses the
            inverter's own power registers; kept for signature symmetry).
        export_limit_w: Export cap for exporting modes.
        inverter_max_power_w: Forced-discharge magnitude (unused — see gaps).

    Returns:
        Plan table covering every dispatchable StorageMode.
    """
    del battery_config, inverter_max_power_w  # see module observations

    def export_cap(w: int) -> EntityWrite:
        return number_write("export_limit", w, "Export limit (W)")

    return {
        StorageMode.SelfUse: ControlPlan(writes=(
            EntityWrite("use_mode_select", "select", _USE_SELF, "Use mode"),
            export_cap(export_limit_w),
        )),
        StorageMode.FeedIn: ControlPlan(writes=(
            EntityWrite("use_mode_select", "select", _USE_FEEDIN, "Use mode"),
            export_cap(export_limit_w),
        )),
        StorageMode.NoExport: ControlPlan(writes=(
            EntityWrite("use_mode_select", "select", _USE_SELF, "Use mode"),
            export_cap(0),
        )),
        StorageMode.StandBy: ControlPlan(writes=(
            EntityWrite("use_mode_select", "select", _USE_MANUAL, "Use mode"),
            EntityWrite("manual_mode_select", "select", _MANUAL_STOP, "Manual mode"),
        )),
        StorageMode.GridCharge: ControlPlan(writes=(
            EntityWrite("use_mode_select", "select", _USE_MANUAL, "Use mode"),
            EntityWrite("manual_mode_select", "select", _MANUAL_FORCE_CHARGE, "Manual mode"),
        )),
        StorageMode.Discharge: ControlPlan(writes=(
            EntityWrite("use_mode_select", "select", _USE_MANUAL, "Use mode"),
            EntityWrite("manual_mode_select", "select", _MANUAL_FORCE_DISCHARGE, "Manual mode"),
            export_cap(export_limit_w),
        )),
    }


_DECODE: dict[str, StorageMode] = {
    _USE_SELF: StorageMode.SelfUse,
    _USE_FEEDIN: StorageMode.FeedIn,
    # "Manual Mode" intentionally absent — the direction lives in the manual-mode
    # sub-select below, consulted only when ``use_mode`` reads this umbrella.
}

# Sub-state decode: when ``use_mode`` = "Manual Mode" (UNKNOWN to _DECODE), the
# manual-mode select recovers the forced direction so the history / diagnostic
# readout separates GridCharge from Discharge instead of collapsing both to
# UNKNOWN.
_SUB_DECODE: dict[str, StorageMode] = {
    _MANUAL_STOP: StorageMode.StandBy,
    _MANUAL_FORCE_CHARGE: StorageMode.GridCharge,
    _MANUAL_FORCE_DISCHARGE: StorageMode.Discharge,
}


def make_solax_driver(
    context: InverterContext,
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> EntityControlDriver:
    """Build the SolaX entity-driven control driver.

    Args:
        context: HA handle, role→entity-id map, and grid-power reader.
        battery_config: Battery limits (forwarded for symmetry).
        export_limit_w: Export cap for exporting modes.
        inverter_max_power_w: Forced-discharge magnitude (forwarded).

    Returns:
        An :class:`EntityControlDriver` configured for SolaX.
    """
    return EntityControlDriver.from_context(
        context,
        platform=InverterPlatform.SOLAX,
        plans=_build_plans(battery_config, export_limit_w, inverter_max_power_w),
        mode_role="use_mode_select",
        decode_map=_DECODE,
        mode_label="Use mode",
        sub_decode_role="manual_mode_select",
        sub_decode_map=_SUB_DECODE,
    )
