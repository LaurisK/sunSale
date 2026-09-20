"""Tests for the install-capability flags and the seams they gate.

``CONF_INSTALL_CAPABILITIES`` records which parts of an installation exist at
all. The setup flow writes it at Finish; the coordinator reads one flag today —
with prices left out it builds no price translator, so pricing and everything
downstream skip. Entries saved before the key are inferred, which is what keeps
every existing install on its current behaviour without a migration.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sun_sale.contract.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_NOMINAL_CAPACITY,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_PURCHASE_PRICE,
    CONF_BATTERY_RATED_CYCLE_LIFE,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY,
    CONF_INSTALL_CAPABILITIES,
    CONF_INVERTER_ENTITY_BATTERY_POWER,
    CONF_INVERTER_ENTITY_BATTERY_SOC,
    CONF_INVERTER_ENTITY_CHARGE_CONTROL,
    CONF_INVERTER_ENTITY_GRID_POWER,
    CONF_INVERTER_PLATFORM,
    CONF_NORDPOOL_ENTITY,
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
    ENERGY_DYNAMIC,
    FORMULA_ENERGY,
)
from custom_components.sun_sale.contract.install_capabilities import (
    CAP_INVERTER,
    CAP_PRICES,
    CAP_SOLAR_FORECAST,
    INSTALL_CAPABILITY_KEYS,
    resolve_install_capabilities,
)
from custom_components.sun_sale.inbound.pricing import (
    AmberTranslator,
    EntsoeTranslator,
    FixedPriceTranslator,
    NordpoolTranslator,
    OctopusAgileTranslator,
)
from custom_components.sun_sale.orchestration import persistent_store
from custom_components.sun_sale.orchestration.coordinator import SunSaleCoordinator
from custom_components.sun_sale.outbound.inverter import InverterPlatform

_PRICE_TRANSLATORS = (
    NordpoolTranslator,
    EntsoeTranslator,
    OctopusAgileTranslator,
    AmberTranslator,
    FixedPriceTranslator,
)


# --- Flag resolution --------------------------------------------------------- #

def test_stored_capabilities_win_and_missing_keys_read_false():
    """A written checklist is the answer; a key it never had means "not included"."""
    caps = resolve_install_capabilities({CONF_INSTALL_CAPABILITIES: {CAP_PRICES: True}})
    assert caps[CAP_PRICES] is True
    assert caps[CAP_INVERTER] is False
    assert set(caps) == set(INSTALL_CAPABILITY_KEYS)


def test_entries_without_the_key_keep_their_current_behaviour():
    """Inference, not migration: an entry saved before the checklist has everything."""
    caps = resolve_install_capabilities({})
    assert caps == {CAP_PRICES: True, CAP_INVERTER: True, CAP_SOLAR_FORECAST: True}


# --- The coordinator gate ---------------------------------------------------- #

class _NoDiskStore:
    """Store stand-in that reads empty and never writes.

    ``async_setup`` loads every persistent store; the HA test stub returns a
    ``MagicMock`` which cannot be awaited. Substituting this keeps the test on
    the real setup path without a disk or an event-loop-bound HA Store.
    """

    def __init__(self, *args, **kwargs) -> None:
        """Accept and ignore the HA Store signature."""

    async def async_load(self) -> None:
        """Return None — an empty store."""
        return None

    async def async_save(self, data) -> None:
        """Discard the write."""

    def async_delay_save(self, *args, **kwargs) -> None:
        """Discard the debounced write."""


_ENTRY = {
    CONF_PRICE_BUY: {FORMULA_ENERGY: ENERGY_DYNAMIC},
    CONF_PRICE_SELL: {FORMULA_ENERGY: ENERGY_DYNAMIC},
    CONF_NORDPOOL_ENTITY: "sensor.prices",
    CONF_INVERTER_PLATFORM: InverterPlatform.GENERIC.value,
    # The generic strategy direct-indexes its four control/telemetry roles;
    # unmapped is the normal "entity unavailable" path for this test.
    CONF_INVERTER_ENTITY_BATTERY_SOC: "",
    CONF_INVERTER_ENTITY_BATTERY_POWER: "",
    CONF_INVERTER_ENTITY_GRID_POWER: "",
    CONF_INVERTER_ENTITY_CHARGE_CONTROL: "",
    # ``async_setup`` builds the BatteryConfig unconditionally and
    # direct-indexes every one of these.
    CONF_BATTERY_NOMINAL_CAPACITY: 10.0,
    CONF_BATTERY_PURCHASE_PRICE: 5000.0,
    CONF_BATTERY_RATED_CYCLE_LIFE: 6000,
    CONF_BATTERY_MIN_SOC: 10,
    CONF_BATTERY_MAX_SOC: 95,
    CONF_BATTERY_MAX_CHARGE_POWER: 5.0,
    CONF_BATTERY_MAX_DISCHARGE_POWER: 5.0,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY: 90,
    CONF_BATTERY_NOMINAL_VOLTAGE: 400.0,
}


async def _setup_coordinator(monkeypatch, extra: dict | None = None) -> SunSaleCoordinator:
    """Return a coordinator that has run the real ``async_setup``.

    Args:
        monkeypatch: pytest monkeypatch fixture (swaps the persistent Store).
        extra: Entry-data keys to add to / override the base entry.
    """
    monkeypatch.setattr(persistent_store, "Store", _NoDiskStore)
    hass = MagicMock()
    hass.config.time_zone = "Europe/Vilnius"
    hass.config.latitude = 54.7
    hass.config.longitude = 25.3
    hass.states.get.return_value = None
    # MagicMock returns a non-awaitable; async_setup awaits the executor to
    # warm the holiday calendar off the loop, so run the job inline.
    async def _run_job(func, *args):
        """Execute an executor job synchronously in place of a thread."""
        return func(*args)
    hass.async_add_executor_job = _run_job
    entry = MagicMock()
    entry.entry_id = "capability_entry"
    entry.data = {**_ENTRY, **(extra or {})}
    entry.options = {}
    coordinator = SunSaleCoordinator(hass, entry)
    await coordinator.async_setup()
    return coordinator


async def test_coordinator_builds_a_price_feed_by_default(monkeypatch):
    """The baseline the gate is measured against: prices included, feed built."""
    coordinator = await _setup_coordinator(monkeypatch)
    assert any(isinstance(t, _PRICE_TRANSLATORS) for t in coordinator._translators)


async def test_coordinator_builds_no_price_feed_when_prices_are_unchecked(monkeypatch):
    """Prices unchecked on the checklist → no price translator, so pricing skips.

    Gating the translator rather than blanking the sensor also covers the
    fixed-price feed, which needs no sensor and would otherwise keep emitting
    slots. The cycle must still complete: everything downstream of pricing
    already handles an absent price series.
    """
    coordinator = await _setup_coordinator(
        monkeypatch, {CONF_INSTALL_CAPABILITIES: {CAP_PRICES: False}},
    )
    assert not any(isinstance(t, _PRICE_TRANSLATORS) for t in coordinator._translators)
    data = await coordinator._async_update_data()
    assert data
