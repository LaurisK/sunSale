"""Per-slot physics: outcome of one StorageMode applied for one pricing slot.

Pure Python — no Home Assistant imports.

This is the single source of truth for "what does inverter mode X do during
one slot given starting SoC S, expected solar G, expected baseload B, and
buy/sell prices P". Both the DP scheduler (`pipeline/schedule.py`) and any
diagnostics layer must route through ``simulate_slot`` so the planner's
internal model and the dashboard agree on energy flows.

Conventions:

  - Energies are slot-totals in kWh (positive).
  - ``grid_in_kwh`` is imported from the grid; ``grid_out_kwh`` is exported.
  - ``batt_charge_kwh`` is AC energy drawn into the battery (no loss on the
    charge leg, matching ``pipeline.battery.trade_profit_per_kwh``). The
    storage-side SoC gain is ``batt_charge_kwh / est_capacity_kwh``.
  - ``batt_discharge_kwh`` is AC energy delivered by the battery; the
    underlying storage drain is ``batt_discharge_kwh / round_trip_efficiency``.
  - ``curtailed_kwh`` is solar generation that the mode neither used,
    stored, nor exported.
  - ``reward_eur`` = ``grid_out * sell − grid_in * buy − throughput * deg``,
    where throughput is the storage-side battery energy moved (charge AC +
    discharge AC / efficiency); matches the round-trip ``deg × 2`` accounting
    used by ``trade_profit_per_kwh``.

slot_physics never "second-guesses" a mode: FeedIn/SelfUse will export
even when ``sell_eur_kwh < 0`` because that's what the hardware does. The
scheduler is responsible for picking a price-aware mode (NoExport or StandBy)
during negative-sell windows; the DP sees the negative reward and learns.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contract.models import BatteryConfig, StorageMode

# ---------------------------------------------------------------------------
# Outcome record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotOutcome:
    """Result of applying one StorageMode for one slot."""

    soc_out: float                # battery SoC at end of slot (0..1)
    grid_in_kwh: float            # AC kWh imported from grid (>=0)
    grid_out_kwh: float           # AC kWh exported to grid (>=0)
    batt_charge_kwh: float        # AC kWh charged into battery (>=0)
    batt_discharge_kwh: float     # AC kWh delivered from battery (>=0)
    curtailed_kwh: float          # solar kWh neither used, stored, nor exported (>=0)
    reward_eur: float             # net reward for the slot (revenue − cost − deg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def simulate_slot(
    *,
    soc_in: float,
    mode: StorageMode,
    solar_kwh: float,
    baseload_kwh: float,
    buy_eur_kwh: float,
    sell_eur_kwh: float,
    slot_hours: float,
    battery_cfg: BatteryConfig,
    est_capacity_kwh: float,
    deg_cost_eur_kwh: float,
    export_limit_kw: float | None = None,
    max_discharge_to_grid_kw: float | None = None,
) -> SlotOutcome:
    """Compute the outcome of applying ``mode`` for one slot.

    Args:
        soc_in: Battery state-of-charge at the start of the slot (0..1).
        mode: Inverter StorageMode to apply for the whole slot.
        solar_kwh: Expected solar generation during the slot (>=0).
        baseload_kwh: Expected household baseload draw during the slot (>=0).
        buy_eur_kwh: Effective grid buy price (already includes tariff fees).
        sell_eur_kwh: Effective grid sell price; may be zero or negative.
        slot_hours: Slot duration in hours (e.g. 0.25 for 15-min slots).
        battery_cfg: Battery limits (capacity, power, SoC bounds, efficiency).
        est_capacity_kwh: Learned usable capacity in kWh.
        deg_cost_eur_kwh: Per-kWh cycle wear cost from DegradationNode.
        export_limit_kw: Optional export cap applied to SelfUse/FeedIn exports;
            ``None`` means uncapped. NoExport never exports; Discharge is
            uncapped; GridCharge curtails any solar surplus because grid-charge
            takes the charge bus.
        max_discharge_to_grid_kw: Optional AC-power cap (kW) applied only to the
            Discharge-to-grid mode; ``None`` means uncapped (hardware limit
            from ``battery_cfg.max_discharge_power_kw`` applies). Does not
            affect battery discharge in SelfUse/NoExport (load cover).

    Returns:
        SlotOutcome describing every energy flow and the resulting reward.
    """
    cap_kwh = max(0.0, est_capacity_kwh)
    eff = battery_cfg.round_trip_efficiency
    max_charge_kwh = max(0.0, battery_cfg.max_charge_power_kw * slot_hours)
    max_discharge_kwh = max(0.0, battery_cfg.max_discharge_power_kw * slot_hours)
    headroom_kwh = max(0.0, (battery_cfg.max_soc - soc_in) * cap_kwh)
    drawdown_storage_kwh = max(0.0, (soc_in - battery_cfg.min_soc) * cap_kwh)
    # Storage→AC ratio: 1 kWh of storage drained → eff kWh delivered to AC.
    max_discharge_storage_kwh = max_discharge_kwh / eff if eff > 0 else 0.0
    export_cap_kwh = (
        export_limit_kw * slot_hours if export_limit_kw is not None else float("inf")
    )

    # Per-mode export caps mirror storage_mode_specs.build_specs():
    #   SelfUse / FeedIn                 → caller-supplied cap (inf when not set)
    #   NoExport / StandBy / GridCharge  → 0 (export disabled by the spec)
    #   Discharge                        → sim caps only via max_discharge_to_grid_kw;
    #                                      the spec writes the deployment export cap
    if mode == StorageMode.StandBy:
        # Hold: charge from surplus allowed, discharge barred, export disabled.
        flows = _simulate_self_use(
            solar_kwh, baseload_kwh, headroom_kwh,
            drawdown_storage_kwh, max_charge_kwh, max_discharge_kwh=0.0, eff=eff,
            export_cap_kwh=0.0,
        )
    elif mode == StorageMode.SelfUse:
        flows = _simulate_self_use(
            solar_kwh, baseload_kwh, headroom_kwh,
            drawdown_storage_kwh, max_charge_kwh, max_discharge_kwh, eff,
            export_cap_kwh=export_cap_kwh,
        )
    elif mode == StorageMode.NoExport:
        flows = _simulate_self_use(
            solar_kwh, baseload_kwh, headroom_kwh,
            drawdown_storage_kwh, max_charge_kwh, max_discharge_kwh, eff,
            export_cap_kwh=0.0,
        )
    elif mode == StorageMode.GridCharge:
        flows = _simulate_grid_charge(
            solar_kwh, baseload_kwh, headroom_kwh,
            max_charge_kwh, export_cap_kwh=0.0,
        )
    elif mode == StorageMode.Discharge:
        if max_discharge_to_grid_kw is not None and eff > 0:
            cap_storage_kwh = max_discharge_to_grid_kw * slot_hours / eff
            effective_discharge_storage_kwh = min(max_discharge_storage_kwh, cap_storage_kwh)
        else:
            effective_discharge_storage_kwh = max_discharge_storage_kwh
        flows = _simulate_discharge(
            solar_kwh, baseload_kwh, drawdown_storage_kwh,
            effective_discharge_storage_kwh, eff,
        )
    elif mode == StorageMode.FeedIn:
        flows = _simulate_feed_in(
            solar_kwh, baseload_kwh, headroom_kwh,
            drawdown_storage_kwh, max_charge_kwh, max_discharge_kwh, eff,
            export_cap_kwh,
        )
    else:
        # TRACK / UNKNOWN — inert placeholder; the DP must not propose them.
        flows = _simulate_idle(
            solar_kwh, baseload_kwh, export_cap_kwh=0.0,
        )

    soc_out = _new_soc(soc_in, flows.batt_charge, flows.batt_discharge, cap_kwh, eff)
    soc_out = _clamp(soc_out, battery_cfg.min_soc, battery_cfg.max_soc)

    throughput_storage = flows.batt_charge + (
        flows.batt_discharge / eff if eff > 0 else 0.0
    )
    reward = (
        flows.grid_out * sell_eur_kwh
        - flows.grid_in * buy_eur_kwh
        - throughput_storage * deg_cost_eur_kwh
    )

    return SlotOutcome(
        soc_out=soc_out,
        grid_in_kwh=flows.grid_in,
        grid_out_kwh=flows.grid_out,
        batt_charge_kwh=flows.batt_charge,
        batt_discharge_kwh=flows.batt_discharge,
        curtailed_kwh=flows.curtailed,
        reward_eur=reward,
    )


# ---------------------------------------------------------------------------
# Internal: per-mode flow computations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Flows:
    """Raw kWh flows before SoC and reward are computed."""

    grid_in: float
    grid_out: float
    batt_charge: float
    batt_discharge: float
    curtailed: float


def _simulate_idle(
    solar_kwh: float,
    baseload_kwh: float,
    export_cap_kwh: float,
) -> _Flows:
    """Inert fallback: battery idle; solar covers load; surplus per the cap.

    The energy model for modes the planner must not schedule (TRACK / UNKNOWN):
    the battery neither charges nor discharges, solar covers baseload first, and
    any surplus exports up to the cap (callers pass 0 to curtail instead). Kept
    deliberately do-nothing so an unschedulable mode can never fabricate battery
    activity in the DP's valuation. StandBy is *not* routed here — it holds
    charge but still soaks surplus solar (see ``simulate_slot``).

    Args:
        solar_kwh: Expected solar generation for the slot.
        baseload_kwh: Expected household draw for the slot.
        export_cap_kwh: Export cap in kWh.

    Returns:
        _Flows for the slot.
    """
    deficit, surplus = _solar_vs_load(solar_kwh, baseload_kwh)
    grid_out, curtailed = _export_or_curtail(surplus, export_cap_kwh)
    return _Flows(
        grid_in=deficit,
        grid_out=grid_out,
        batt_charge=0.0,
        batt_discharge=0.0,
        curtailed=curtailed,
    )


def _simulate_self_use(
    solar_kwh: float,
    baseload_kwh: float,
    headroom_kwh: float,
    drawdown_storage_kwh: float,
    max_charge_kwh: float,
    max_discharge_kwh: float,
    eff: float,
    export_cap_kwh: float,
) -> _Flows:
    """Self-use family (SelfUse / NoExport): battery balances solar vs load.

    Battery discharges to cover baseload deficit; surplus solar charges the
    battery; remaining surplus exports (capped) or curtails. Pass the
    configured cap for SelfUse and ``0.0`` for NoExport.

    Args:
        solar_kwh: Expected solar generation for the slot.
        baseload_kwh: Expected household draw for the slot.
        headroom_kwh: Available battery charge headroom this slot.
        drawdown_storage_kwh: Storage-side energy available for discharge.
        max_charge_kwh: Max AC charge energy this slot.
        max_discharge_kwh: Max AC discharge energy this slot.
        eff: Round-trip efficiency (used to translate storage drawdown to AC).
        export_cap_kwh: Export cap (0 disables export entirely).

    Returns:
        _Flows for the slot.
    """
    deficit, surplus = _solar_vs_load(solar_kwh, baseload_kwh)

    # Battery covers as much of the AC deficit as physics permits.
    # Storage drawdown × eff is the AC energy the storage can deliver this slot.
    max_ac_discharge = min(max_discharge_kwh, drawdown_storage_kwh * eff)
    batt_discharge = min(deficit, max_ac_discharge)
    grid_in = deficit - batt_discharge

    # Battery soaks up as much surplus as headroom and the charge limit allow.
    batt_charge = min(surplus, headroom_kwh, max_charge_kwh)
    leftover = surplus - batt_charge

    grid_out, curtailed = _export_or_curtail(leftover, export_cap_kwh)

    return _Flows(
        grid_in=grid_in,
        grid_out=grid_out,
        batt_charge=batt_charge,
        batt_discharge=batt_discharge,
        curtailed=curtailed,
    )


def _simulate_grid_charge(
    solar_kwh: float,
    baseload_kwh: float,
    headroom_kwh: float,
    max_charge_kwh: float,
    export_cap_kwh: float,
) -> _Flows:
    """GridCharge: force grid charge; solar covers baseload, surplus curtails (export=0).

    Battery is force-charged from the grid at the maximum charge rate
    (clamped by available headroom). Solar still covers any baseload it
    can; the deficit is imported on top of the GridCharge charge. Surplus
    solar cannot export — GridCharge's export limit is zero by spec — so
    it curtails unless the caller passes a non-zero ``export_cap_kwh``
    (kept as a parameter for symmetry with the other modes).

    Args:
        solar_kwh: Expected solar generation for the slot.
        baseload_kwh: Expected household draw for the slot.
        headroom_kwh: Available battery charge headroom this slot.
        max_charge_kwh: Max AC charge energy this slot.
        export_cap_kwh: Export cap in kWh (defaults to 0 from caller).

    Returns:
        _Flows for the slot.
    """
    batt_charge = min(headroom_kwh, max_charge_kwh)
    baseload_from_solar = min(solar_kwh, baseload_kwh)
    baseload_from_grid = baseload_kwh - baseload_from_solar
    surplus = solar_kwh - baseload_from_solar

    grid_out, curtailed = _export_or_curtail(surplus, export_cap_kwh)
    grid_in = batt_charge + baseload_from_grid

    return _Flows(
        grid_in=grid_in,
        grid_out=grid_out,
        batt_charge=batt_charge,
        batt_discharge=0.0,
        curtailed=curtailed,
    )


def _simulate_discharge(
    solar_kwh: float,
    baseload_kwh: float,
    drawdown_storage_kwh: float,
    max_discharge_storage_kwh: float,
    eff: float,
) -> _Flows:
    """Discharge: force battery discharge (uncapped export); solar exports too.

    Battery discharges at the maximum rate (clamped by storage drawdown
    available). All AC produced (battery + solar) serves baseload first;
    the remainder exports unconditionally — Discharge has no export cap
    and runs even when the sell price is negative. The scheduler must not
    pick Discharge under negative prices.

    Args:
        solar_kwh: Expected solar generation for the slot.
        baseload_kwh: Expected household draw for the slot.
        drawdown_storage_kwh: Storage-side energy available for discharge.
        max_discharge_storage_kwh: Max storage drain this slot
            (=max_discharge_kwh / eff).
        eff: Round-trip efficiency applied on the discharge leg.

    Returns:
        _Flows for the slot.
    """
    storage_drained = min(drawdown_storage_kwh, max_discharge_storage_kwh)
    batt_discharge_ac = storage_drained * eff

    ac_available = batt_discharge_ac + solar_kwh
    baseload_covered = min(baseload_kwh, ac_available)
    ac_after_load = ac_available - baseload_covered
    grid_in = max(0.0, baseload_kwh - baseload_covered)
    grid_out = ac_after_load

    return _Flows(
        grid_in=grid_in,
        grid_out=grid_out,
        batt_charge=0.0,
        batt_discharge=batt_discharge_ac,
        curtailed=0.0,
    )


def _simulate_feed_in(
    solar_kwh: float,
    baseload_kwh: float,
    headroom_kwh: float,
    drawdown_storage_kwh: float,
    max_charge_kwh: float,
    max_discharge_kwh: float,
    eff: float,
    export_cap_kwh: float,
) -> _Flows:
    """FeedIn: export-priority for surplus PV; battery still covers load deficit.

    Surplus PV exports first (up to ``export_cap_kwh``); whatever exceeds the
    cap charges the battery. Household load is served from the battery before
    importing, exactly like self-use. The battery never exports to grid here —
    forced battery→grid export is the Discharge mode's RC setpoint. Export
    occurs even at non-positive sell prices (hardware behaviour, not a planning
    choice).

    Args:
        solar_kwh: Expected solar generation for the slot.
        baseload_kwh: Expected household draw for the slot.
        headroom_kwh: Available battery charge headroom this slot.
        drawdown_storage_kwh: Storage-side energy available for discharge.
        max_charge_kwh: Max AC charge energy this slot.
        max_discharge_kwh: Max AC discharge energy this slot.
        eff: Round-trip efficiency (translates storage drawdown to AC).
        export_cap_kwh: Export cap in kWh.

    Returns:
        _Flows for the slot.
    """
    baseload_from_solar = min(solar_kwh, baseload_kwh)
    baseload_deficit = baseload_kwh - baseload_from_solar
    solar_remaining = solar_kwh - baseload_from_solar

    # A slot has either a deficit or a surplus, so the discharge branch here and
    # the charge branch below never both fire.
    max_ac_discharge = min(max_discharge_kwh, drawdown_storage_kwh * eff)
    batt_discharge = min(baseload_deficit, max_ac_discharge)
    grid_in = baseload_deficit - batt_discharge

    export_attempt = min(solar_remaining, export_cap_kwh)
    over_cap = solar_remaining - export_attempt
    batt_charge = min(over_cap, headroom_kwh, max_charge_kwh)
    curtailed = over_cap - batt_charge

    return _Flows(
        grid_in=grid_in,
        grid_out=export_attempt,
        batt_charge=batt_charge,
        batt_discharge=batt_discharge,
        curtailed=curtailed,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _solar_vs_load(solar_kwh: float, baseload_kwh: float) -> tuple[float, float]:
    """Split solar/load into (AC deficit, AC surplus) for the slot.

    Args:
        solar_kwh: Solar generation in the slot.
        baseload_kwh: Household draw in the slot.

    Returns:
        ``(deficit, surplus)`` — both non-negative; one is zero.
    """
    if solar_kwh >= baseload_kwh:
        return 0.0, solar_kwh - baseload_kwh
    return baseload_kwh - solar_kwh, 0.0


def _export_or_curtail(
    surplus_kwh: float,
    export_cap_kwh: float,
) -> tuple[float, float]:
    """Split a solar surplus into exported and curtailed parts using the cap.

    The export decision is price-blind — modes (not slot_physics) decide
    whether export is allowed by passing ``0.0`` (NoExport/StandBy/GridCharge)
    or a non-zero cap. The DP picks a price-aware mode; the resulting reward
    will reflect a loss when ``sell_eur_kwh`` is negative.

    Args:
        surplus_kwh: Solar surplus available to dispose of (>=0).
        export_cap_kwh: Per-slot export cap (0 disables export entirely).

    Returns:
        ``(grid_out_kwh, curtailed_kwh)``; sum equals ``surplus_kwh``.
    """
    if surplus_kwh <= 0:
        return 0.0, 0.0
    if export_cap_kwh <= 0:
        return 0.0, surplus_kwh
    exported = min(surplus_kwh, export_cap_kwh)
    return exported, surplus_kwh - exported


def _new_soc(
    soc_in: float,
    batt_charge_ac: float,
    batt_discharge_ac: float,
    capacity_kwh: float,
    eff: float,
) -> float:
    """Compute end-of-slot SoC from AC flow magnitudes.

    Charge is loss-free on the storage side (1 kWh AC in → 1 kWh stored);
    discharge applies the full round-trip efficiency loss on the AC side
    (1 kWh stored → eff kWh AC out). Matches the convention used by
    ``pipeline.battery.trade_profit_per_kwh``.

    Args:
        soc_in: SoC at the start of the slot.
        batt_charge_ac: AC kWh charged into the battery.
        batt_discharge_ac: AC kWh delivered from the battery.
        capacity_kwh: Usable battery capacity.
        eff: Round-trip efficiency.

    Returns:
        SoC at the end of the slot (unclamped).
    """
    if capacity_kwh <= 0:
        return soc_in
    storage_drained = batt_discharge_ac / eff if eff > 0 else 0.0
    return soc_in + (batt_charge_ac - storage_drained) / capacity_kwh


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp a value to the inclusive range [lo, hi].

    Args:
        value: Value to clamp.
        lo: Lower bound.
        hi: Upper bound.

    Returns:
        ``value`` clamped to ``[lo, hi]``.
    """
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value
