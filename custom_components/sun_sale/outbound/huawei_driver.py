"""Huawei SUN2000 + LUNA2000 control driver (entity-driven).

Targets the community **wlcrs/huawei_solar** integration (HA config entry domain
``huawei_solar``), which exposes battery control once "Elevate permissions" is
selected at setup and the inverter runs 2023-or-later firmware.

=============================  OBSERVATIONS — UNTESTED  =========================
Implemented from the integration's documented control surface; **not validated
against hardware** (sunSale's author runs Solis). Treat every option string and
role name below as needing confirmation against a live install.

Control surface used
--------------------
  * ``work_mode_select`` — ``select.*_working_mode``. Observed option labels
    vary by firmware/locale; the decode map below assumes the English
    "Maximise Self Consumption" / "Fully fed to grid" / "Time Of Use ..."
    labels. CONFIRM the exact strings on hardware.
  * ``charge_from_grid_switch`` — ``switch.*_charge_from_grid`` (a.k.a. "grid
    charge"): gates whether forced/TOU charging may pull from the grid.
  * ``max_feed_grid_power`` — ``number.*_maximum_feed_grid_power_w`` (export
    cap). Set to 0 for NoExport.
  * Services ``huawei_solar.forcible_charge`` / ``forcible_discharge`` (power W
    + duration min) and ``stop_forcible_charge``. These target the **battery
    device** by ``device_id`` — role ``battery_device`` must resolve to a
    device id, not an entity id (the only role here injected as ``device_id``).

Known gaps / weak spots
-----------------------
  * **No battery-hold primitive.** ``StandBy`` is approximated as Maximise Self
    Consumption + ``stop_forcible_charge``; Huawei will still discharge to cover
    load. A truer hold needs a TOU schedule with a zero-power window.
  * **Forced modes are not reflected in ``working_mode``.** ``forcible_charge`` /
    ``forcible_discharge`` run *underneath* the selected working mode, so
    ``decode_observed`` reports the base mode (SelfUse/FeedIn), not GridCharge/
    Discharge. The verify loop therefore only checks the working-mode select +
    export number for forced modes, not that the forcible command actually
    engaged — there is no clean readback entity for the forcible state. Consider
    adding the "Forcible charge/discharge" sensor as a decode discriminator once
    its exact entity + states are confirmed.
  * **Keepalive (handled).** ``forcible_charge`` / ``forcible_discharge`` take a
    duration and stop when it lapses. GridCharge / Discharge therefore carry
    ``keepalive=True``: the control module re-issues the service from
    ``refresh_rc`` every RC keep-alive tick while the mode is held, so a slot
    longer than the duration never silently stops forcing. The requested
    duration is deliberately short (a deadman — see ``_FORCIBLE_DURATION_MIN``)
    so the inverter reverts on its own if sunSale stops dispatching. The one
    residual gap is *verification*: the forcible state has no clean readback
    entity, so the verify loop confirms only the working mode + switches, not
    that the forcible command engaged — continuous re-issue is the mitigation.
  * Entity resolution for the ``huawei_solar`` config entry is **not yet wired**
    (no resolver in ``inbound/``); until it is, the roles above must be filled
    via the manual mapping form. ``apply_mode`` degrades to logged skips for any
    unmapped role.
================================================================================
"""
from __future__ import annotations

from ..contract.models import BatteryConfig, StorageMode
from .entity_control import (
    ControlPlan,
    EntityControlDriver,
    EntityWrite,
    InverterContext,
    ServiceWrite,
    number_write,
)
from .inverter import InverterPlatform

# Working-mode select option labels (CONFIRM on hardware — locale-dependent).
_WM_SELF_CONSUMPTION = "Maximise Self Consumption"
_WM_FULLY_FED = "Fully fed to grid"

# Duration requested for a forcible charge/discharge. This is a *deadman*, not a
# slot length: the GridCharge / Discharge plans carry ``keepalive=True``, so the
# control module re-issues the service every RC keep-alive tick (~3 min) while
# the mode is held — the command never lapses mid-slot. Keeping the duration
# short (well above the re-issue cadence, far below a slot) bounds how long the
# inverter keeps forcing if sunSale stops dispatching entirely (crash / restart),
# mirroring the Solis RC timeout's fail-safe.
_FORCIBLE_DURATION_MIN = 10


def _build_plans(
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> dict[StorageMode, ControlPlan]:
    """Return the StorageMode → :class:`ControlPlan` table for Huawei.

    Args:
        battery_config: Battery limits (charge/discharge power → forcible W).
        export_limit_w: Export cap applied for exporting modes.
        inverter_max_power_w: Forced-discharge power magnitude.

    Returns:
        Plan table covering every dispatchable StorageMode.
    """
    charge_w = int(battery_config.max_charge_power_kw * 1000)

    def export_cap(w: int) -> EntityWrite:
        return number_write("max_feed_grid_power", w, "Max feed-in power (W)")

    return {
        StorageMode.SelfUse: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_SELF_CONSUMPTION, "Working mode"),
            export_cap(export_limit_w),
        )),
        StorageMode.FeedIn: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_FULLY_FED, "Working mode"),
            export_cap(export_limit_w),
        )),
        StorageMode.NoExport: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_SELF_CONSUMPTION, "Working mode"),
            export_cap(0),
        )),
        # No native hold — approximate, and stop any running forcible command.
        StorageMode.StandBy: ControlPlan(
            writes=(EntityWrite("work_mode_select", "select", _WM_SELF_CONSUMPTION, "Working mode"),),
            services=(ServiceWrite(
                "huawei_solar", "stop_forcible_charge",
                target_role="battery_device", target_key="device_id",
                label="Stop forcible charge/discharge",
            ),),
        ),
        StorageMode.GridCharge: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_SELF_CONSUMPTION, "Working mode"),
            EntityWrite("charge_from_grid_switch", "switch", True, "Charge from grid"),
        ), services=(ServiceWrite(
            "huawei_solar", "forcible_charge",
            data={"power": charge_w, "duration": _FORCIBLE_DURATION_MIN},
            target_role="battery_device", target_key="device_id",
            label="Forcible charge",
        ),), keepalive=True),
        StorageMode.Discharge: ControlPlan(writes=(
            EntityWrite("work_mode_select", "select", _WM_SELF_CONSUMPTION, "Working mode"),
            export_cap(export_limit_w),
        ), services=(ServiceWrite(
            "huawei_solar", "forcible_discharge",
            data={"power": inverter_max_power_w, "duration": _FORCIBLE_DURATION_MIN},
            target_role="battery_device", target_key="device_id",
            label="Forcible discharge",
        ),), keepalive=True),
    }


# Working-mode readback → StorageMode. Forced modes collapse to their base
# working mode (see module gaps); only the persistent modes round-trip.
_DECODE: dict[str, StorageMode] = {
    _WM_SELF_CONSUMPTION: StorageMode.SelfUse,
    _WM_FULLY_FED: StorageMode.FeedIn,
}


def make_huawei_driver(
    context: InverterContext,
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> EntityControlDriver:
    """Build the Huawei entity-driven control driver.

    Args:
        context: HA handle, role→entity-id map, and grid-power reader.
        battery_config: Battery limits for forcible-power derivation.
        export_limit_w: Export cap for exporting modes.
        inverter_max_power_w: Forced-discharge power magnitude.

    Returns:
        An :class:`EntityControlDriver` configured for Huawei.
    """
    return EntityControlDriver.from_context(
        context,
        platform=InverterPlatform.HUAWEI_SOLAR,
        plans=_build_plans(battery_config, export_limit_w, inverter_max_power_w),
        mode_role="work_mode_select",
        decode_map=_DECODE,
        mode_label="Working mode",
    )
