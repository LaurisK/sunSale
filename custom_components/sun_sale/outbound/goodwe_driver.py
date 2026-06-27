"""GoodWe control driver (entity-driven).

Targets the **mletenay/home-assistant-goodwe-inverter** integration (a superset
of HA core ``goodwe``), which exposes the operation-mode select plus the Eco /
Peak-shaving power+SoC numbers.

=============================  OBSERVATIONS — UNTESTED  =========================
Implemented from the integration's operation-mode scheme; **not validated
against hardware**.

Control surface used
--------------------
  * ``operation_mode_select`` — ``select.*_operation_mode``: "General mode",
    "Off grid mode", "Backup mode", "Eco mode", "Eco charge mode",
    "Eco discharge mode", "Peak shaving mode".
  * ``eco_power`` — Eco-mode (dis)charge power setpoint (W).
  * ``export_limit`` — ``number.*_grid_export_limit`` (W). 0 = NoExport.

Mode mapping rationale
----------------------
  * ``SelfUse`` / ``FeedIn`` → "General mode". GoodWe General self-consumes and
    exports surplus; there is no distinct feed-in-priority mode, so both map to
    General (FeedIn just opens the export limit). They are indistinguishable to
    ``decode_observed``.
  * ``GridCharge`` → "Eco charge mode" + ``eco_power`` = max charge W. Eco-charge
    draws from grid to hit the configured power/SoC.
  * ``Discharge`` → "Eco discharge mode" + ``eco_power`` = inverter max + export
    limit opened.

Known gaps / weak spots
-----------------------
  * **No battery-hold primitive.** ``StandBy`` is approximated as "Eco charge
    mode" with ``eco_power`` = 0 (battery idle, no forced flow). CONFIRM that
    zero eco power truly idles rather than erroring on hardware; if not, fall
    back to "General mode" and accept some battery activity.
  * **Eco modes carry an SoC bound** (start/stop SoC) not modelled here — the
    eco charge/discharge will halt at the configured SoC limits regardless of
    what sunSale commands. Wire ``eco_soc`` numbers (0/100) into the plans if
    your install's defaults fight the dispatcher.
  * Some firmwares store operation mode in EEPROM; rapid switching may wear it.
    The actuator's idempotent skip mitigates, but avoid per-cycle thrashing.
  * Core ``goodwe`` (vs mletenay's fork) exposes fewer modes; this driver assumes
    the fork's fuller select. Roles are resolved via the manual mapping form
    until a config-entry resolver exists.
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

_OP_GENERAL = "General mode"
_OP_ECO_CHARGE = "Eco charge mode"
_OP_ECO_DISCHARGE = "Eco discharge mode"


def _build_plans(
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, ControlPlan]:
    """Return the StorageMode → :class:`ControlPlan` table for GoodWe.

    Args:
        battery_config: Battery limits → eco-charge power.
        export_limit_w: Export cap for exporting modes / zero for NoExport.
        inverter_max_power_w: Eco-discharge power magnitude.

    Returns:
        Plan table covering every dispatchable StorageMode.
    """
    charge_w = int(battery_config.max_charge_power_kw * 1000)

    def export_cap(w: int) -> EntityWrite:
        return number_write("export_limit", w, "Export limit (W)")

    def eco_power(w: int) -> EntityWrite:
        return number_write("eco_power", w, "Eco mode power (W)")

    return {
        StorageMode.SelfUse: ControlPlan(writes=(
            EntityWrite("operation_mode_select", "select", _OP_GENERAL, "Operation mode"),
            export_cap(export_limit_w),
        )),
        StorageMode.FeedIn: ControlPlan(writes=(
            EntityWrite("operation_mode_select", "select", _OP_GENERAL, "Operation mode"),
            export_cap(export_limit_w),
        )),
        StorageMode.NoExport: ControlPlan(writes=(
            EntityWrite("operation_mode_select", "select", _OP_GENERAL, "Operation mode"),
            export_cap(0),
        )),
        # No native hold — idle the battery via a zero-power eco charge.
        StorageMode.StandBy: ControlPlan(writes=(
            EntityWrite("operation_mode_select", "select", _OP_ECO_CHARGE, "Operation mode"),
            eco_power(0),
        )),
        StorageMode.GridCharge: ControlPlan(writes=(
            EntityWrite("operation_mode_select", "select", _OP_ECO_CHARGE, "Operation mode"),
            eco_power(charge_w),
        )),
        StorageMode.Discharge: ControlPlan(writes=(
            EntityWrite("operation_mode_select", "select", _OP_ECO_DISCHARGE, "Operation mode"),
            eco_power(inverter_max_power_w),
            export_cap(export_limit_w),
        )),
    }


_DECODE: dict[str, StorageMode] = {
    _OP_GENERAL: StorageMode.SelfUse,
    _OP_ECO_CHARGE: StorageMode.GridCharge,
    _OP_ECO_DISCHARGE: StorageMode.Discharge,
}


def make_goodwe_driver(
    context: InverterContext,
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> EntityControlDriver:
    """Build the GoodWe entity-driven control driver.

    Args:
        context: HA handle, role→entity-id map, and grid-power reader.
        battery_config: Battery limits for eco-charge power.
        export_limit_w: Export cap.
        inverter_max_power_w: Eco-discharge magnitude.

    Returns:
        An :class:`EntityControlDriver` configured for GoodWe.
    """
    return EntityControlDriver.from_context(
        context,
        platform=InverterPlatform.GOODWE,
        plans=_build_plans(battery_config, export_limit_w, inverter_max_power_w),
        mode_role="operation_mode_select",
        decode_map=_DECODE,
        mode_label="Operation mode",
    )
