"""Tests for models.py — data structure correctness."""
from datetime import datetime, timezone, timedelta
import pytest
from custom_components.sun_sale.contract.models import (
    PriceEntry, TariffConfig, BatteryConfig, BatteryState,
    SolarForecast, ScheduleSlot, Schedule, CapacityObservation, StorageMode,
)


NOW = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)


def test_storage_mode_enum_values():
    assert StorageMode.GridCharge.value == "grid_charge"
    assert StorageMode.Discharge.value == "discharge"
    assert StorageMode.SelfUse.value == "self_use"


def test_storage_mode_enum_from_value():
    assert StorageMode("grid_charge") == StorageMode.GridCharge


def test_storage_mode_auto_removed():
    # AUTO was retired — neither the member nor its legacy "auto" string parse.
    assert not hasattr(StorageMode, "AUTO")
    with pytest.raises(ValueError):
        StorageMode("auto")


def test_price_entry_frozen():
    hp = PriceEntry(start=NOW, end=NOW + timedelta(hours=1), price_eur_kwh=0.10)
    with pytest.raises((AttributeError, TypeError)):
        hp.price_eur_kwh = 0.20  # type: ignore[misc]


def test_tariff_config_construction():
    tc = TariffConfig(
        distribution_fee=0.03, tax_rate=0.21, markup=0.01,
        sell_distribution_fee=0.02, sell_tax_rate=0.0, sell_markup=0.005,
    )
    assert tc.tax_rate == 0.21


def test_battery_config_frozen():
    bc = BatteryConfig(
        nominal_capacity_kwh=10.0, purchase_price_eur=5000.0,
        rated_cycle_life=6000, max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0, min_soc=0.10, max_soc=0.95,
        round_trip_efficiency=0.90,
    )
    with pytest.raises((AttributeError, TypeError)):
        bc.nominal_capacity_kwh = 20.0  # type: ignore[misc]


def test_battery_state_mutable():
    bs = BatteryState(soc=0.5, estimated_capacity_kwh=10.0)
    bs.soc = 0.6
    assert bs.soc == 0.6


def test_schedule_slot_frozen():
    slot = ScheduleSlot(
        start=NOW, end=NOW + timedelta(hours=1),
        mode=StorageMode.SelfUse, power_kw=0.0,
        expected_soc_after=0.5, expected_profit_eur=0.0, reason="test",
    )
    with pytest.raises((AttributeError, TypeError)):
        slot.mode = StorageMode.GridCharge  # type: ignore[misc]


def test_capacity_observation_frozen():
    obs = CapacityObservation(
        timestamp=NOW, soc_start=0.2, soc_end=0.8,
        energy_kwh=5.5, direction="charge",
    )
    with pytest.raises((AttributeError, TypeError)):
        obs.energy_kwh = 6.0  # type: ignore[misc]
