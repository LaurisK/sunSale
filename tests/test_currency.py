"""Tests for the display-currency seam (currency.py + its UI consumers).

The optimisation math is currency-neutral; these tests pin the *presentation*
contract — that CONF_CURRENCY flows into the sensor / number unit labels, the
icon, and the dashboard attribute the panel reads — without touching any value.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.contract.const import CONF_CURRENCY
from custom_components.sun_sale.currency import (
    currency_icon,
    per_kwh_unit,
    resolve_currency,
)
from custom_components.sun_sale.number import ModeChangePenaltyNumber
from custom_components.sun_sale.sensor import (
    CurrentBuyPriceSensor,
    DashboardSensor,
    ExpectedProfitSensor,
    MonthlyBillSensor,
)


def _entry(currency_data=None, currency_option=None) -> MagicMock:
    """Build a config-entry mock with real data/options dicts."""
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.data = {} if currency_data is None else {CONF_CURRENCY: currency_data}
    entry.options = {} if currency_option is None else {CONF_CURRENCY: currency_option}
    return entry


# --- currency.py pure helpers ----------------------------------------------- #

def test_resolve_currency_defaults_to_eur():
    assert resolve_currency(_entry()) == "EUR"


def test_resolve_currency_reads_data():
    assert resolve_currency(_entry(currency_data="SEK")) == "SEK"


def test_resolve_currency_options_take_precedence_over_data():
    assert resolve_currency(_entry(currency_data="SEK", currency_option="NOK")) == "NOK"


def test_resolve_currency_normalises_case_and_whitespace():
    assert resolve_currency(_entry(currency_data="  dkk ")) == "DKK"


def test_resolve_currency_blank_falls_back_to_default():
    assert resolve_currency(_entry(currency_data="")) == "EUR"


def test_per_kwh_unit():
    assert per_kwh_unit("SEK") == "SEK/kWh"


@pytest.mark.parametrize(
    ("code", "icon"),
    [
        ("EUR", "mdi:currency-eur"),
        ("USD", "mdi:currency-usd"),
        ("GBP", "mdi:currency-gbp"),
        ("SEK", "mdi:cash"),   # no dedicated MDI glyph → generic cash
        ("ZZZ", "mdi:cash"),
    ],
)
def test_currency_icon(code, icon):
    assert currency_icon(code) == icon


# --- sensor / number localisation ------------------------------------------- #

def test_sensor_flat_currency_unit_localised():
    sensor = ExpectedProfitSensor(MagicMock(), _entry(currency_data="SEK"))
    assert sensor._attr_native_unit_of_measurement == "SEK"


def test_sensor_per_kwh_unit_localised():
    sensor = CurrentBuyPriceSensor(MagicMock(), _entry(currency_data="SEK"))
    assert sensor._attr_native_unit_of_measurement == "SEK/kWh"


def test_sensor_currency_icon_localised():
    # MonthlyBillSensor declares the mdi:currency-eur icon → rewritten for non-EUR.
    sensor = MonthlyBillSensor(MagicMock(), _entry(currency_data="SEK"))
    assert sensor._attr_icon == "mdi:cash"


def test_sensor_defaults_keep_eur():
    sensor = CurrentBuyPriceSensor(MagicMock(), _entry())
    assert sensor._attr_native_unit_of_measurement == "EUR/kWh"
    assert sensor._currency == "EUR"


def test_number_currency_unit_localised():
    coord = MagicMock()
    coord.mode_change_penalty_eur_per_kwh = 0.005
    number = ModeChangePenaltyNumber(coord, _entry(currency_data="NOK"))
    assert number._attr_native_unit_of_measurement == "NOK/kWh"


# --- dashboard attribute (panel feed) --------------------------------------- #

def test_dashboard_exposes_currency_attribute():
    coord = MagicMock()
    coord.data = None  # short-circuits to {} unless we feed it data
    sensor = DashboardSensor(coord, _entry(currency_data="SEK"))
    assert sensor._currency == "SEK"
