"""Tests for inbound/observer/grid.py per-direction power observers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import (
    GridExportPowerReading,
    GridImportPowerReading,
)
from custom_components.sun_sale.inbound.observer.grid import (
    GridExportPowerObserver,
    GridImportPowerObserver,
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

    def get(self, entity_id: str) -> _State | None:
        return self._states.get(entity_id)


NOW = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# GridImportPowerObserver
# ---------------------------------------------------------------------------


def test_import_observer_reads_kw_directly():
    """A kW state is parsed as-is into the reading."""
    hass = _Hass({"sensor.imp_kw": _State(
        state="2.5",
        attributes={"unit_of_measurement": "kW"},
        last_updated=NOW,
    )})
    obs = GridImportPowerObserver("sensor.imp_kw")
    reading = obs._parse(hass, NOW)
    assert isinstance(reading, GridImportPowerReading)
    assert reading.power_kw == 2.5
    assert reading.timestamp == NOW


def test_import_observer_normalises_w_to_kw():
    """A W state is divided by 1000 into kW."""
    hass = _Hass({"sensor.imp_w": _State(
        state="2500",
        attributes={"unit_of_measurement": "W"},
        last_updated=NOW,
    )})
    obs = GridImportPowerObserver("sensor.imp_w")
    reading = obs._parse(hass, NOW)
    assert reading.power_kw == 2.5


def test_import_observer_clamps_negative_to_zero():
    """Negative values are clamped to 0 — directional magnitudes are non-negative."""
    hass = _Hass({"sensor.imp": _State(
        state="-0.5",
        attributes={"unit_of_measurement": "kW"},
        last_updated=NOW,
    )})
    obs = GridImportPowerObserver("sensor.imp")
    reading = obs._parse(hass, NOW)
    assert reading.power_kw == 0.0


def test_import_observer_returns_none_on_unavailable_state():
    """``unavailable`` / ``unknown`` / empty state returns None."""
    obs = GridImportPowerObserver("sensor.imp")
    for raw in ("unavailable", "unknown", ""):
        hass = _Hass({"sensor.imp": _State(state=raw, last_updated=NOW)})
        assert obs._parse(hass, NOW) is None


def test_import_observer_returns_none_on_empty_entity_id():
    """An unconfigured entity ID disables the observer."""
    obs = GridImportPowerObserver("")
    hass = _Hass({"sensor.anything": _State(state="1.0", last_updated=NOW)})
    assert obs._parse(hass, NOW) is None


def test_import_observer_returns_none_on_unparseable_state():
    """Non-numeric state returns None rather than crashing."""
    hass = _Hass({"sensor.imp": _State(state="not a number", last_updated=NOW)})
    obs = GridImportPowerObserver("sensor.imp")
    assert obs._parse(hass, NOW) is None


def test_import_observer_freshness_check_drops_stale_state():
    """A state older than ``max_age_s`` is treated as unavailable."""
    stale_ts = NOW - timedelta(seconds=240)
    hass = _Hass({"sensor.imp": _State(
        state="2.0",
        attributes={"unit_of_measurement": "kW"},
        last_updated=stale_ts,
    )})
    obs = GridImportPowerObserver("sensor.imp", max_age_s=180)
    assert obs._parse(hass, NOW) is None


def test_import_observer_freshness_check_accepts_recent_state():
    """A state within the freshness window is accepted."""
    fresh_ts = NOW - timedelta(seconds=30)
    hass = _Hass({"sensor.imp": _State(
        state="2.0",
        attributes={"unit_of_measurement": "kW"},
        last_updated=fresh_ts,
    )})
    obs = GridImportPowerObserver("sensor.imp", max_age_s=180)
    reading = obs._parse(hass, NOW)
    assert reading is not None
    assert reading.power_kw == 2.0


# ---------------------------------------------------------------------------
# GridExportPowerObserver — mirror coverage
# ---------------------------------------------------------------------------


def test_export_observer_reads_kw_directly():
    """The export observer produces a GridExportPowerReading."""
    hass = _Hass({"sensor.exp": _State(
        state="3.1",
        attributes={"unit_of_measurement": "kW"},
        last_updated=NOW,
    )})
    obs = GridExportPowerObserver("sensor.exp")
    reading = obs._parse(hass, NOW)
    assert isinstance(reading, GridExportPowerReading)
    assert reading.power_kw == 3.1


def test_export_observer_clamps_negative_to_zero():
    """Export observer clamps negative readings to 0 (same contract as import)."""
    hass = _Hass({"sensor.exp": _State(
        state="-2.0",
        attributes={"unit_of_measurement": "kW"},
        last_updated=NOW,
    )})
    obs = GridExportPowerObserver("sensor.exp")
    reading = obs._parse(hass, NOW)
    assert reading.power_kw == 0.0


# ---------------------------------------------------------------------------
# Signed-fallback mode (Solis auto-detect / installs with only a net sensor)
# ---------------------------------------------------------------------------


def test_import_observer_signed_fallback_extracts_positive_side():
    """With no directional entity, the import side reads the signed sensor's positive half."""
    hass = _Hass({"sensor.grid_net": _State(
        state="2500",
        attributes={"unit_of_measurement": "W"},
        last_updated=NOW,
    )})
    obs = GridImportPowerObserver(entity_id="", signed_entity_id="sensor.grid_net")
    reading = obs._parse(hass, NOW)
    assert reading is not None
    assert reading.power_kw == 2.5


def test_import_observer_signed_fallback_clamps_export_to_zero():
    """Negative signed value (export) projects onto the import side as zero."""
    hass = _Hass({"sensor.grid_net": _State(
        state="-2957",
        attributes={"unit_of_measurement": "W"},
        last_updated=NOW,
    )})
    obs = GridImportPowerObserver(entity_id="", signed_entity_id="sensor.grid_net")
    reading = obs._parse(hass, NOW)
    assert reading is not None
    assert reading.power_kw == 0.0


def test_export_observer_signed_fallback_extracts_negative_side():
    """With no directional entity, the export side reads the signed sensor's negative half (flipped)."""
    hass = _Hass({"sensor.grid_net": _State(
        state="-2957",
        attributes={"unit_of_measurement": "W"},
        last_updated=NOW,
    )})
    obs = GridExportPowerObserver(entity_id="", signed_entity_id="sensor.grid_net")
    reading = obs._parse(hass, NOW)
    assert reading is not None
    assert reading.power_kw == 2.957


def test_export_observer_signed_fallback_clamps_import_to_zero():
    """Positive signed value (import) projects onto the export side as zero."""
    hass = _Hass({"sensor.grid_net": _State(
        state="500",
        attributes={"unit_of_measurement": "W"},
        last_updated=NOW,
    )})
    obs = GridExportPowerObserver(entity_id="", signed_entity_id="sensor.grid_net")
    reading = obs._parse(hass, NOW)
    assert reading is not None
    assert reading.power_kw == 0.0


def test_directional_entity_preferred_over_signed_fallback():
    """When both are configured, the directional entity wins (signed never read)."""
    hass = _Hass({
        "sensor.imp": _State(state="1.0", attributes={"unit_of_measurement": "kW"}, last_updated=NOW),
        "sensor.grid_net": _State(state="-2.0", attributes={"unit_of_measurement": "kW"}, last_updated=NOW),
    })
    obs = GridImportPowerObserver(entity_id="sensor.imp", signed_entity_id="sensor.grid_net")
    reading = obs._parse(hass, NOW)
    assert reading is not None
    # Directional entity says 1.0 kW import; signed sensor (export) would imply 0.0
    assert reading.power_kw == 1.0


def test_observer_returns_none_when_neither_entity_configured():
    """An observer with neither directional nor signed entity is fully disabled."""
    hass = _Hass({"sensor.anything": _State(state="1.0", last_updated=NOW)})
    obs = GridImportPowerObserver(entity_id="", signed_entity_id="")
    assert obs._parse(hass, NOW) is None


def test_signed_fallback_respects_freshness_check():
    """A stale signed sensor still gets the freshness drop."""
    stale_ts = NOW - timedelta(seconds=240)
    hass = _Hass({"sensor.grid_net": _State(
        state="-1500",
        attributes={"unit_of_measurement": "W"},
        last_updated=stale_ts,
    )})
    obs = GridExportPowerObserver(
        entity_id="", signed_entity_id="sensor.grid_net", max_age_s=180,
    )
    assert obs._parse(hass, NOW) is None
