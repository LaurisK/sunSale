"""Deye / Sunsynk control driver (entity-driven).

Targets the Solarman-style modbus integrations (StephanJoubert/home_assistant_
solarman, kellerza/sunsynk) that expose Deye/Sunsynk work-mode + time-of-use
control as HA entities.

=============================  OBSERVATIONS — UNTESTED  =========================
Implemented from the Deye/Sunsynk register scheme; **not validated against
hardware**. Of the five entity-driven platforms this is the **lowest
confidence** — see the structural caveat below.

Structural caveat — Deye has no single force-charge/discharge command
---------------------------------------------------------------------
Deye real-time battery forcing is done through the **six time-of-use slots**
(each slot has a target SoC, a power, and a grid-charge enable bit). There is no
atomic "force charge now at X W" command like Sungrow/Huawei. A faithful
implementation would rewrite the *currently active* TOU slot every cycle, which
needs slot-index arithmetic against the inverter clock. This driver instead
models the coarse global controls (work-mode + a grid-charge switch + a sell-
power number); it WILL need a TOU-slot rewrite layer before GridCharge /
Discharge behave precisely. Treat GridCharge/Discharge here as placeholders.

Control surface used
--------------------
  * ``work_mode_select`` — "Energy management" / work mode: "Selling First",
    "Zero Export To Load", "Zero Export To CT", "Limited to Load".
  * ``grid_charge_switch`` — global grid-charge enable (proxy for the per-slot
    grid-charge bits).
  * ``max_sell_power`` — max export/sell power (W).

Mode mapping rationale (approximate)
------------------------------------
  * ``SelfUse`` → "Zero Export To CT" (self-use, meter at the grid CT).
  * ``FeedIn`` → "Selling First" (export surplus).
  * ``NoExport`` → "Zero Export To Load".
  * ``StandBy`` → work mode left at self-use + grid charge off. Deye has no true
    discharge-bar; the battery may still cover load. Approximate.
  * ``GridCharge`` → grid-charge switch on (+ self-use). Real power/SoC must come
    from the active TOU slot — NOT set here.
  * ``Discharge`` → "Selling First" + ``max_sell_power`` opened to inverter max.
    Deye exports battery under Selling First; this is the closest native
    behaviour to forced discharge.

Known gaps / weak spots
-----------------------
  * Option labels differ substantially between Solarman profiles and the kellerza
    sunsynk add-on — CONFIRM against your specific integration.
  * ``decode_observed`` keys off ``work_mode_select`` only; grid-charge and the
    TOU state are invisible to it.
  * No config-entry resolver; roles via manual mapping form.
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

_WM_SELLING_FIRST = "Selling First"
_WM_ZERO_EXPORT_LOAD = "Zero Export To Load"
_WM_ZERO_EXPORT_CT = "Zero Export To CT"


def _build_plans(
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, ControlPlan]:
    """Return the StorageMode → :class:`ControlPlan` table for Deye/Sunsynk.

    Args:
        battery_config: Battery limits (unused — Deye power lives in TOU slots).
        export_limit_w: Sell-power cap for exporting modes.
        inverter_max_power_w: Forced-discharge (Selling First) sell-power cap.

    Returns:
        Plan table covering every dispatchable StorageMode (GridCharge /
        Discharge are approximate — see module observations).
    """
    del battery_config  # Deye charge/discharge power is per-TOU-slot

    def sell_power(w: int) -> EntityWrite:
        return number_write("max_sell_power", w, "Max sell power (W)")

    return {
        StorageMode.SelfUse: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_ZERO_EXPORT_CT, "Work mode"),
            sell_power(export_limit_w),
        )),
        StorageMode.FeedIn: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_SELLING_FIRST, "Work mode"),
            sell_power(export_limit_w),
        )),
        StorageMode.NoExport: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_ZERO_EXPORT_LOAD, "Work mode"),
            sell_power(0),
        )),
        StorageMode.StandBy: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_ZERO_EXPORT_CT, "Work mode"),
            EntityWrite("grid_charge_switch", "switch", False, "Grid charge"),
        )),
        StorageMode.GridCharge: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_ZERO_EXPORT_CT, "Work mode"),
            EntityWrite("grid_charge_switch", "switch", True, "Grid charge"),
        )),
        StorageMode.Discharge: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_SELLING_FIRST, "Work mode"),
            sell_power(inverter_max_power_w),
        )),
    }


_DECODE: dict[str, StorageMode] = {
    _WM_ZERO_EXPORT_CT: StorageMode.SelfUse,
    _WM_ZERO_EXPORT_LOAD: StorageMode.NoExport,
    _WM_SELLING_FIRST: StorageMode.FeedIn,
}


def make_deye_driver(
    context: InverterContext,
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> EntityControlDriver:
    """Build the Deye/Sunsynk entity-driven control driver.

    Args:
        context: HA handle, role→entity-id map, and grid-power reader.
        battery_config: Battery limits (forwarded for symmetry).
        export_limit_w: Sell-power cap.
        inverter_max_power_w: Forced-discharge sell-power cap.

    Returns:
        An :class:`EntityControlDriver` configured for Deye/Sunsynk.
    """
    return EntityControlDriver.from_context(
        context,
        platform=InverterPlatform.DEYE,
        plans=_build_plans(battery_config, export_limit_w, inverter_max_power_w),
        mode_role="work_mode_select",
        decode_map=_DECODE,
        mode_label="Work mode",
    )
