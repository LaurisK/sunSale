"""Sungrow SH-RS / SH-T control driver (entity-driven).

Targets the **mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant** YAML package.
Sungrow's EMS exposes a clean forced-mode command set, so the control mapping is
the most faithful of the entity-driven platforms.

=============================  OBSERVATIONS — UNTESTED  =========================
Implemented from the package's ``doc/usage.md`` control scheme; **not validated
against hardware**.

Integration shape caveat (important)
------------------------------------
mkaiser's package is a **YAML modbus package**, not a config-entry integration.
There is therefore *no config entry to auto-detect or scan* — sunSale's Solis-
style ``async_entries(domain)`` resolver cannot find these entities. The roles
below MUST be filled via the manual entity-mapping form, and the entity ids are
whatever the user named them in their package (no stable ``unique_id`` scheme).
This is the main friction for Sungrow, not the control mapping.

Control surface used
--------------------
  * ``ems_mode_select`` — "EMS mode selection": "Self-consumption mode (default)",
    "Forced mode", "External EMS".
  * ``forced_cmd_select`` — "Battery forced charge discharge cmd": "Stop
    (default)", "Forced charge", "Forced discharge".
  * ``forced_power`` — "Set forced charge discharge power" (W).
  * ``export_power`` — export-power limit (W); 0 = zero export. NOTE: Sungrow
    gates this with a separate "export power limit mode" enable switch — wire
    ``export_limit_enable_switch`` if your model needs it (left out here to keep
    the plan minimal; see gaps).

Known gaps / weak spots
-----------------------
  * **No feed-in-priority mode.** Sungrow self-consumption already exports
    surplus, so ``FeedIn`` maps to self-consumption with the export limit opened
    to ``export_limit_w`` — there is no distinct "battery-charges-from-surplus-
    first" mode to distinguish it from ``SelfUse``. ``decode_observed`` cannot
    tell them apart (both read "Self-consumption mode").
  * **Export-limit enable gating.** Zero-export only takes effect when the export
    power limit *mode* is enabled. If your install relies on it, add the enable
    switch to the NoExport (and remove from exporting) plans.
  * **Forced charge/discharge share one EMS state, disambiguated by sub-decode.**
    The EMS select reads "Forced mode" for GridCharge / Discharge / StandBy
    alike; the secondary ``forced_cmd_select`` decode (``_SUB_DECODE``) recovers
    the direction, so both the verify loop (control-surface row) and the history
    readout separate them. Confirm the forced-cmd option labels on hardware.
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

_EMS_SELF = "Self-consumption mode (default)"
_EMS_FORCED = "Forced mode"

_CMD_STOP = "Stop (default)"
_CMD_CHARGE = "Forced charge"
_CMD_DISCHARGE = "Forced discharge"


def _build_plans(
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, ControlPlan]:
    """Return the StorageMode → :class:`ControlPlan` table for Sungrow.

    Args:
        battery_config: Battery limits → forced-charge power.
        export_limit_w: Export cap for exporting modes / zero for NoExport.
        inverter_max_power_w: Forced-discharge power magnitude.

    Returns:
        Plan table covering every dispatchable StorageMode.
    """
    charge_w = int(battery_config.max_charge_power_kw * 1000)

    def export_power(w: int) -> EntityWrite:
        return number_write("export_power", w, "Export power limit (W)")

    def forced_power(w: int) -> EntityWrite:
        return number_write("forced_power", w, "Forced charge/discharge power (W)")

    return {
        StorageMode.SelfUse: ControlPlan(writes=(
            EntityWrite("ems_mode_select", "select", _EMS_SELF, "EMS mode"),
            export_power(export_limit_w),
        )),
        StorageMode.FeedIn: ControlPlan(writes=(
            EntityWrite("ems_mode_select", "select", _EMS_SELF, "EMS mode"),
            export_power(export_limit_w),
        )),
        StorageMode.NoExport: ControlPlan(writes=(
            EntityWrite("ems_mode_select", "select", _EMS_SELF, "EMS mode"),
            export_power(0),
        )),
        StorageMode.StandBy: ControlPlan(writes=(
            EntityWrite("ems_mode_select", "select", _EMS_FORCED, "EMS mode"),
            EntityWrite("forced_cmd_select", "select", _CMD_STOP, "Forced cmd"),
        )),
        StorageMode.GridCharge: ControlPlan(writes=(
            EntityWrite("ems_mode_select", "select", _EMS_FORCED, "EMS mode"),
            EntityWrite("forced_cmd_select", "select", _CMD_CHARGE, "Forced cmd"),
            forced_power(charge_w),
        )),
        StorageMode.Discharge: ControlPlan(writes=(
            EntityWrite("ems_mode_select", "select", _EMS_FORCED, "EMS mode"),
            EntityWrite("forced_cmd_select", "select", _CMD_DISCHARGE, "Forced cmd"),
            forced_power(inverter_max_power_w),
            export_power(export_limit_w),
        )),
    }


_DECODE: dict[str, StorageMode] = {
    _EMS_SELF: StorageMode.SelfUse,
    # "Forced mode" omitted — direction lives in the forced-cmd sub-select below.
}

# Sub-state decode: when EMS mode reads "Forced mode" (UNKNOWN to _DECODE), the
# forced-cmd select recovers the direction so GridCharge / Discharge / StandBy
# are separated in the history / diagnostic readout.
_SUB_DECODE: dict[str, StorageMode] = {
    _CMD_STOP: StorageMode.StandBy,
    _CMD_CHARGE: StorageMode.GridCharge,
    _CMD_DISCHARGE: StorageMode.Discharge,
}


def make_sungrow_driver(
    context: InverterContext,
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> EntityControlDriver:
    """Build the Sungrow entity-driven control driver.

    Args:
        context: HA handle, role→entity-id map, and grid-power reader.
        battery_config: Battery limits for forced-charge power.
        export_limit_w: Export cap.
        inverter_max_power_w: Forced-discharge magnitude.

    Returns:
        An :class:`EntityControlDriver` configured for Sungrow.
    """
    return EntityControlDriver.from_context(
        context,
        platform=InverterPlatform.SUNGROW,
        plans=_build_plans(battery_config, export_limit_w, inverter_max_power_w),
        mode_role="ems_mode_select",
        decode_map=_DECODE,
        mode_label="EMS mode",
        sub_decode_role="forced_cmd_select",
        sub_decode_map=_SUB_DECODE,
    )
