"""Tests for the battery-source seam: JK-BMS, chaining, and conformance."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.sun_sale.inbound.battery_source import (
    BatterySource,
    ChainedBatterySource,
    InverterBatterySource,
    JkBmsBatterySource,
)


class _FakeStates:
    """Minimal hass.states stand-in mapping entity_id → SimpleNamespace state."""

    def __init__(self, states: dict):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def _hass(states: dict) -> MagicMock:
    h = MagicMock()
    h.states = _FakeStates(states)
    return h


def _state(value, unit=""):
    return SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": unit} if unit else {},
        last_updated=None,
    )


# --- conformance -----------------------------------------------------------

def test_all_sources_conform_to_protocol():
    inv = MagicMock()
    assert isinstance(InverterBatterySource(inv), BatterySource)
    assert isinstance(JkBmsBatterySource(_hass({}), "", ""), BatterySource)
    assert isinstance(
        ChainedBatterySource(
            JkBmsBatterySource(_hass({}), "", ""), InverterBatterySource(inv)
        ),
        BatterySource,
    )


# --- JK-BMS source ---------------------------------------------------------

def test_jkbms_reads_soc_percent_and_signed_power():
    hass = _hass({
        "sensor.bms_soc": _state(83, "%"),
        "sensor.bms_power": _state(-1500, "W"),  # discharging
    })
    src = JkBmsBatterySource(hass, "sensor.bms_soc", "sensor.bms_power")
    assert src.soc() == 0.83
    assert src.power_kw() == -1.5
    # No daily AC energy counters on a BMS.
    assert src.charge_energy_today_kwh() is None
    assert src.discharge_energy_today_kwh() is None


def test_jkbms_unmapped_entities_yield_none():
    src = JkBmsBatterySource(_hass({}), "", "")
    assert src.soc() is None
    assert src.power_kw() is None


# --- chaining (per-field fallback) -----------------------------------------

class _Stub:
    def __init__(self, soc=None, power=None, charge=None, discharge=None):
        self._soc, self._power = soc, power
        self._charge, self._discharge = charge, discharge

    def soc(self):
        return self._soc

    def power_kw(self):
        return self._power

    def charge_energy_today_kwh(self):
        return self._charge

    def discharge_energy_today_kwh(self):
        return self._discharge


def test_chain_prefers_primary_per_field():
    primary = _Stub(soc=0.6, power=2.0)               # BMS: SoC + power only
    fallback = _Stub(soc=0.99, power=9.9, charge=12.0, discharge=8.0)
    chain = ChainedBatterySource(primary, fallback)
    # SoC + power from the BMS …
    assert chain.soc() == 0.6
    assert chain.power_kw() == 2.0
    # … energy counters fall through to the inverter per field.
    assert chain.charge_energy_today_kwh() == 12.0
    assert chain.discharge_energy_today_kwh() == 8.0


def test_chain_falls_back_when_primary_missing():
    primary = _Stub()  # all None (BMS offline / unmapped)
    fallback = _Stub(soc=0.4, power=-1.0, charge=3.0, discharge=2.0)
    chain = ChainedBatterySource(primary, fallback)
    assert chain.soc() == 0.4
    assert chain.power_kw() == -1.0
    assert chain.charge_energy_today_kwh() == 3.0
    assert chain.discharge_energy_today_kwh() == 2.0


def test_chain_zero_power_from_primary_is_not_overridden():
    # 0.0 is a real reading, not "missing" — must not fall through.
    chain = ChainedBatterySource(_Stub(soc=0.5, power=0.0), _Stub(power=5.0))
    assert chain.power_kw() == 0.0
