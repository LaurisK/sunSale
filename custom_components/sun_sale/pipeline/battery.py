"""Battery degradation model and capacity estimator.

Pure Python — no Home Assistant imports.
"""
from __future__ import annotations

import math
from datetime import datetime

from ..contract.const import CAPACITY_OBS_MIN_SOC_DELTA
from ..contract.models import BatteryConfig, BatteryState, CapacityObservation

# Persisted-observation schema version. Bumped to 2 when the capacity estimator
# moved to counter-based, anchor-accumulated observations: pre-2 observations
# were built by differencing a fabricated 0.5 SoC fallback (since removed)
# against real readings, so they are discarded on load and relearned clean.
_CAPACITY_SCHEMA_VERSION = 2

# A configured-nominal change larger than this (kWh) — e.g. batteries added or
# removed — invalidates the learned observations (they describe a differently
# sized pack), so they are purged on load and the estimate resets to the new
# nominal to re-learn quickly. Small enough to ignore float-repr noise, large
# enough that any real pack change trips it.
_NOMINAL_CHANGE_EPS_KWH = 0.1

# An observation's implied capacity (energy_kwh / |ΔSoC|) must land within
# [MIN, MAX] × nominal to be trusted. Outside this band the sample reflects a
# sensor artifact rather than real battery behaviour, and would corrupt the
# weighted-average estimator:
#   * Above MAX — a unit bug (W read as kW), or a fabricated/placeholder SoC
#     whose fake-small ΔSoC inflates the ratio.
#   * Below MIN — a fabricated/placeholder SoC (e.g. the old 0.5 SoC fallback
#     that fired during a Modbus dropout) differenced against a real reading,
#     giving a large fake ΔSoC against near-zero energy. A real pack does not
#     silently shed half its nameplate capacity, so MIN=0.5 rejects these while
#     still admitting a genuinely end-of-life (50 %-health) battery.
_CAPACITY_OBS_MIN_MULTIPLIER = 0.5
_CAPACITY_OBS_MAX_MULTIPLIER = 2.0


def degradation_cost_per_kwh(config: BatteryConfig, state: BatteryState) -> float:
    """Compute the wear cost per kWh cycled through the battery.

    Formula: purchase_price / (rated_cycle_life * estimated_capacity_kwh * 2).
    The *2 accounts for one full cycle = one charge + one discharge.

    Args:
        config: Battery configuration including purchase price and cycle life.
        state: Current battery state containing the learned capacity estimate.

    Returns:
        Degradation cost in EUR/kWh.
    """
    return config.purchase_price_eur / (
        config.rated_cycle_life * state.estimated_capacity_kwh * 2.0
    )


def trade_profit_per_kwh(
    buy_tariff: float,
    sell_tariff: float,
    deg_cost: float,
    efficiency: float,
) -> float:
    """Compute net profit per kWh charged: sell_revenue - buy_cost - degradation.

    Degradation is counted twice (once charging, once discharging).
    Efficiency reduces the kWh available to sell.

    Args:
        buy_tariff: Effective buy price in EUR/kWh.
        sell_tariff: Effective sell price in EUR/kWh.
        deg_cost: Degradation cost per kWh (from degradation_cost_per_kwh).
        efficiency: Round-trip efficiency (0.0–1.0).

    Returns:
        Net profit in EUR per kWh charged; negative means unprofitable.
    """
    return sell_tariff * efficiency - buy_tariff - deg_cost * 2.0


class CapacityEstimator:
    """Learns actual usable battery capacity from observed charge/discharge cycles.

    Uses an exponentially-decayed weighted average: recent observations have
    higher weight (DECAY^0 = 1.0) while older ones decay by DECAY per position.
    """

    DECAY = 0.9  # Weight of each observation relative to the next newer one

    def __init__(
        self,
        nominal_capacity_kwh: float,
        observations: list[CapacityObservation] | None = None,
        round_trip_efficiency: float = 1.0,
    ) -> None:
        """Initialise estimator with the nameplate capacity and optional history.

        Args:
            nominal_capacity_kwh: Nameplate battery capacity; used as fallback
                when no observations are available.
            observations: Previously recorded charge/discharge observations to
                seed the estimator on startup.
            round_trip_efficiency: Battery round-trip efficiency (0–1) used to
                normalise AC-side charge/discharge samples to stored capacity.
                Defaults to 1.0 (no correction). Non-positive values are treated
                as 1.0 to keep the one-way ``sqrt`` well-defined.
        """
        self._nominal = nominal_capacity_kwh
        self._observations: list[CapacityObservation] = list(observations or [])
        self._round_trip_efficiency = (
            round_trip_efficiency if round_trip_efficiency > 0 else 1.0
        )

    def _implied(self, obs: CapacityObservation) -> float | None:
        """Return an observation's efficiency-corrected implied stored capacity.

        Raw ``energy_kwh / |ΔSoC|`` is the AC energy moved per unit SoC. Charge
        energy is measured AC-side *into* the battery, so it over-reads stored
        capacity by 1/η_one_way; discharge energy is measured AC-side *out*, so it
        under-reads by η_one_way. With ``η_one_way = sqrt(round_trip_efficiency)``
        the charge sample is scaled ×η and the discharge sample ÷η, so both
        estimate the same stored (DC) capacity — the quantity the scheduler treats
        as usable capacity. ``None`` only guards the zero-ΔSoC division; the
        reliability/plausibility filters live in :meth:`_accept`.

        Args:
            obs: Observation to evaluate.

        Returns:
            Efficiency-corrected implied capacity in kWh, or None when ΔSoC is
            exactly zero.
        """
        soc_delta = abs(obs.soc_end - obs.soc_start)
        if soc_delta <= 0:
            return None
        raw = obs.energy_kwh / soc_delta
        eff_one_way = math.sqrt(self._round_trip_efficiency)
        if obs.direction == "charge":
            return raw * eff_one_way
        return raw / eff_one_way

    def _accept(self, obs: CapacityObservation) -> bool:
        """Return whether an observation is trustworthy enough to estimate from.

        Rejects samples whose SoC swing is too small to yield a reliable ratio
        (``|ΔSoC| < CAPACITY_OBS_MIN_SOC_DELTA``) or whose implied capacity falls
        outside the physical plausibility band
        ``[_CAPACITY_OBS_MIN_MULTIPLIER, _CAPACITY_OBS_MAX_MULTIPLIER] × nominal``
        (a sensor artifact — see the constant comments). Single source of truth
        for both intake (:meth:`add_observation`) and read-time filtering
        (:attr:`estimated_capacity_kwh`, :meth:`debug_observations`).

        Args:
            obs: Observation to evaluate.

        Returns:
            True when the observation passes both filters.
        """
        if abs(obs.soc_end - obs.soc_start) < CAPACITY_OBS_MIN_SOC_DELTA:
            return False
        implied = self._implied(obs)
        return (
            implied is not None
            and self._nominal * _CAPACITY_OBS_MIN_MULTIPLIER
            <= implied
            <= self._nominal * _CAPACITY_OBS_MAX_MULTIPLIER
        )

    def add_observation(self, obs: CapacityObservation) -> None:
        """Record a charge/discharge observation; silently discards bad samples.

        An observation is kept only when it passes :meth:`_accept` — a large
        enough SoC swing and an implied capacity inside the plausibility band.
        Rejected samples (too-small ΔSoC, or a sensor-unit bug / fabricated-SoC
        artifact outside the band) would otherwise corrupt the weighted average.

        Args:
            obs: Observation to append.
        """
        if self._accept(obs):
            self._observations.append(obs)

    @property
    def estimated_capacity_kwh(self) -> float:
        """Current best estimate of usable capacity in kWh."""
        implied = [
            cap
            for obs in self._observations
            if self._accept(obs) and (cap := self._implied(obs)) is not None
        ]
        if not implied:
            return self._nominal

        n = len(implied)
        weighted_sum = 0.0
        total_weight = 0.0
        for i, cap in enumerate(implied):
            w = self.DECAY ** (n - 1 - i)  # Newer observations (higher i) get weight closer to 1
            weighted_sum += w * cap
            total_weight += w

        return weighted_sum / total_weight

    def debug_observations(self) -> dict:
        """Return per-observation diagnostics for auditing the capacity estimate.

        Exposes every stored observation with its implied capacity
        (``energy_kwh / |soc_delta|``), whether it currently passes the
        plausibility filters, and the exponential weight it carries in the
        weighted average. Lets the debug API show *why* the estimate sits where
        it does — e.g. a run of low-implied discharge samples dragging it below
        nominal. Diagnostic only; nothing in the pipeline consumes it.

        Returns:
            Dict with the live ``estimated_capacity_kwh``, ``nominal_capacity_kwh``,
            total/accepted observation counts, and an ``observations`` list of
            per-sample rows in storage order (newest last).
        """
        n = sum(1 for o in self._observations if self._accept(o))

        rows: list[dict] = []
        acc_i = 0
        for obs in self._observations:
            implied = self._implied(obs)
            is_acc = self._accept(obs)
            weight = self.DECAY ** (n - 1 - acc_i) if is_acc else 0.0
            if is_acc:
                acc_i += 1
            rows.append({
                "timestamp": obs.timestamp.isoformat(),
                "direction": obs.direction,
                "soc_start": round(obs.soc_start, 4),
                "soc_end": round(obs.soc_end, 4),
                "soc_delta": round(obs.soc_end - obs.soc_start, 4),
                "energy_kwh": round(obs.energy_kwh, 4),
                "implied_capacity_kwh": (
                    round(implied, 3) if implied is not None else None
                ),
                "accepted": is_acc,
                "weight": round(weight, 4),
            })

        return {
            "estimated_capacity_kwh": round(self.estimated_capacity_kwh, 4),
            "nominal_capacity_kwh": self._nominal,
            "count": len(self._observations),
            "accepted_count": n,
            "observations": rows,
        }

    def to_dict(self) -> dict:
        """Serialize for HA persistent storage.

        ``round_trip_efficiency`` is deliberately *not* persisted — it is runtime
        config re-injected on load, so a config change takes effect without a
        stale stored value overriding it.
        """
        return {
            "schema_version": _CAPACITY_SCHEMA_VERSION,
            "nominal_capacity_kwh": self._nominal,
            "observations": [
                {
                    "timestamp": obs.timestamp.isoformat(),
                    "soc_start": obs.soc_start,
                    "soc_end": obs.soc_end,
                    "energy_kwh": obs.energy_kwh,
                    "direction": obs.direction,
                }
                for obs in self._observations
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
        round_trip_efficiency: float = 1.0,
        current_nominal_kwh: float | None = None,
    ) -> CapacityEstimator:
        """Deserialise from the HA persistent-storage dict format.

        Stored observations are discarded — and the estimate falls back to
        nominal — in two cases:

        * **Stale schema** (older than ``_CAPACITY_SCHEMA_VERSION``): pre-2
          samples were built from a fabricated 0.5 SoC fallback (since removed)
          and are unrecoverable poison.
        * **Changed nominal capacity**: when ``current_nominal_kwh`` (the live
          config value) differs from the persisted nominal by more than
          ``_NOMINAL_CHANGE_EPS_KWH`` — e.g. batteries were added — the old
          observations describe a differently sized pack and would only slow
          re-learning, so the estimate resets to the new nominal.

        When supplied, ``current_nominal_kwh`` is authoritative (config is the
        source of truth for nameplate capacity); the persisted nominal is used
        only to detect the change.

        Args:
            data: Dict previously produced by to_dict().
            round_trip_efficiency: Battery round-trip efficiency injected from
                config (not persisted); see :meth:`__init__`.
            current_nominal_kwh: Live configured nominal capacity. When None
                (legacy callers, tests) the persisted nominal is kept and no
                nominal-change purge occurs.

        Returns:
            Restored CapacityEstimator — with historical observations when the
            schema is current and the nominal is unchanged, else empty
            (nominal-only).
        """
        persisted_nominal = data["nominal_capacity_kwh"]
        nominal = (
            persisted_nominal if current_nominal_kwh is None else current_nominal_kwh
        )
        nominal_changed = (
            current_nominal_kwh is not None
            and abs(current_nominal_kwh - persisted_nominal) > _NOMINAL_CHANGE_EPS_KWH
        )
        if (
            data.get("schema_version", 1) < _CAPACITY_SCHEMA_VERSION
            or nominal_changed
        ):
            return cls(
                nominal_capacity_kwh=nominal,
                round_trip_efficiency=round_trip_efficiency,
            )
        observations = [
            CapacityObservation(
                timestamp=datetime.fromisoformat(o["timestamp"]),
                soc_start=o["soc_start"],
                soc_end=o["soc_end"],
                energy_kwh=o["energy_kwh"],
                direction=o["direction"],
            )
            for o in data.get("observations", [])
        ]
        return cls(
            nominal_capacity_kwh=nominal,
            observations=observations,
            round_trip_efficiency=round_trip_efficiency,
        )
