"""Tests for inbound/telemetry/codec.py — pure Python, no HA.

The codec is the future single source of truth for vendor sign/unit transforms.
These tests pin the codec directly against the documented canonical conventions
and against the concrete numbers the live readers produce
(``InverterController.get_battery_power`` / ``get_grid_power``,
``PvPowerTranslator``, the directional observers, ``read_soc_fraction``). The
recorder resampler already routes its transforms through this codec
(``inbound/observer/recorder_resample.py``), so these pins are what guarantee
that path — and the future live reader — cannot drift.
"""
from custom_components.sun_sale.inbound.telemetry import (
    GenericCodec,
    SignalBinding,
    SignalRole,
    SolisCodec,
    TelemetryCodec,
    signed_polarity,
)
from custom_components.sun_sale.inbound.telemetry.binding import TelemetrySignal as S


def _binding(signal, role=SignalRole.PRIMARY):
    """Build a binding with a dummy entity for transform-only tests."""
    return SignalBinding(signal=signal, entity_id="sensor.x", role=role)


# --- Protocol conformance ---------------------------------------------------

def test_codecs_satisfy_protocol():
    """Both concrete codecs structurally satisfy the TelemetryCodec protocol."""
    assert isinstance(SolisCodec(), TelemetryCodec)
    assert isinstance(GenericCodec(), TelemetryCodec)


# --- PV: watts, ≥0, W-or-×1000 (no mW/MW) -----------------------------------

def test_pv_power_w_or_scaled_and_clamped():
    """PV: W (or blank) passes through, any other unit scales ×1000, clamp ≥ 0."""
    codec = SolisCodec()
    b = _binding(S.PV_POWER)
    assert codec.to_canonical(b, 1500.0, "W") == 1500.0
    assert codec.to_canonical(b, 1.5, "kW") == 1500.0
    assert codec.to_canonical(b, -5.0, "W") == 0.0
    assert codec.to_canonical(b, 2.0, "") == 2.0      # blank unit = watts


def test_pv_power_kw_unit_scaled_to_watts_and_clamped():
    """A kW-unit PV sensor scales ×1000; negatives clamp to 0."""
    codec = GenericCodec()
    b = _binding(S.PV_POWER)
    assert codec.to_canonical(b, 1.5, "kW") == 1500.0
    assert codec.to_canonical(b, -3.0, "W") == 0.0


# --- Battery: kW, positive=charging; Solis signed-net is flipped ------------

def test_battery_signed_net_solis_flips_to_charging_positive():
    """Solis discharge-positive net → sunSale charging-positive (sign flip)."""
    codec = SolisCodec()
    b = _binding(S.BATTERY_POWER, SignalRole.SIGNED_NET)
    # Solis publishes +2 kW while *discharging*; canonical must read −2 kW.
    assert codec.to_canonical(b, 2000.0, "W") == -2.0


def test_battery_generic_signed_net_not_flipped():
    """A generic/manual signed sensor is assumed already in sunSale convention."""
    codec = GenericCodec()
    b = _binding(S.BATTERY_POWER, SignalRole.SIGNED_NET)
    assert codec.to_canonical(b, 2.0, "kW") == 2.0


def test_battery_magnitude_primary_role_never_flipped():
    """The unsigned magnitude fallback is read as-is on every platform."""
    b = _binding(S.BATTERY_POWER, SignalRole.PRIMARY)
    assert SolisCodec().to_canonical(b, 1.0, "kW") == 1.0
    assert GenericCodec().to_canonical(b, 1.0, "kW") == 1.0


# --- Grid net: kW, positive=import; Solis ac-port fallback is flipped -------

def test_grid_net_primary_passthrough():
    """The primary net sensor already matches sunSale (positive=import)."""
    b = _binding(S.GRID_NET_POWER, SignalRole.PRIMARY)
    assert SolisCodec().to_canonical(b, 3.0, "kW") == 3.0


def test_grid_net_solis_fallback_flips_ac_port():
    """Solis ac-port (inverter→grid-positive) fallback flips to import-positive."""
    codec = SolisCodec()
    b = _binding(S.GRID_NET_POWER, SignalRole.FALLBACK)
    assert codec.to_canonical(b, 1000.0, "W") == -1.0


# --- Directional grid: ≥0; net split per side -------------------------------

def test_grid_directional_clamped_non_negative():
    """Directional import/export sensors normalise and clamp ≥ 0."""
    codec = GenericCodec()
    assert codec.to_canonical(_binding(S.GRID_IMPORT), -0.2, "kW") == 0.0
    assert codec.to_canonical(_binding(S.GRID_EXPORT), 1.0, "kW") == 1.0


def test_grid_signed_net_splits_into_sides():
    """A signed net (positive=import) projects onto each directional side."""
    codec = GenericCodec()
    imp = _binding(S.GRID_IMPORT, SignalRole.SIGNED_NET)
    exp = _binding(S.GRID_EXPORT, SignalRole.SIGNED_NET)
    # net = +2 kW → import 2, export 0
    assert codec.to_canonical(imp, 2.0, "kW") == 2.0
    assert codec.to_canonical(exp, 2.0, "kW") == 0.0
    # net = −1.5 kW → import 0, export 1.5
    assert codec.to_canonical(imp, -1.5, "kW") == 0.0
    assert codec.to_canonical(exp, -1.5, "kW") == 1.5


# --- AC port: sign preserved (raw vendor convention) ------------------------

def test_ac_port_sign_preserved():
    """AC-port power keeps its sign (positive=inverter→grid); derived.py flips."""
    codec = SolisCodec()
    b = _binding(S.AC_PORT_POWER)
    assert codec.to_canonical(b, -800.0, "W") == -0.8
    assert codec.to_canonical(b, 800.0, "W") == 0.8


# --- Backup: kW magnitude, clamped ≥0 ---------------------------------------

def test_backup_clamped_non_negative():
    """Backup-port power normalises to kW and clamps a small negative bias."""
    codec = GenericCodec()
    b = _binding(S.BACKUP_POWER)
    assert codec.to_canonical(b, -10.0, "W") == 0.0
    assert codec.to_canonical(b, 1200.0, "W") == 1.2


# --- SoC: 0–1 fraction, % vs heuristic --------------------------------------

def test_soc_percent_and_fraction():
    """SoC divides a %-unit value by 100; a unit-less fraction passes through."""
    codec = SolisCodec()
    b = _binding(S.BATTERY_SOC)
    assert codec.to_canonical(b, 55.0, "%") == 0.55
    assert codec.to_canonical(b, 1.0, "%") == 0.01      # exact 1 % via unit
    assert codec.to_canonical(b, 0.42, "") == 0.42      # already a fraction
    assert codec.to_canonical(b, 80.0, "") == 0.8       # heuristic: >1 → percent


# --- Daily energy: kWh ------------------------------------------------------

def test_daily_energy_normalised_to_kwh():
    """Daily counters scale Wh→kWh and pass kWh through."""
    codec = GenericCodec()
    for sig in (S.GENERATION_DAILY, S.GRID_IMPORT_DAILY, S.GRID_EXPORT_DAILY):
        b = _binding(sig)
        assert codec.to_canonical(b, 2500.0, "Wh") == 2.5
        assert codec.to_canonical(b, 4.0, "kWh") == 4.0


# --- signed_polarity helper (drives the live getters + panel spec) ----------

def test_signed_polarity_reflects_codec_flips():
    """The helper returns −1 for the Solis-flipped roles, +1 otherwise."""
    solis, generic = SolisCodec(), GenericCodec()
    # Solis flips the signed battery net and the ac-port grid fallback.
    assert signed_polarity(solis, S.BATTERY_POWER, SignalRole.SIGNED_NET) == -1
    assert signed_polarity(solis, S.GRID_NET_POWER, SignalRole.FALLBACK) == -1
    # Generic leaves both as-is; primary roles are never flipped on any platform.
    assert signed_polarity(generic, S.BATTERY_POWER, SignalRole.SIGNED_NET) == 1
    assert signed_polarity(generic, S.GRID_NET_POWER, SignalRole.FALLBACK) == 1
    assert signed_polarity(solis, S.GRID_NET_POWER, SignalRole.PRIMARY) == 1
