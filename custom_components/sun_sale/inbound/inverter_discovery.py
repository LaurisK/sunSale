"""Vendor-pluggable inverter entity discovery.

Entity discovery — turning the config entry into the role→entity-ID map
``InverterController`` consumes — is the one remaining vendor-specific seam on
the read side. This module makes it pluggable: each platform supplies an
:class:`InverterEntityDiscovery` strategy, and :func:`discovery_for` selects it
from a registry. The front door
(:func:`..inverter_entity_resolver.resolve_inverter_entities`) delegates here and
no longer branches on platform, so adding a vendor is a new strategy class plus
one :func:`register_discovery` call — never an edit to the front door.

The built-in strategies are :class:`..solis_entity_resolver.SolisEntityDiscovery`
(auto-detect + manual fallback) and :class:`GenericEntityDiscovery` (the
telemetry-only manual mapping, also the default for the not-yet-implemented
Huawei / SolarEdge / GoodWe platforms).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from homeassistant.core import HomeAssistant

from ..contract.const import (
    CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY,
    CONF_INVERTER_ENTITY_GRID_POWER,
)
from ..outbound.inverter import InverterPlatform
from .solis_entity_resolver import SolisEntityDiscovery


@runtime_checkable
class InverterEntityDiscovery(Protocol):
    """Resolve a platform's telemetry + control role → entity-ID mapping."""

    def resolve_role_ids(self, hass: HomeAssistant, data: dict) -> dict[str, str]:
        """Return the role-keyed entity-ID dict for ``InverterController``.

        Args:
            hass: Home Assistant instance (for any registry scan).
            data: Merged config-entry data + options.

        Returns:
            Role → entity-ID mapping; roles a platform does not expose are
            simply omitted.
        """
        ...


class GenericEntityDiscovery:
    """Telemetry-only manual mapping for platforms with no auto-detect.

    Reads the user-mapped telemetry entities straight from config. Control
    roles are absent — ``apply_mode`` is a no-op on these platforms — so the
    map carries only the reads the pipeline needs. Used for the ``GENERIC``
    platform and as the default for any platform without a registered strategy.
    """

    def resolve_role_ids(self, hass: HomeAssistant, data: dict) -> dict[str, str]:
        """Return the manual telemetry role mapping from config data."""
        return {
            "battery_soc": data[CONF_INVERTER_ENTITY_BATTERY_SOC],
            "battery_power": data[CONF_INVERTER_ENTITY_BATTERY_POWER],
            "grid_power": data[CONF_INVERTER_ENTITY_GRID_POWER],
            "charge_control": data[CONF_INVERTER_ENTITY_CHARGE_CONTROL],
            "grid_import_energy_today":
                data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_ENERGY, ""),
            "grid_export_energy_today":
                data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_ENERGY, ""),
            "battery_charge_energy_today":
                data.get(CONF_INVERTER_ENTITY_BATTERY_CHARGE_ENERGY, ""),
            "battery_discharge_energy_today":
                data.get(CONF_INVERTER_ENTITY_BATTERY_DISCHARGE_ENERGY, ""),
        }


_GENERIC = GenericEntityDiscovery()

# Per-platform discovery strategies. Platforms absent here fall back to the
# generic telemetry-only mapping (the not-yet-implemented vendors).
_DISCOVERY: dict[InverterPlatform, InverterEntityDiscovery] = {
    InverterPlatform.SOLIS: SolisEntityDiscovery(),
    InverterPlatform.GENERIC: _GENERIC,
}

# The entity-driven platforms each resolve through one generic profile-backed
# strategy (``inbound/platform_profiles.py``) — auto-detect from the vendor's
# config entry when present, manual mapping otherwise. Registered here so the
# front door stays vendor-neutral. Untested against hardware: the role patterns
# are the debug surface, not this wiring.
from .platform_profiles import ProfileEntityDiscovery, profile_for  # noqa: E402

for _platform in (
    InverterPlatform.HUAWEI_SOLAR,
    InverterPlatform.SOLAX,
    InverterPlatform.SUNGROW,
    InverterPlatform.GOODWE,
    InverterPlatform.DEYE,
):
    _profile = profile_for(_platform)
    if _profile is not None:
        _DISCOVERY[_platform] = ProfileEntityDiscovery(_profile)


def register_discovery(
    platform: InverterPlatform, discovery: InverterEntityDiscovery
) -> None:
    """Register (or override) the discovery strategy for ``platform``.

    The extension hook for a new vendor: implement
    :class:`InverterEntityDiscovery` and register it, without touching the
    front door.

    Args:
        platform: The platform the strategy serves.
        discovery: The strategy instance.
    """
    _DISCOVERY[platform] = discovery


def discovery_for(platform: InverterPlatform) -> InverterEntityDiscovery:
    """Return the discovery strategy for ``platform``.

    Args:
        platform: The configured inverter platform.

    Returns:
        The registered strategy, or the generic telemetry-only mapping when the
        platform has none.
    """
    return _DISCOVERY.get(platform, _GENERIC)
