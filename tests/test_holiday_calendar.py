"""Tests for inbound/holiday_calendar.py — the public-holiday predicate."""
from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import pytest

from custom_components.sun_sale.inbound.holiday_calendar import holiday_predicate


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Clear the per-country cache so each test sees its own ``holidays`` module."""
    holiday_predicate.cache_clear()
    yield
    holiday_predicate.cache_clear()


def _fake_holidays(monkeypatch, calendars: dict[str, set[date]]) -> None:
    """Install a stand-in ``holidays`` module serving ``calendars``."""
    def country_holidays(country):
        if country not in calendars:
            raise NotImplementedError(country)
        return calendars[country]
    monkeypatch.setitem(sys.modules, "holidays", SimpleNamespace(country_holidays=country_holidays))


def test_no_country_means_no_holidays(monkeypatch):
    _fake_holidays(monkeypatch, {"LT": {date(2026, 2, 16)}})
    assert holiday_predicate(None) is None
    assert holiday_predicate("") is None


def test_the_predicate_reads_the_country_calendar(monkeypatch):
    _fake_holidays(monkeypatch, {"LT": {date(2026, 2, 16)}})
    is_holiday = holiday_predicate("lt")  # HA's code, any case
    assert is_holiday is not None
    assert is_holiday(date(2026, 2, 16))
    assert not is_holiday(date(2026, 2, 17))


def test_an_unsupported_country_means_no_holidays(monkeypatch):
    _fake_holidays(monkeypatch, {})
    assert holiday_predicate("XX") is None


def test_a_missing_package_means_no_holidays(monkeypatch):
    monkeypatch.setitem(sys.modules, "holidays", None)  # import raises ImportError
    assert holiday_predicate("LT") is None


def test_the_real_calendar_knows_lithuanian_statehood_day():
    pytest.importorskip("holidays")
    is_holiday = holiday_predicate("LT")
    assert is_holiday is not None
    assert is_holiday(date(2026, 2, 16))
    assert not is_holiday(date(2026, 2, 17))
