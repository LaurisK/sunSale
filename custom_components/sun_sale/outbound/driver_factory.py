"""The platform dispatch seam — selects the control driver for a platform.

This is the one place that knows the full set of inverter platforms and which
concrete :class:`..outbound.driver.InverterControlDriver` each maps to. It lives
in its own module (rather than inside any one platform's file) so the neutral
dispatch point does not depend on — or read as "belonging to" — a single
vendor. Adding a platform is a new branch here plus its ``*_driver.py``; nothing
else changes.

Each entity-driven branch is imported lazily so selecting Solis never imports
the (untested-against-hardware) entity-driven platform modules, and vice versa.
"""
from __future__ import annotations

import importlib

from ..contract.models import BatteryConfig
from .driver import InverterControlDriver
from .entity_control import InverterContext
from .inverter import InverterController, InverterPlatform
from .solis_driver import SolisDriver

# Platform → (relative module, factory function name) for the entity-driven
# drivers. Looked up lazily (see ``make_inverter_driver``) so selecting one
# platform never imports the others' untested-against-hardware modules. Adding a
# platform is a new row here plus its ``*_driver.py``; nothing else changes.
_ENTITY_DRIVER_FACTORIES: dict[InverterPlatform, tuple[str, str]] = {
    InverterPlatform.HUAWEI_SOLAR: (".huawei_driver", "make_huawei_driver"),
    InverterPlatform.SOLAX: (".solax_driver", "make_solax_driver"),
    InverterPlatform.SUNGROW: (".sungrow_driver", "make_sungrow_driver"),
    InverterPlatform.GOODWE: (".goodwe_driver", "make_goodwe_driver"),
    InverterPlatform.DEYE: (".deye_driver", "make_deye_driver"),
}


def make_inverter_driver(
    inverter: InverterController,
    context: InverterContext,
    platform: InverterPlatform,
    battery_config: BatteryConfig,
    export_limit_w: int,
    inverter_max_power_w: int,
) -> InverterControlDriver:
    """Return the control driver for ``platform``.

    The single seam where a new inverter platform plugs in. Solis returns the
    register-level :class:`..outbound.solis_driver.SolisDriver` (wrapping the
    full ``inverter`` controller); Huawei / SolaX / Sungrow / GoodWe / Deye
    return entity-driven :class:`..outbound.entity_control.EntityControlDriver`
    instances built from the neutral ``context`` alone (implemented but
    **untested against hardware** — see each ``*_driver.py`` module). Fronius /
    SMA / Kostal (notes-only) and every other platform fall back to a
    telemetry-only :class:`..outbound.solis_driver.SolisDriver` wrapper, whose
    ``apply_mode`` no-ops on the non-Solis controller — preserving the prior
    "read but don't write" behaviour rather than risking a wrong write.

    Args:
        inverter: The Solis register read / write source (used only by the
            register-level / telemetry-only :class:`..outbound.solis_driver.SolisDriver`
            path).
        context: The platform-neutral HA handle + entity-map + grid-power reader
            the entity-driven drivers build their actuator from.
        platform: The configured inverter platform.
        battery_config: Battery limits for spec derivation.
        export_limit_w: Backflow / export cap.
        inverter_max_power_w: Rated AC output for RC / forced-discharge magnitude.

    Returns:
        A control driver implementing
        :class:`..outbound.driver.InverterControlDriver`.
    """
    factory = _ENTITY_DRIVER_FACTORIES.get(platform)
    if factory is not None:
        module_name, func_name = factory
        make = getattr(importlib.import_module(module_name, __package__), func_name)
        return make(context, battery_config, export_limit_w, inverter_max_power_w)
    # Solis (register-level) and every notes-only / telemetry-only platform.
    base = SolisDriver(
        inverter, battery_config, export_limit_w, inverter_max_power_w,
        hass=context.hass,
    )
    if platform is not InverterPlatform.SOLIS:
        return base
    # Remote Dispatch is strictly better for the forced modes (inverter-side
    # failsafe instead of a ~5-min RC expiry, raw register writes instead of
    # bounded entity writes), but not every install has it. The wrapper is
    # therefore always built and resolves support **lazily** — deciding here
    # would race ``solis_modbus``'s own startup (services are registered during
    # its setup, which HA may run after ours) and pin a capable install to the
    # RC path until the next restart. See ``SolisDispatchDriver._supported``.
    from .solis_dispatch_driver import SolisDispatchDriver

    return SolisDispatchDriver(base, context.hass, dict(context.entity_ids))
