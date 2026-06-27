"""Tests for inbound/telemetry/reader.py — the live read surface over the codec.

Covers ``resolve_bindings`` chain construction (fallback chains vs single
config-pick) and ``HaTelemetryReader``'s walk-the-chain / codec-transform /
freshness behaviour against a stubbed ``hass``, pinning that the reader
reproduces the exact semantics the controller getters and observer translators
used to inline.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.sun_sale.inbound.telemetry import (
    GenericCodec,
    HaTelemetryReader,
    SignalRole,
    SolisCodec,
    TelemetryReader,
    resolve_bindings,
)
from custom_components.sun_sale.inbound.telemetry.binding import TelemetrySignal as S

_NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


class _Hass:
    """Minimal hass stub: maps entity_id → (state, unit, last_updated) or absence."""

    def __init__(self, values: dict):
        self._values = values
        self.states = SimpleNamespace(get=self._get)

    def _get(self, entity_id):
        if entity_id not in self._values:
            return None
        spec = self._values[entity_id]
        state, unit = spec[0], spec[1]
        last_updated = spec[2] if len(spec) > 2 else _NOW
        return SimpleNamespace(
            state=state,
            attributes={"unit_of_measurement": unit},
            last_updated=last_updated,
        )


# --- resolve_bindings -------------------------------------------------------

def test_resolve_bindings_battery_is_runtime_fallback_chain():
    """Battery chain is signed-net first, then magnitude — a two-leg fallback."""
    chains = resolve_bindings(
        {"battery_power_signed": "sensor.bsig", "battery_power": "sensor.bmag"}
    )
    assert [(b.entity_id, b.role) for b in chains[S.BATTERY_POWER]] == [
        ("sensor.bsig", SignalRole.SIGNED_NET),
        ("sensor.bmag", SignalRole.PRIMARY),
    ]


def test_resolve_bindings_directional_is_single_config_pick():
    """Directional grid is ONE binding: directional preferred, else signed-net.

    Crucially never both — a mapped directional sensor must not runtime-fall
    back to the signed one (that would diverge from _DirectionalPowerObserver).
    """
    both = resolve_bindings(
        {}, grid_import_power="sensor.imp", signed_grid_power="sensor.net"
    )
    assert [(b.entity_id, b.role) for b in both[S.GRID_IMPORT]] == [
        ("sensor.imp", SignalRole.PRIMARY),
    ]
    only_net = resolve_bindings({}, signed_grid_power="sensor.net")
    assert [(b.entity_id, b.role) for b in only_net[S.GRID_EXPORT]] == [
        ("sensor.net", SignalRole.SIGNED_NET),
    ]
    assert resolve_bindings({})[S.GRID_IMPORT] == ()


def test_resolve_bindings_single_observer_signals():
    """PV / ac-port / backup each resolve to a single PRIMARY binding."""
    chains = resolve_bindings(
        {}, pv_power="sensor.pv", ac_port_power="sensor.ac", backup_power="sensor.bk"
    )
    assert [b.entity_id for b in chains[S.PV_POWER]] == ["sensor.pv"]
    assert [b.entity_id for b in chains[S.AC_PORT_POWER]] == ["sensor.ac"]
    assert [b.entity_id for b in chains[S.BACKUP_POWER]] == ["sensor.bk"]


# --- HaTelemetryReader ------------------------------------------------------

def test_reader_conforms_to_protocol():
    """HaTelemetryReader structurally satisfies the TelemetryReader protocol."""
    assert isinstance(HaTelemetryReader(resolve_bindings({}), GenericCodec()), TelemetryReader)


def test_reader_battery_signed_solis_flip():
    """Solis signed battery net (discharge-positive) reads as charging-positive."""
    hass = _Hass({"sensor.bsig": ("2000", "W")})   # +2 kW discharging on Solis
    reader = HaTelemetryReader(
        resolve_bindings({"battery_power_signed": "sensor.bsig"}), SolisCodec()
    )
    assert reader.read(hass, S.BATTERY_POWER) == -2.0


def test_reader_falls_back_to_magnitude_when_signed_absent():
    """With the signed leg unavailable, the reader uses the magnitude leg as-is."""
    hass = _Hass({"sensor.bmag": ("1.5", "kW")})   # signed entity simply absent
    reader = HaTelemetryReader(
        resolve_bindings(
            {"battery_power_signed": "sensor.bsig", "battery_power": "sensor.bmag"}
        ),
        SolisCodec(),
    )
    assert reader.read(hass, S.BATTERY_POWER) == 1.5     # PRIMARY role: never flipped


def test_reader_grid_primary_preferred_over_fallback():
    """When both grid sensors are present, the primary net wins (no flip)."""
    hass = _Hass({"sensor.gp": ("0.4", "kW"), "sensor.acport": ("1.0", "kW")})
    reader = HaTelemetryReader(
        resolve_bindings(
            {"grid_power": "sensor.gp", "grid_power_fallback": "sensor.acport"}
        ),
        SolisCodec(),
    )
    assert reader.read(hass, S.GRID_NET_POWER) == 0.4


def test_reader_grid_fallback_flips_ac_port_on_solis():
    """With no primary, the ac-port fallback (inverter→grid+) flips to import+."""
    hass = _Hass({"sensor.acport": ("1000", "W")})   # +1 kW inverter→grid
    reader = HaTelemetryReader(
        resolve_bindings({"grid_power_fallback": "sensor.acport"}), SolisCodec()
    )
    assert reader.read(hass, S.GRID_NET_POWER) == -1.0     # exporting → import-negative


def test_reader_directional_signed_split():
    """A signed net sensor projects onto each direction (import / export)."""
    bindings = resolve_bindings({}, signed_grid_power="sensor.net")
    imp = HaTelemetryReader(bindings, GenericCodec())
    # net = −1.5 kW → import 0, export 1.5
    hass = _Hass({"sensor.net": ("-1.5", "kW")})
    assert imp.read(hass, S.GRID_IMPORT) == 0.0
    assert imp.read(hass, S.GRID_EXPORT) == 1.5


def test_reader_returns_none_when_chain_empty_or_all_unavailable():
    """Unmapped signal or every candidate unavailable → None (caller defaults it)."""
    reader = HaTelemetryReader(
        resolve_bindings({"battery_power_signed": "sensor.bsig"}), GenericCodec()
    )
    hass = _Hass({"sensor.bsig": ("unavailable", "W")})
    assert reader.read(hass, S.BATTERY_POWER) is None
    assert reader.read(hass, S.GRID_NET_POWER) is None     # no chain at all


# --- freshness --------------------------------------------------------------

def test_reader_rejects_stale_state_when_window_finite():
    """A reading older than max_age_s is dropped; a fresh one passes."""
    fresh = _Hass({"sensor.ac": ("1.0", "kW", _NOW - timedelta(seconds=60))})
    stale = _Hass({"sensor.ac": ("1.0", "kW", _NOW - timedelta(seconds=300))})
    reader = HaTelemetryReader(
        resolve_bindings({}, ac_port_power="sensor.ac"), GenericCodec()
    )
    assert reader.read(fresh, S.AC_PORT_POWER, now=_NOW, max_age_s=180) == 1.0
    assert reader.read(stale, S.AC_PORT_POWER, now=_NOW, max_age_s=180) is None


def test_reader_infinite_window_keeps_stale_state():
    """The default (inf) window applies no freshness check (controller reads)."""
    stale = _Hass({"sensor.bsig": ("2.0", "kW", _NOW - timedelta(hours=2))})
    reader = HaTelemetryReader(
        resolve_bindings({"battery_power_signed": "sensor.bsig"}), GenericCodec()
    )
    assert reader.read(stale, S.BATTERY_POWER, now=_NOW) == 2.0
