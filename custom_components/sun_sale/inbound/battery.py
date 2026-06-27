"""Battery stage: BatteryReading HA-edge reader + BatteryStatus assembly.

The `BatteryTranslator` reads inverter telemetry (SoC, battery power, grid
power) via `InverterController` plus the household-load sensor, producing a
`BatteryReading`. The `build_battery_status` function then combines that
live reading with the configured nominal capacity to produce a `BatteryStatus`
for downstream DAG nodes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contract.models import (
    BatteryConfig,
    BatteryReading,
    BatteryStatus,
    SunSaleConfig,
)
from ..ha_state import read_float_state
from ..outbound.inverter import InverterController
from .battery_source import BatterySource, InverterBatterySource

_DEFAULT_HOUSEHOLD_LOAD_KW = 0.2


def build_battery_status(
    reading: BatteryReading,
    config: BatteryConfig,
) -> BatteryStatus:
    """Combine live inverter telemetry with configured limits into a BatteryStatus snapshot.

    Args:
        reading: Current per-cycle inverter reading (SoC, power).
        config: Battery configuration (nominal capacity, power limits).

    Returns:
        BatteryStatus with remaining_capacity_kwh derived from SoC × nominal capacity.
    """
    return BatteryStatus(
        total_capacity_kwh=config.nominal_capacity_kwh,
        max_charge_power_kw=config.max_charge_power_kw,
        max_discharge_power_kw=config.max_discharge_power_kw,
        soc=reading.soc,
        remaining_capacity_kwh=reading.soc * config.nominal_capacity_kwh,
    )


# ---------------------------------------------------------------------------
# Battery translator (HA-edge reader)
# ---------------------------------------------------------------------------

def _read_household_load(hass: Any, entity_id: str) -> float:
    """Read household load from a HA power sensor in watts; returns kW.

    Falls back to _DEFAULT_HOUSEHOLD_LOAD_KW when the entity is absent,
    unavailable, or unparseable, so downstream consumers always receive a number.

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID of the household load sensor (watts); empty → fallback.

    Returns:
        Current household load in kW.
    """
    value = read_float_state(hass, entity_id)
    if value is None:
        return _DEFAULT_HOUSEHOLD_LOAD_KW
    return max(0.0, value) / 1000.0  # W → kW


class BatteryTranslator:
    """Reads inverter telemetry and household load; produces BatteryReading."""

    output_type = BatteryReading

    def __init__(
        self,
        inverter: InverterController,
        household_load_entity: str,
        battery_source: BatterySource | None = None,
    ) -> None:
        """Initialise translator with the inverter controller and load sensor entity.

        Args:
            inverter: Platform-agnostic inverter abstraction. Still the source
                of grid power (an inverter-side measurement).
            household_load_entity: HA entity ID of the household power sensor (watts).
            battery_source: Source for battery telemetry (SoC, power, energy).
                Defaults to :class:`InverterBatterySource` wrapping ``inverter``,
                preserving the previous inverter-only behaviour; a BMS-backed
                source can be injected instead.
        """
        self._inverter = inverter
        self._load_entity = household_load_entity
        self._battery = battery_source or InverterBatterySource(inverter)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> BatteryReading | None:
        """Read inverter telemetry and household load; produce a BatteryReading.

        Returns ``None`` when SoC is genuinely unavailable (a solis_modbus
        outage, an unmapped sensor). SoC is the one telemetry with no safe
        fabricated default — guessing it would let the scheduler plan, and the
        dispatcher act, against a fictional battery. Dropping the whole reading
        instead omits ``BatteryReading`` from the primary inputs, so the
        ``BatteryState`` / ``BatteryStatus`` nodes skip and the pipeline
        degrades to ``no_target`` rather than dispatching on invented state.

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp (unused here).

        Returns:
            BatteryReading with current SoC, power flows, and household load,
            or ``None`` when the SoC sensor is unavailable.
        """
        soc = self._battery.soc()
        if soc is None:
            return None
        # A missing power reading degrades to 0.0 (no flow) at the edge — unlike
        # SoC, it has a safe default and must not drop the whole reading.
        power_kw = self._battery.power_kw()
        if power_kw is None:
            power_kw = 0.0
        grid_kw = self._inverter.get_grid_power()
        load_kw = _read_household_load(hass, self._load_entity)
        return BatteryReading(
            soc=soc,
            power_kw=power_kw,
            grid_power_kw=grid_kw,
            household_load_kw=load_kw,
            charge_energy_today_kwh=self._battery.charge_energy_today_kwh(),
            discharge_energy_today_kwh=self._battery.discharge_energy_today_kwh(),
        )
