"""InverterModeTranslator — reads the inverter's current StorageMode each cycle.

Thin adapter that delegates to the platform driver's ``observe`` to produce an
``InverterModeReading`` (a primary type injected into ``NodeContext`` by the
coordinator). The driver owns the platform-specific reading + decoding, so this
module carries no register knowledge. The decoded ``StorageMode`` feeds two
consumers:

  - the persistent ``InverterModeHistory`` rolling log (managed by
    ``outbound/inverter_control_module.py``);
  - the diagnostic ``inverter_mode_now`` sensor attribute (current target vs
    observed).

The driver is resilient to missing or unparseable entities — any field it
cannot read becomes ``None`` and the decoded mode collapses to
``StorageMode.UNKNOWN``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contract.models import InverterModeReading, SunSaleConfig
from ..outbound.driver import InverterControlDriver


class InverterModeTranslator:
    """Reads the inverter's StorageMode signals and produces an InverterModeReading."""

    output_type = InverterModeReading

    def __init__(self, driver: InverterControlDriver) -> None:
        """Initialise with the platform control driver.

        Args:
            driver: Control driver whose ``observe`` reads and decodes the
                current mode (the platform-specific register knowledge lives
                there, not here).
        """
        self._driver = driver

    def parse(self, hass: Any, now: datetime) -> InverterModeReading:
        """Read and decode the current mode via the driver.

        ``hass`` is accepted for parity with other translators but is not used
        directly here — all reads go through the driver.

        Args:
            hass: Home Assistant instance (unused here).
            now: Cycle timestamp, used as ``InverterModeReading.timestamp``.

        Returns:
            An ``InverterModeReading`` capturing the current observed state.
        """
        del hass  # reads are routed through the driver
        return self._driver.observe(now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> InverterModeReading:
        """Async wrapper around ``parse`` so this slots into ``run_translators``.

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            ``InverterModeReading`` for this cycle.
        """
        del config, raw_config
        return self.parse(hass, now)
