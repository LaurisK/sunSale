"""Resolve the full inverter entity-ID mapping for coordinator setup.

This is the platform-neutral front door to entity resolution. The per-role
role→entity discovery is delegated to the platform's registered
:class:`~.inverter_discovery.InverterEntityDiscovery` strategy (via
:func:`~.inverter_discovery.discovery_for`) — Solis auto-detect / manual,
generic manual, … — so this module branches on no vendor. On top of that
``inverter_entity_ids`` dict it pins the observer-pipeline entity IDs
(grid-power directions, today-total counters, PV/solar energy, AC-port/backup
power) using a *manual-config-first, auto-detect-second* merge, and maps
auto-detected yesterday-total entities into ``raw_config`` so
``yesterday_total_resolver`` can find them.

Keeping this maze out of the coordinator leaves ``async_setup`` reading as the
thin wiring step it is meant to be.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant

from ..contract.const import (
    CONF_INVERTER_ENTITY_AC_PORT_POWER,
    CONF_INVERTER_ENTITY_BACKUP_POWER,
    CONF_INVERTER_ENTITY_GENERATION_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_EXPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_GRID_IMPORT_POWER,
    CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY,
    CONF_INVERTER_ENTITY_PV_POWER,
    CONF_INVERTER_ENTITY_SOLAR_ENERGY,
    CONF_INVERTER_PLATFORM,
)
from ..outbound.inverter import InverterPlatform
from .inverter_discovery import discovery_for


@dataclass(frozen=True)
class ResolvedInverterEntities:
    """Fully resolved inverter entity IDs for coordinator wiring.

    Attributes:
        platform: The configured inverter platform.
        inverter_entity_ids: Telemetry + control role → entity-ID mapping
            passed to ``InverterController`` (and mirrored for the debug view).
        grid_import_power / grid_export_power: Per-direction grid-power sensor
            entity IDs; empty when only the signed net sensor is available.
        signed_grid_power: The signed net grid-power sensor entity ID.
        grid_import_total / grid_export_total: Daily-resetting grid-energy
            counter entity IDs.
        pv_power: Instantaneous PV-power sensor entity ID.
        solar_energy_today: Daily-resetting solar-energy counter entity ID.
        ac_port_power / backup_power: Derived-observer input entity IDs.
    """

    platform: InverterPlatform
    inverter_entity_ids: dict[str, str]
    grid_import_power: str
    grid_export_power: str
    signed_grid_power: str
    grid_import_total: str
    grid_export_total: str
    pv_power: str
    solar_energy_today: str
    ac_port_power: str
    backup_power: str


def _resolve_role_ids(hass: HomeAssistant, data: dict) -> dict[str, str]:
    """Build the telemetry + control role → entity-ID mapping for the platform.

    Delegates to the platform's registered :class:`InverterEntityDiscovery`
    strategy (Solis auto-detect / manual, generic manual, …) selected by
    :func:`..inverter_discovery.discovery_for`. This front door no longer knows
    any vendor specifics — a new platform registers its own strategy.

    Args:
        hass: Home Assistant instance, used for any registry scan.
        data: Merged config-entry data + options.

    Returns:
        Role-keyed entity-ID dict ready for ``InverterController``.
    """
    platform = InverterPlatform(data[CONF_INVERTER_PLATFORM])
    return discovery_for(platform).resolve_role_ids(hass, data)


def resolve_inverter_entities(
    hass: HomeAssistant, data: dict,
) -> ResolvedInverterEntities:
    """Resolve every inverter entity ID the coordinator needs from config.

    Builds the role mapping for ``InverterController`` and pins the
    observer-pipeline entity IDs, merging manual config first and Solis
    auto-detect second. Auto-detected yesterday-total entities are written
    back into ``data`` under the keys ``yesterday_total_resolver`` expects.

    Args:
        hass: Home Assistant instance.
        data: Merged config-entry data + options. **Mutated in place** to add
            any auto-detected yesterday-total entity IDs.

    Returns:
        A :class:`ResolvedInverterEntities` with the role mapping and every
        observer-pipeline entity ID.
    """
    inverter_platform = InverterPlatform(data[CONF_INVERTER_PLATFORM])
    inverter_entity_ids = _resolve_role_ids(hass, data)

    # Per-direction grid-power observers — prefer the per-direction
    # entity when configured. When absent (typical for Solis auto-detect,
    # where solis_modbus only publishes signed ``grid_power_net``), the
    # observers fall back to the signed sensor and project onto their
    # side via ``_signed_polarity``. The signed ``grid_power`` entity
    # also remains in ``inverter_entity_ids`` for
    # ``InverterController.get_grid_power`` (used by BatteryReading /
    # capacity estimator), independent of the observer pipeline.
    grid_import_power = data.get(CONF_INVERTER_ENTITY_GRID_IMPORT_POWER, "")
    grid_export_power = data.get(CONF_INVERTER_ENTITY_GRID_EXPORT_POWER, "")
    signed_grid_power = inverter_entity_ids.get("grid_power", "")
    grid_import_total = inverter_entity_ids.get("grid_import_energy_today", "")
    grid_export_total = inverter_entity_ids.get("grid_export_energy_today", "")
    # PV power + today-solar-energy: prefer the manual-config value when
    # set; fall back to the Solis resolver's auto-detected entity. The
    # resolver omits the role entirely when not present, so dict.get
    # with empty-string default is the right merge.
    pv_power = (
        data.get(CONF_INVERTER_ENTITY_PV_POWER, "")
        or inverter_entity_ids.get("pv_power", "")
    )
    solar_energy_today = (
        data.get(CONF_INVERTER_ENTITY_SOLAR_ENERGY, "")
        or inverter_entity_ids.get("solar_energy_today", "")
    )
    # Derived-observer inputs — same manual-first / Solis-auto-second merge.
    # Both are optional; absence simply disables the consumption + losses
    # observed series. The grid-net and battery/PV signals already arrive
    # via the existing translators, so only AC port + backup are new
    # entities to wire through.
    ac_port_power = (
        data.get(CONF_INVERTER_ENTITY_AC_PORT_POWER, "")
        or inverter_entity_ids.get("ac_port_power", "")
    )
    backup_power = (
        data.get(CONF_INVERTER_ENTITY_BACKUP_POWER, "")
        or inverter_entity_ids.get("backup_power", "")
    )
    # Yesterday-total entities: map auto-detected Solis entries into the
    # raw-config dict so ``yesterday_total_resolver`` finds them via its
    # ``DEDICATED_ENTITY_CONFIG_KEY`` lookup without further plumbing.
    for cfg_key, role_key in (
        (CONF_INVERTER_ENTITY_GENERATION_YESTERDAY, "solar_energy_yesterday"),
        (CONF_INVERTER_ENTITY_GRID_IMPORT_YESTERDAY, "grid_import_energy_yesterday"),
        (CONF_INVERTER_ENTITY_GRID_EXPORT_YESTERDAY, "grid_export_energy_yesterday"),
    ):
        if not data.get(cfg_key) and inverter_entity_ids.get(role_key):
            data[cfg_key] = inverter_entity_ids[role_key]

    return ResolvedInverterEntities(
        platform=inverter_platform,
        inverter_entity_ids=inverter_entity_ids,
        grid_import_power=grid_import_power,
        grid_export_power=grid_export_power,
        signed_grid_power=signed_grid_power,
        grid_import_total=grid_import_total,
        grid_export_total=grid_export_total,
        pv_power=pv_power,
        solar_energy_today=solar_energy_today,
        ac_port_power=ac_port_power,
        backup_power=backup_power,
    )
