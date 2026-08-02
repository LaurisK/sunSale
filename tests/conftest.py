"""Shared fixtures and helpers for sunSale tests."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from types import ModuleType
from unittest.mock import MagicMock

# Ensure project root is on sys.path so pure-Python modules can be imported
# without a full HA installation.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Stub the homeassistant package so HA-dependent modules can be imported
# without a real HA installation. Pure-Python modules (models, tariff, battery,
# optimizer) never touch these stubs at runtime.
# ---------------------------------------------------------------------------

class _AutoMockModule(ModuleType):
    """A module that returns a MagicMock for any undefined attribute.

    This allows 'from homeassistant.x import Y' to succeed in tests
    even when HA is not installed.
    """
    def __getattr__(self, name: str):
        value = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


def _mock_module(name: str) -> _AutoMockModule:
    mod = _AutoMockModule(name)
    return mod


_HA_MODULES = [
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.event",
    "homeassistant.helpers.restore_state",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.entity_platform",
    "homeassistant.components",
    "homeassistant.components.frontend",
    "homeassistant.components.http",
    "homeassistant.components.panel_custom",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.components.number",
    "homeassistant.components.select",
    "homeassistant.const",
    "homeassistant.data_entry_flow",
    "homeassistant.util",
    "homeassistant.util.dt",
    "aiohttp",
    "voluptuous",
]

for _mod_name in _HA_MODULES:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _mock_module(_mod_name)

# Bind each submodule as an attribute on its parent module.
# Python's import machinery only does this for newly-loaded modules; when a
# module is already in sys.modules it returns early without calling setattr on
# the parent.  Without this, 'from homeassistant import config_entries' triggers
# _AutoMockModule.__getattr__ which stores a *plain MagicMock*, not the stub.
for _mod_name in _HA_MODULES:
    if "." in _mod_name:
        _parent_name, _child_name = _mod_name.rsplit(".", 1)
        if _parent_name in sys.modules:
            setattr(sys.modules[_parent_name], _child_name, sys.modules[_mod_name])

# Concrete stub classes for HA base types.
# Using distinct classes (not bare `object`) avoids "duplicate base class"
# TypeError when sensor/switch entities inherit from two of these stubs.


class _DataUpdateCoordinatorStub:
    def __init__(self, *args, **kwargs):
        if args:
            self.hass = args[0]


class _CoordinatorEntityStub:
    def __init__(self, coordinator=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if coordinator is not None:
            self.coordinator = coordinator

    def async_write_ha_state(self):
        pass


class _SensorEntityStub:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class _SwitchEntityStub:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class _NumberEntityStub:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class _ConfigFlowStub:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def async_show_form(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs


class _OptionsFlowStub:
    def async_show_form(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs


# Give the coordinator base class a real-enough shape for import
_coordinator_mod = sys.modules["homeassistant.helpers.update_coordinator"]
_coordinator_mod.DataUpdateCoordinator = _DataUpdateCoordinatorStub
_coordinator_mod.UpdateFailed = Exception
_coordinator_mod.CoordinatorEntity = _CoordinatorEntityStub

# Give the HTTP view base class a real-enough shape for import
_http_mod = sys.modules["homeassistant.components.http"]
_http_mod.HomeAssistantView = object

# Give config_entries shapes
_ce_mod = sys.modules["homeassistant.config_entries"]
_ce_mod.ConfigFlow = _ConfigFlowStub
_ce_mod.OptionsFlow = _OptionsFlowStub
_ce_mod.ConfigEntry = MagicMock

# Give sensor module shapes
_sensor_mod = sys.modules["homeassistant.components.sensor"]
_sensor_mod.SensorEntity = _SensorEntityStub
_sensor_mod.SensorDeviceClass = MagicMock()
_sensor_mod.SensorStateClass = MagicMock()

# Give switch module shapes
_switch_mod = sys.modules["homeassistant.components.switch"]
_switch_mod.SwitchEntity = _SwitchEntityStub

# Give number module shapes
_number_mod = sys.modules["homeassistant.components.number"]
_number_mod.NumberEntity = _NumberEntityStub


class _SelectEntityStub:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


_select_mod = sys.modules["homeassistant.components.select"]
_select_mod.SelectEntity = _SelectEntityStub


class _NumberModeStub:
    BOX = "box"
    SLIDER = "slider"
    AUTO = "auto"


_number_mod.NumberMode = _NumberModeStub

# RestoreEntity stub for switch.py
class _RestoreEntityStub:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def async_added_to_hass(self):
        pass

    async def async_get_last_state(self):
        return None


_restore_mod = sys.modules["homeassistant.helpers.restore_state"]
_restore_mod.RestoreEntity = _RestoreEntityStub

# panel_custom: register_panel must be awaitable, not bare MagicMock
_panel_mod = sys.modules["homeassistant.components.panel_custom"]
from unittest.mock import AsyncMock as _AsyncMock  # noqa: E402

_panel_mod.async_register_panel = _AsyncMock()

# http: StaticPathConfig is a dataclass-like; expose a passthrough
_http_mod.StaticPathConfig = lambda *a, **kw: object()

# dt_util.now() returns a real datetime so format strings work in tests
import datetime as _dt  # noqa: E402

_dt_util_mod = sys.modules["homeassistant.util.dt"]
_dt_util_mod.now = lambda: _dt.datetime.now(_dt.UTC)

# @callback must be a no-op pass-through decorator
_core_mod = sys.modules["homeassistant.core"]
_core_mod.callback = lambda fn: fn

# Give const shapes
_const_mod = sys.modules["homeassistant.const"]
_const_mod.UnitOfEnergy = MagicMock()
_const_mod.UnitOfEnergy.KILO_WATT_HOUR = "kWh"
_const_mod.STATE_ON = "on"
_const_mod.STATE_OFF = "off"
_const_mod.STATE_UNKNOWN = "unknown"
_const_mod.STATE_UNAVAILABLE = "unavailable"

from custom_components.sun_sale.contract.models import (  # noqa: E402  (after HA stubs)
    BatteryConfig,
    BatteryState,
    PriceEntry,
    SolarForecast,
    TariffConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_DT = datetime(2024, 1, 15, 0, 0, 0, tzinfo=_dt.UTC)


def make_price(hour: int, price: float, base: datetime = BASE_DT) -> PriceEntry:
    """Create a PriceEntry for a given hour of BASE_DT."""
    start = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    return PriceEntry(start=start, end=start + timedelta(hours=1), price_eur_kwh=price)


def make_solar(hour: int, kwh: float, base: datetime = BASE_DT) -> SolarForecast:
    start = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    return SolarForecast(start=start, end=start + timedelta(hours=1), generation_kwh=kwh)


# ---------------------------------------------------------------------------
# Default configs
# ---------------------------------------------------------------------------

def default_tariff_config() -> TariffConfig:
    return TariffConfig(
        distribution_fee=0.03,
        tax_rate=0.21,
        markup=0.01,
        sell_distribution_fee=0.02,
        sell_tax_rate=0.0,
        sell_markup=0.005,
    )


def default_battery_config() -> BatteryConfig:
    return BatteryConfig(
        nominal_capacity_kwh=10.0,
        purchase_price_eur=5000.0,
        rated_cycle_life=6000,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        min_soc=0.10,
        max_soc=0.95,
        round_trip_efficiency=0.90,
        nominal_voltage_v=48.0,
    )


def default_battery_state(soc: float = 0.50) -> BatteryState:
    return BatteryState(soc=soc, estimated_capacity_kwh=10.0)
