"""Household-consumption stage: read the today-total kWh counter sensor.

Mirrors `inbound/generation.py` but for the household-load energy counter
(e.g. `sensor.namai_inv_household_load_today_energy_2`). The sensor is a
cumulative kWh counter that resets at local midnight; this translator only
takes a snapshot per cycle so downstream consumers can display
"consumption so far today" without re-deriving it from instantaneous
load samples.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..contract.models import HouseholdConsumptionReading, SunSaleConfig
from ..ha_state import read_float_state


class HouseholdConsumptionTranslator:
    """Reads the household-consumption today-total sensor; produces HouseholdConsumptionReading."""

    output_type = HouseholdConsumptionReading

    def __init__(self, entity_id: str) -> None:
        """Initialise with the HA entity ID of the household-consumption today-total sensor.

        Args:
            entity_id: Entity ID of the cumulative today-total kWh sensor.
        """
        self._entity_id = entity_id

    def parse(
        self, hass: Any, now: datetime | None = None
    ) -> HouseholdConsumptionReading | None:
        """Read the household-consumption today-total sensor and return a snapshot.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            HouseholdConsumptionReading with today_total_kwh, or None when unavailable.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        value = read_float_state(hass, self._entity_id)
        if value is None:
            return None
        return HouseholdConsumptionReading(today_total_kwh=value, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> HouseholdConsumptionReading | None:
        """DAG translator entry-point; delegates to parse().

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            HouseholdConsumptionReading or None when unavailable.
        """
        return self.parse(hass, now)
