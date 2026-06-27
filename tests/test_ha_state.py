"""Tests for the shared HA-edge state-reading helpers in ha_state.py."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.ha_state import (
    available_state,
    normalize_energy_to_kwh,
    normalize_power_to_kw,
    power_unit_scale,
    read_float_state,
    read_power_kw,
)


@dataclass
class _State:
    """Minimal HA-like state stub."""
    state: str
    attributes: dict | None = None
    last_updated: datetime | None = None


class _Hass:
    """Minimal hass stub exposing ``states.get``."""

    def __init__(self, states: dict[str, _State] | None = None) -> None:
        self.states = self  # type: ignore[assignment]
        self._states = states or {}

    def get(self, entity_id: str):
        """Return the stubbed state for ``entity_id`` or None."""
        return self._states.get(entity_id)


NOW = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)


# --- available_state ---------------------------------------------------------

def test_available_state_returns_state_when_usable():
    state = _State(state="1.5")
    hass = _Hass({"sensor.x": state})
    assert available_state(hass, "sensor.x") is state


def test_available_state_none_for_empty_entity_id():
    assert available_state(_Hass(), "") is None


def test_available_state_none_when_entity_missing():
    assert available_state(_Hass(), "sensor.absent") is None


@pytest.mark.parametrize("raw", ["unavailable", "unknown", ""])
def test_available_state_none_for_sentinel_states(raw):
    hass = _Hass({"sensor.x": _State(state=raw)})
    assert available_state(hass, "sensor.x") is None


# --- read_float_state --------------------------------------------------------

def test_read_float_state_parses_number():
    hass = _Hass({"sensor.x": _State(state="3.92")})
    assert read_float_state(hass, "sensor.x") == pytest.approx(3.92)


def test_read_float_state_none_for_non_numeric():
    hass = _Hass({"sensor.x": _State(state="not a number")})
    assert read_float_state(hass, "sensor.x") is None


def test_read_float_state_none_for_missing_and_empty():
    assert read_float_state(_Hass(), "sensor.absent") is None
    assert read_float_state(_Hass(), "") is None


# --- read_power_kw -----------------------------------------------------------

@pytest.mark.parametrize(
    "value,unit,expected",
    [
        ("3920", "W", 3.92),
        ("3.92", "kW", 3.92),
        ("0.0039", "MW", 3.9),
        ("3920", "", 3920.0),       # unknown unit treated as kW
        ("3920", "garbage", 3920.0),
    ],
)
def test_read_power_kw_normalises_units(value, unit, expected):
    hass = _Hass({
        "sensor.p": _State(
            state=value, attributes={"unit_of_measurement": unit}, last_updated=NOW
        )
    })
    assert read_power_kw(hass, "sensor.p", now=NOW) == pytest.approx(expected)


def test_read_power_kw_none_for_unavailable_and_non_numeric():
    hass = _Hass({
        "sensor.bad": _State(state="unavailable"),
        "sensor.nan": _State(state="x", attributes={"unit_of_measurement": "kW"}),
    })
    assert read_power_kw(hass, "sensor.bad", now=NOW) is None
    assert read_power_kw(hass, "sensor.nan", now=NOW) is None


def test_read_power_kw_freshness_drops_stale_reading():
    stale = NOW - timedelta(seconds=120)
    hass = _Hass({
        "sensor.p": _State(
            state="1.0", attributes={"unit_of_measurement": "kW"}, last_updated=stale
        )
    })
    assert read_power_kw(hass, "sensor.p", now=NOW, max_age_s=60) is None


def test_read_power_kw_freshness_accepts_recent_reading():
    fresh = NOW - timedelta(seconds=10)
    hass = _Hass({
        "sensor.p": _State(
            state="1.0", attributes={"unit_of_measurement": "kW"}, last_updated=fresh
        )
    })
    assert read_power_kw(hass, "sensor.p", now=NOW, max_age_s=60) == pytest.approx(1.0)


def test_read_power_kw_missing_last_updated_skips_freshness_check():
    hass = _Hass({
        "sensor.p": _State(
            state="2.0", attributes={"unit_of_measurement": "kW"}, last_updated=None
        )
    })
    assert read_power_kw(hass, "sensor.p", now=NOW, max_age_s=60) == pytest.approx(2.0)


def test_read_power_kw_infinite_window_keeps_old_reading():
    ancient = NOW - timedelta(days=5)
    hass = _Hass({
        "sensor.p": _State(
            state="1.0", attributes={"unit_of_measurement": "kW"}, last_updated=ancient
        )
    })
    assert read_power_kw(hass, "sensor.p", now=NOW) == pytest.approx(1.0)


# --- pure unit helpers -------------------------------------------------------

def test_normalize_power_to_kw_handles_known_and_unknown_units():
    assert normalize_power_to_kw(3920.0, "W") == pytest.approx(3.92)
    assert normalize_power_to_kw(3.92, "kW") == pytest.approx(3.92)
    assert normalize_power_to_kw(0.0039, "MW") == pytest.approx(3.9)
    assert normalize_power_to_kw(3920.0, "") == pytest.approx(3920.0)
    assert normalize_power_to_kw(3920.0, "garbage") == pytest.approx(3920.0)


def test_power_unit_scale_matches_normalize():
    assert power_unit_scale("W") == pytest.approx(1.0 / 1000.0)
    assert power_unit_scale("kW") == pytest.approx(1.0)
    assert power_unit_scale("garbage") == pytest.approx(1.0)


def test_normalize_energy_to_kwh_handles_units():
    assert normalize_energy_to_kwh(3920.0, "Wh") == pytest.approx(3.92)
    assert normalize_energy_to_kwh(3.92, "kWh") == pytest.approx(3.92)
    assert normalize_energy_to_kwh(0.0039, "MWh") == pytest.approx(3.9)
    assert normalize_energy_to_kwh(5.0, "") == pytest.approx(5.0)
