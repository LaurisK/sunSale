"""Tests for battery.py — pure Python, no HA required."""
from datetime import UTC, datetime

from custom_components.sun_sale.contract.models import BatteryState, CapacityObservation
from custom_components.sun_sale.pipeline.battery import (
    CapacityEstimator,
    degradation_cost_per_kwh,
    trade_profit_per_kwh,
)
from tests.conftest import default_battery_config, default_battery_state

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def obs(soc_start, soc_end, energy_kwh, direction="charge"):
    return CapacityObservation(
        timestamp=NOW, soc_start=soc_start, soc_end=soc_end,
        energy_kwh=energy_kwh, direction=direction,
    )


# ---------------------------------------------------------------------------
# degradation_cost_per_kwh
# ---------------------------------------------------------------------------

def test_degradation_cost_formula():
    config = default_battery_config()
    state = default_battery_state()
    # 5000 / (6000 * 10.0 * 2) = 5000 / 120000 = 0.041667
    expected = 5000.0 / (6000 * 10.0 * 2)
    assert abs(degradation_cost_per_kwh(config, state) - expected) < 1e-10


def test_degradation_scales_with_capacity():
    config = default_battery_config()
    state_small = BatteryState(soc=0.5, estimated_capacity_kwh=5.0)
    state_large = BatteryState(soc=0.5, estimated_capacity_kwh=20.0)
    # Smaller capacity → higher cost per kWh (fewer kWh per cycle)
    assert degradation_cost_per_kwh(config, state_small) > degradation_cost_per_kwh(config, state_large)


# ---------------------------------------------------------------------------
# trade_profit_per_kwh
# ---------------------------------------------------------------------------

def test_trade_profitable_positive_spread():
    # sell=0.20 * 0.9 - buy=0.05 - deg=0.04 * 2 = 0.18 - 0.05 - 0.08 = 0.05
    assert trade_profit_per_kwh(0.05, 0.20, 0.04, 0.9) > 0


def test_trade_not_profitable_small_spread():
    # sell=0.12 * 0.9 - buy=0.10 - deg=0.04 * 2 = 0.108 - 0.10 - 0.08 = -0.072
    assert trade_profit_per_kwh(0.10, 0.12, 0.04, 0.9) <= 0


def test_trade_zero_profit_boundary():
    # solve: sell * eff - buy - deg * 2 = 0
    # sell = (buy + deg * 2) / eff = (0.05 + 0.08) / 0.9 = 0.14444...
    buy = 0.05
    deg = 0.04
    eff = 0.9
    sell = (buy + deg * 2) / eff
    profit = trade_profit_per_kwh(buy, sell, deg, eff)
    assert abs(profit) < 1e-10


def test_trade_profit_formula():
    profit = trade_profit_per_kwh(0.05, 0.20, 0.04, 0.90)
    expected = 0.20 * 0.90 - 0.05 - 0.04 * 2
    assert abs(profit - expected) < 1e-12


def test_trade_efficiency_reduces_profit():
    p_high = trade_profit_per_kwh(0.05, 0.20, 0.04, 0.95)
    p_low = trade_profit_per_kwh(0.05, 0.20, 0.04, 0.80)
    assert p_high > p_low


# ---------------------------------------------------------------------------
# CapacityEstimator
# ---------------------------------------------------------------------------

def test_estimator_returns_nominal_when_no_observations():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    assert est.estimated_capacity_kwh == 10.0


def test_estimator_single_observation():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    # charge 20%→80%: delta=0.6, energy=5.5kWh → implied=5.5/0.6=9.167
    est.add_observation(obs(0.20, 0.80, 5.5))
    assert abs(est.estimated_capacity_kwh - 5.5 / 0.6) < 0.001


def test_estimator_discards_small_delta():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    est.add_observation(obs(0.50, 0.53, 0.3))  # delta=0.03 < 0.05 threshold
    # Nominal should be unchanged since observation was discarded
    assert est.estimated_capacity_kwh == 10.0


def test_estimator_discards_at_exact_threshold():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    est.add_observation(obs(0.50, 0.549, 0.49))  # delta=0.049 < 0.05
    assert est.estimated_capacity_kwh == 10.0


def test_estimator_recent_bias():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    # Old: implies 8.0 kWh (6.4 / 0.8)
    est.add_observation(obs(0.10, 0.90, 6.4))
    # Recent: implies 9.5 kWh (7.6 / 0.8)
    est.add_observation(obs(0.10, 0.90, 7.6))
    result = est.estimated_capacity_kwh
    # Should be closer to 9.5 than 8.0 (weighted toward recent)
    assert result > 8.75, f"Expected >8.75, got {result}"


def test_estimator_convergence_multiple_identical():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    for _ in range(5):
        est.add_observation(obs(0.10, 0.90, 7.2))  # implies 9.0 kWh
    assert abs(est.estimated_capacity_kwh - 9.0) < 0.01


def test_estimator_serialization_roundtrip():
    est = CapacityEstimator(nominal_capacity_kwh=10.0)
    est.add_observation(obs(0.20, 0.80, 5.5))
    est.add_observation(obs(0.10, 0.90, 8.0))
    original = est.estimated_capacity_kwh

    d = est.to_dict()
    restored = CapacityEstimator.from_dict(d)
    assert abs(restored.estimated_capacity_kwh - original) < 1e-10


def test_estimator_from_dict_no_observations():
    d = {"nominal_capacity_kwh": 12.5, "observations": []}
    est = CapacityEstimator.from_dict(d)
    assert est.estimated_capacity_kwh == 12.5


# ---------------------------------------------------------------------------
# CapacityEstimator — plausibility band (rejects fabricated/placeholder-SoC
# artifacts that would corrupt the weighted average)
# ---------------------------------------------------------------------------

def test_estimator_rejects_implausibly_low_implied():
    """A large fake ΔSoC against near-zero energy (placeholder-SoC poison) is dropped."""
    est = CapacityEstimator(nominal_capacity_kwh=28.0)
    # ΔSoC=0.42 but only 0.012 kWh moved → implied ≈ 0.03 kWh, far below 0.5×28.
    est.add_observation(obs(0.50, 0.92, 0.012))
    assert est.estimated_capacity_kwh == 28.0


def test_estimator_rejects_implausibly_high_implied():
    """A fake-small ΔSoC against large energy → implied above 2×nominal is dropped."""
    est = CapacityEstimator(nominal_capacity_kwh=28.0)
    # ΔSoC=0.26 with 30 kWh → implied ≈ 115 kWh, far above 2×28.
    est.add_observation(obs(0.50, 0.76, 30.0))
    assert est.estimated_capacity_kwh == 28.0


def test_estimator_low_poison_rejected_at_intake():
    """Interleaved low-implied poison never enters, so the estimate is unmoved."""
    est = CapacityEstimator(nominal_capacity_kwh=28.0)
    est.add_observation(obs(0.50, 0.85, 10.0))  # implied 28.57, accepted
    baseline = est.estimated_capacity_kwh
    for _ in range(5):
        est.add_observation(obs(0.50, 1.00, 1.333))  # implied 2.67, poison
    assert est.estimated_capacity_kwh == baseline


def test_estimator_excludes_stored_poison_at_read_time():
    """Pre-fix poison already in storage is filtered out of the live estimate.

    Mirrors the live-box recovery path: the read-time filter neutralises the
    13.3 kWh under-estimate without purging .storage.
    """
    est = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[
            obs(0.50, 0.85, 10.0),                 # implied 28.57 (good)
            obs(0.50, 1.00, 1.333),                # implied 2.67 (low poison)
            obs(0.50, 0.08, 0.012, "discharge"),   # implied 0.03 (low poison)
        ],
    )
    # Only the single in-band sample contributes.
    assert abs(est.estimated_capacity_kwh - 10.0 / 0.35) < 0.01


def test_debug_observations_flags_out_of_band_samples():
    """debug_observations marks out-of-band stored samples rejected with zero weight."""
    est = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[
            obs(0.50, 0.85, 10.0),    # implied 28.57 (accepted)
            obs(0.50, 1.00, 1.333),   # implied 2.67 (rejected, below band)
        ],
    )
    dbg = est.debug_observations()
    assert dbg["count"] == 2
    assert dbg["accepted_count"] == 1
    by_implied = {round(r["implied_capacity_kwh"], 2): r for r in dbg["observations"]}
    assert by_implied[28.57]["accepted"] is True
    assert by_implied[2.67]["accepted"] is False
    assert by_implied[2.67]["weight"] == 0.0


# ---------------------------------------------------------------------------
# CapacityEstimator — efficiency correction (AC-side energy → stored capacity)
# ---------------------------------------------------------------------------

def test_estimator_default_efficiency_is_no_correction():
    # Default round_trip_efficiency=1.0 leaves raw energy/ΔSoC untouched.
    est = CapacityEstimator(nominal_capacity_kwh=30.0)
    est.add_observation(obs(0.20, 0.80, 18.0, "charge"))  # raw 30.0
    assert abs(est.estimated_capacity_kwh - 30.0) < 1e-6


def test_estimator_efficiency_corrects_charge_down():
    # round_trip 0.81 → one-way 0.9. Charge over-reads, so corrected = raw × 0.9.
    est = CapacityEstimator(nominal_capacity_kwh=30.0, round_trip_efficiency=0.81)
    est.add_observation(obs(0.20, 0.80, 18.0, "charge"))  # raw 30 → 27.0
    assert abs(est.estimated_capacity_kwh - 27.0) < 1e-6


def test_estimator_efficiency_corrects_discharge_up():
    # Discharge under-reads, so corrected = raw / 0.9.
    est = CapacityEstimator(nominal_capacity_kwh=30.0, round_trip_efficiency=0.81)
    est.add_observation(obs(0.80, 0.20, 18.0, "discharge"))  # raw 30 → 33.33
    assert abs(est.estimated_capacity_kwh - 30.0 / 0.9) < 1e-6


def test_estimator_nonpositive_efficiency_falls_back_to_no_correction():
    est = CapacityEstimator(nominal_capacity_kwh=30.0, round_trip_efficiency=0.0)
    est.add_observation(obs(0.20, 0.80, 18.0, "charge"))
    assert abs(est.estimated_capacity_kwh - 30.0) < 1e-6


# ---------------------------------------------------------------------------
# CapacityEstimator — schema-version purge of pre-fix poison
# ---------------------------------------------------------------------------

def test_from_dict_purges_pre_schema_observations():
    # Legacy dict (no schema_version) → poison discarded, estimate = nominal.
    legacy = {
        "nominal_capacity_kwh": 28.0,
        "observations": [
            {"timestamp": NOW.isoformat(), "soc_start": 0.5, "soc_end": 1.0,
             "energy_kwh": 1.333, "direction": "charge"},
        ],
    }
    est = CapacityEstimator.from_dict(legacy)
    assert est.estimated_capacity_kwh == 28.0
    assert est.debug_observations()["count"] == 0


def test_from_dict_keeps_current_schema_observations():
    d = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[obs(0.20, 0.80, 18.0, "charge")],
    ).to_dict()
    assert d["schema_version"] == 2
    restored = CapacityEstimator.from_dict(d)
    assert restored.debug_observations()["count"] == 1


def test_from_dict_injects_runtime_efficiency():
    # Efficiency is config (not persisted): from_dict applies the injected value.
    d = CapacityEstimator(
        nominal_capacity_kwh=30.0,
        observations=[obs(0.20, 0.80, 18.0, "charge")],
    ).to_dict()
    restored = CapacityEstimator.from_dict(d, round_trip_efficiency=0.81)
    assert abs(restored.estimated_capacity_kwh - 27.0) < 1e-6


# ---------------------------------------------------------------------------
# CapacityEstimator — purge + reset to nominal when configured capacity changes
# ---------------------------------------------------------------------------

def test_from_dict_purges_observations_when_nominal_changes():
    # Batteries added: persisted nominal 28 → live config 43. Old observations
    # describe the smaller pack, so they are dropped and the estimate resets to
    # the new nominal to re-learn quickly.
    d = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[obs(0.20, 0.80, 16.8, "charge")],  # would imply ~28
    ).to_dict()
    restored = CapacityEstimator.from_dict(d, current_nominal_kwh=43.0)
    assert restored.estimated_capacity_kwh == 43.0
    assert restored.debug_observations()["count"] == 0


def test_from_dict_keeps_observations_when_nominal_unchanged():
    d = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[obs(0.20, 0.80, 16.8, "charge")],
    ).to_dict()
    restored = CapacityEstimator.from_dict(d, current_nominal_kwh=28.0)
    assert restored.debug_observations()["count"] == 1


def test_from_dict_ignores_subthreshold_nominal_drift():
    # A sub-0.1 kWh float wobble is not a real pack change → observations kept.
    d = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[obs(0.20, 0.80, 16.8, "charge")],
    ).to_dict()
    restored = CapacityEstimator.from_dict(d, current_nominal_kwh=28.05)
    assert restored.debug_observations()["count"] == 1


def test_from_dict_adopts_current_nominal_over_persisted():
    # Config is the source of truth for nameplate: even when observations are
    # kept, a None-free current nominal is adopted (here within tolerance).
    d = CapacityEstimator(nominal_capacity_kwh=28.0).to_dict()
    restored = CapacityEstimator.from_dict(d, current_nominal_kwh=28.05)
    assert restored.estimated_capacity_kwh == 28.05  # no obs → nominal fallback


def test_from_dict_none_current_nominal_keeps_persisted():
    # Legacy callers (no live nominal) keep the persisted value and never purge.
    d = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[obs(0.20, 0.80, 16.8, "charge")],
    ).to_dict()
    restored = CapacityEstimator.from_dict(d)
    assert restored.debug_observations()["count"] == 1
