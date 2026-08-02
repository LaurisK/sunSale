"""Tests for inbound/battery.py — pure Python, no HA required."""
from datetime import UTC

from custom_components.sun_sale.contract.models import BatteryReading
from custom_components.sun_sale.inbound.battery import build_battery_status
from tests.conftest import default_battery_config


def _reading(soc: float = 0.5) -> BatteryReading:
    return BatteryReading(
        soc=soc,
        power_kw=0.0,
        grid_power_kw=0.0,
        household_load_kw=0.2,
    )


def test_total_capacity_from_config():
    status = build_battery_status(_reading(), default_battery_config())
    assert status.total_capacity_kwh == 10.0


def test_max_charge_power_from_config():
    status = build_battery_status(_reading(), default_battery_config())
    assert status.max_charge_power_kw == 5.0


def test_max_discharge_power_from_config():
    status = build_battery_status(_reading(), default_battery_config())
    assert status.max_discharge_power_kw == 5.0


def test_soc_passthrough():
    status = build_battery_status(_reading(soc=0.73), default_battery_config())
    assert status.soc == 0.73


def test_remaining_capacity_at_half_soc():
    status = build_battery_status(_reading(soc=0.5), default_battery_config())
    assert status.remaining_capacity_kwh == 5.0


def test_remaining_capacity_at_empty():
    status = build_battery_status(_reading(soc=0.0), default_battery_config())
    assert status.remaining_capacity_kwh == 0.0


def test_remaining_capacity_at_full():
    status = build_battery_status(_reading(soc=1.0), default_battery_config())
    assert status.remaining_capacity_kwh == 10.0


def test_status_is_immutable():
    import dataclasses
    status = build_battery_status(_reading(), default_battery_config())
    try:
        status.soc = 0.99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("BatteryStatus should be frozen")


# ---------------------------------------------------------------------------
# Node wiring: BatteryStatusNode within the DAG engine
# ---------------------------------------------------------------------------

def test_battery_status_node_produces_status_from_primary():
    import asyncio
    from datetime import datetime

    from custom_components.sun_sale.contract.models import (
        BatteryStatus,
        SunSaleConfig,
    )
    from custom_components.sun_sale.pipeline.dag_engine import NodeContext
    from custom_components.sun_sale.pipeline.nodes import BatteryStatusNode

    now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    config = SunSaleConfig(
        tariff=None,  # not consumed by this node
        battery=default_battery_config(),
    )
    ctx = NodeContext(
        primary={type(_reading()): _reading(soc=0.42)},
        secondary={},
        config=config,
        now=now,
    )

    node = BatteryStatusNode()
    asyncio.run(node.run(ctx))

    status = ctx.secondary[BatteryStatus]
    assert isinstance(status, BatteryStatus)
    assert status.soc == 0.42
    assert status.total_capacity_kwh == 10.0
    assert status.remaining_capacity_kwh == 4.2
    assert status.max_charge_power_kw == 5.0
    assert status.max_discharge_power_kw == 5.0


# ---------------------------------------------------------------------------
# BatteryTranslator: degrade to None when SoC is unavailable
# ---------------------------------------------------------------------------

class _StubInverter:
    """Minimal InverterController stand-in returning canned telemetry."""

    def __init__(self, soc, charge_energy=12.0, discharge_energy=8.0):
        self._soc = soc
        self._charge_energy = charge_energy
        self._discharge_energy = discharge_energy

    def get_battery_soc(self):
        return self._soc

    def get_battery_power(self):
        return 1.5

    def get_grid_power(self):
        return -0.3

    def get_battery_charge_energy_today(self):
        return self._charge_energy

    def get_battery_discharge_energy_today(self):
        return self._discharge_energy


class _StubHass:
    """Hass stub whose states.get always reports the load sensor missing."""

    class _States:
        def get(self, _entity_id):
            return None

    def __init__(self):
        self.states = self._States()


def test_translator_returns_none_when_soc_unavailable():
    import asyncio
    from datetime import datetime

    from custom_components.sun_sale.inbound.battery import BatteryTranslator

    translator = BatteryTranslator(_StubInverter(soc=None), household_load_entity="")
    now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = asyncio.run(translator.translate(_StubHass(), None, {}, now))
    assert result is None


def test_translator_produces_reading_when_soc_present():
    import asyncio
    from datetime import datetime

    from custom_components.sun_sale.inbound.battery import BatteryTranslator

    translator = BatteryTranslator(_StubInverter(soc=0.42), household_load_entity="")
    now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = asyncio.run(translator.translate(_StubHass(), None, {}, now))
    assert isinstance(result, BatteryReading)
    assert result.soc == 0.42
    assert result.power_kw == 1.5
    assert result.grid_power_kw == -0.3
    assert result.charge_energy_today_kwh == 12.0
    assert result.discharge_energy_today_kwh == 8.0


# ---------------------------------------------------------------------------
# BatterySource seam: an injected source overrides the inverter's battery reads
# (the future BMS-primary path), while grid stays sourced from the inverter.
# ---------------------------------------------------------------------------

class _StubBatterySource:
    """Battery source stand-in returning canned telemetry distinct from the inverter."""

    def soc(self):
        return 0.77

    def power_kw(self):
        return -2.0

    def charge_energy_today_kwh(self):
        return 99.0

    def discharge_energy_today_kwh(self):
        return 1.0


def test_injected_battery_source_overrides_inverter_battery_reads():
    import asyncio
    from datetime import datetime

    from custom_components.sun_sale.inbound.battery import BatteryTranslator

    translator = BatteryTranslator(
        _StubInverter(soc=0.42),
        household_load_entity="",
        battery_source=_StubBatterySource(),
    )
    now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = asyncio.run(translator.translate(_StubHass(), None, {}, now))
    assert isinstance(result, BatteryReading)
    # Battery fields come from the injected source, not the inverter stub.
    assert result.soc == 0.77
    assert result.power_kw == -2.0
    assert result.charge_energy_today_kwh == 99.0
    assert result.discharge_energy_today_kwh == 1.0
    # Grid power still comes from the inverter (an inverter-side measurement).
    assert result.grid_power_kw == -0.3


def test_inverter_battery_source_delegates_to_inverter():
    from custom_components.sun_sale.inbound.battery_source import InverterBatterySource

    source = InverterBatterySource(_StubInverter(soc=0.42))
    assert source.soc() == 0.42
    assert source.power_kw() == 1.5
    assert source.charge_energy_today_kwh() == 12.0
    assert source.discharge_energy_today_kwh() == 8.0
