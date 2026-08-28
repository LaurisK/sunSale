"""Schedule stage: SoC-bucketed DP over the price horizon → per-slot StorageMode.

Pure Python — no Home Assistant imports.

Algorithm: backward dynamic programming on (slot_index, soc_bucket) cells with
the action set {SelfUse, NoExport, StandBy, GridCharge, Discharge, FeedIn}. Per-slot
physics is delegated to ``pipeline.slot_physics.simulate_slot`` so the planner
and any diagnostics see identical energy flows for the same (mode, slot) pair.

Scope:
  - phase 2: SoC-bucketed DP, no baseload, no terminal value, no mode-change penalty.
  - phase 3: per-slot baseload from BaseLoadProfile (local-hour buckets).
  - phase 4 (this file): terminal value tilted by ProfitabilityScore; mode-change
    penalty scaled by battery throughput; state augmented to (t, soc, prev_mode).
  - phase 4 (deferred): ε-improvement gate against the persisted last schedule —
    requires coordinator-side wiring beyond this module.

The DP recurrence:

    V[T][s]   = 0
    V[t][s]   = max_a  reward(s, a, slot_t) + V[t+1][bucket(soc_after(s, a))]
    choice[t][s] = argmax_a of the same expression

Forward-roll from the current (non-snapped) SoC picks the per-slot mode by
looking up ``choice[t][bucket(current_soc)]`` and re-running ``simulate_slot``
at the exact SoC to keep rewards and projected SoC continuous.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, tzinfo
from statistics import median

from ..contract.models import (
    DISPATCHABLE_MODES,
    BaseLoadProfile,
    BatteryConfig,
    BatteryState,
    CalculationResult,
    PriceSeries,
    PriceSlot,
    ProfitabilityScore,
    Schedule,
    ScheduleSlot,
    StorageMode,
)
from .slot_physics import SlotOutcome, simulate_slot

# Action set the DP may pick — the canonical dispatchable-mode tuple shared
# with the operator override (see contract.models.DISPATCHABLE_MODES). The
# inverter control module dispatches whichever mode the DP picks.
_ACTIONS: tuple[StorageMode, ...] = DISPATCHABLE_MODES

# SoC discretization. 51 buckets across [min_soc, max_soc] gives ~1.7% steps
# on the default 0.10..0.95 envelope — about 170 Wh per bucket on a 10 kWh
# pack. The DP is O(T × B × |A|²) with the mode-change penalty; with T≈288
# (72 h @ 15-min) and |A|=6 that stays well under 10 ms.
_SOC_BUCKETS = 51

# Default mode-change penalty: a small EUR cost per storage-side kWh moved by
# the battery whenever the chosen mode differs from the previous slot's mode.
# Scales with throughput so passive→passive transitions (StandBy ↔ SelfUse with no
# battery activity) are free, while flapping between GridCharge/Discharge gets punished.
DEFAULT_MODE_CHANGE_PENALTY_EUR_PER_KWH = 0.005

# Strength of the profitability tilt on the terminal value. score=1 (today is
# a peak day) → no boost; score=0 (today is unusually cheap) → +α boost on
# end-SoC value, biasing the DP toward holding charge for a better day.
DEFAULT_PROFITABILITY_TILT_ALPHA = 0.5

# Discount applied to the end-of-horizon SoC valuation. The horizon is 72 h
# but the integration runs forever; the discount acknowledges that next
# horizon's prices are not guaranteed to match this horizon's median. Without
# it the DP behaves as if end-SoC could be sold at the in-horizon median for
# certain, which makes it hoard charge past every horizon boundary.
DEFAULT_TERMINAL_VALUE_DISCOUNT = 0.5


def optimize_schedule(
    price_series: PriceSeries,
    calc: CalculationResult,
    battery_config: BatteryConfig,
    battery_state: BatteryState,
    degradation_cost: float,
    now: datetime,
    base_load_profile: BaseLoadProfile | None = None,
    local_tz: tzinfo | None = None,
    export_limit_kw: float | None = None,
    profitability_score: ProfitabilityScore | None = None,
    current_mode: StorageMode | None = None,
    mode_change_penalty: float = DEFAULT_MODE_CHANGE_PENALTY_EUR_PER_KWH,
    use_standby: bool = True,
    allow_grid_charging: bool = True,
    allow_feed_in: bool = True,
    allow_discharge_to_grid: bool = True,
    profitability_tilt_alpha: float = DEFAULT_PROFITABILITY_TILT_ALPHA,
    terminal_value_discount: float = DEFAULT_TERMINAL_VALUE_DISCOUNT,
    max_discharge_to_grid_kw: float | None = None,
    forecast_reserve_soc: float = 0.0,
) -> Schedule:
    """Compute a future StorageMode schedule via SoC-bucketed dynamic programming.

    Args:
        price_series: Full price series; only slots ending after ``now`` are scheduled.
        calc: CalculationResult providing per-slot expected solar generation.
        battery_config: Battery limits (capacity, power, SoC bounds, efficiency).
        battery_state: Current SoC and estimated usable capacity.
        degradation_cost: EUR/kWh cycle wear cost from DegradationNode.
        now: Cycle timestamp; slots whose ``end <= now`` are skipped.
        base_load_profile: 24-bucket household baseload profile (local hour);
            when ``None`` baseload is treated as 0 for every slot.
        local_tz: Local timezone used to map slot timestamps to baseload-profile
            buckets; required when ``base_load_profile`` is provided.
        export_limit_kw: Deployment-wide export cap in kW; ``None`` for uncapped.
        profitability_score: Today's day-class-normalised peak percentile; tilts
            the terminal value of end-of-horizon SoC. ``None`` (cold start)
            collapses to a neutral 0.5.
        current_mode: The inverter's mode at the start of the forward roll.
            Used to charge the first-slot mode-change penalty so the schedule
            doesn't flap on every cycle. ``None`` skips the first-slot charge.
        mode_change_penalty: EUR per storage-side kWh moved by the battery
            when the chosen mode differs from the previous slot's mode.
        use_standby: When False, StandBy is removed from the DP action set so the
            planner picks SelfUse during no-generation windows (battery stays
            available to cover load instead of sitting idle).
        allow_grid_charging: When False, GridCharge is removed from the DP action
            set so the planner never force-charges the battery from grid.
        allow_feed_in: When False, FeedIn is removed; export to the grid still
            happens through SelfUse when over-cap solar exists, but the
            explicit feed-in-priority mode is not selected.
        allow_discharge_to_grid: When False, the explicit Discharge mode is
            removed; the battery may still discharge to cover local load via
            SelfUse / NoExport.
        profitability_tilt_alpha: Strength of the profitability-score bias on
            end-of-horizon SoC valuation. 0 disables the bias.
        terminal_value_discount: Multiplier applied to the in-horizon median
            sell price when valuing end-of-horizon SoC. 0 disables terminal
            valuation entirely (matches phase-2/3 behaviour).
        max_discharge_to_grid_kw: Optional AC-power cap applied only to the
            Discharge-to-grid mode; ``None`` means use hardware max. Does not
            affect discharge in SelfUse/NoExport (load cover).
        forecast_reserve_soc: Extra SoC fraction the planner may not spend,
            held back against the generation forecast being wrong. Raises the
            floor of the DP's SoC envelope only — it never forces a charge, and
            a battery already below the raised floor is not stranded (the
            bucketer clamps such a state to its lowest bucket). 0 disables it.

    Returns:
        Schedule with one ScheduleSlot per future price slot.
    """
    if not price_series.slots:
        return _empty_schedule(degradation_cost, now)

    decision_by_start = {d.start: d for d in calc.slots}
    future_slots: list[PriceSlot] = [
        p for p in price_series.slots
        if p.end > now and p.start in decision_by_start
    ]
    if not future_slots:
        return _empty_schedule(degradation_cost, now)

    cap_kwh = battery_state.estimated_capacity_kwh
    slot_hours = price_series.resolution.total_seconds() / 3600.0

    solar_kwh = [
        decision_by_start[p.start].expected_solar_kwh for p in future_slots
    ]
    baseload_kwh = _baseload_per_slot(
        future_slots, base_load_profile, local_tz, slot_hours,
    )

    # The reserve is a planning floor, not a hardware limit: the DP simply may
    # not schedule its way below it. Two clamps keep it well-behaved —
    #   * under max_soc, so a huge reserve degrades to "hold everything"
    #     instead of inverting the envelope;
    #   * never above the *current* SoC, because a reserve cannot retroactively
    #     create charge. A battery already under the floor must still be
    #     schedulable (and must not be reported as fuller than it is); the
    #     reserve then simply stops it being drained further.
    reserve = max(0.0, forecast_reserve_soc)
    floor_soc = min(
        battery_config.max_soc,
        battery_config.min_soc + reserve,
        max(battery_config.min_soc, battery_state.soc),
    )
    # Threaded through the slot physics, not just the value lookup: the DP's
    # bucketing only shapes which mode is chosen, while the discharge depth
    # itself is bounded by the config handed to simulate_slot.
    planning_config = (
        replace(battery_config, min_soc=floor_soc)
        if floor_soc > battery_config.min_soc else battery_config
    )
    bucketer = _Bucketer(floor_soc, battery_config.max_soc, _SOC_BUCKETS)

    actions = _filter_actions(
        _ACTIONS,
        use_standby=use_standby,
        allow_grid_charging=allow_grid_charging,
        allow_feed_in=allow_feed_in,
        allow_discharge_to_grid=allow_discharge_to_grid,
    )

    # Degenerate envelope (min_soc == max_soc) — no usable battery; emit StandBy.
    if not bucketer.has_envelope:
        return _standby_only_schedule(
            future_slots, baseload_kwh, solar_kwh, slot_hours,
            planning_config, cap_kwh, degradation_cost, export_limit_kw, now,
        )

    terminal_per_kwh = _terminal_value_per_storage_kwh(
        price_series, battery_config.round_trip_efficiency,
        profitability_score, profitability_tilt_alpha,
        terminal_value_discount,
    )

    choice = _run_dp(
        future_slots, baseload_kwh, solar_kwh, slot_hours,
        planning_config, cap_kwh, degradation_cost, export_limit_kw,
        bucketer, terminal_per_kwh, mode_change_penalty, actions,
        max_discharge_to_grid_kw, floor_soc,
    )

    return _forward_roll(
        future_slots, baseload_kwh, solar_kwh, slot_hours,
        planning_config, battery_state, cap_kwh, degradation_cost,
        export_limit_kw, bucketer, choice, now,
        current_mode, mode_change_penalty, actions,
        max_discharge_to_grid_kw,
    )


def _filter_actions(
    actions: tuple[StorageMode, ...],
    *,
    use_standby: bool,
    allow_grid_charging: bool,
    allow_feed_in: bool,
    allow_discharge_to_grid: bool,
) -> tuple[StorageMode, ...]:
    """Drop disabled modes from the action set per the user-toggled policy.

    Args:
        actions: Full DP action tuple.
        use_standby: Keep StandBy when True.
        allow_grid_charging: Keep GridCharge when True.
        allow_feed_in: Keep FeedIn when True.
        allow_discharge_to_grid: Keep Discharge when True.

    Returns:
        Filtered action tuple, preserving original order. SelfUse is always
        retained as a safe fallback — if the caller managed to disable every
        policy flag we still need at least one mode for the DP.
    """
    excluded: set[StorageMode] = set()
    if not use_standby:
        excluded.add(StorageMode.StandBy)
    if not allow_grid_charging:
        excluded.add(StorageMode.GridCharge)
    if not allow_feed_in:
        excluded.add(StorageMode.FeedIn)
    if not allow_discharge_to_grid:
        excluded.add(StorageMode.Discharge)
    if not excluded:
        return actions
    filtered = tuple(a for a in actions if a not in excluded)
    return filtered if filtered else (StorageMode.SelfUse,)


# ---------------------------------------------------------------------------
# Terminal value
# ---------------------------------------------------------------------------


def _terminal_value_per_storage_kwh(
    price_series: PriceSeries,
    eff: float,
    profitability_score: ProfitabilityScore | None,
    tilt_alpha: float,
    horizon_discount: float,
) -> float:
    """Per storage-side kWh worth of end-of-horizon SoC, tilted by profitability.

    The base figure is the median positive sell price across the horizon,
    discounted by the round-trip efficiency: a storage kWh sells for
    ``eff × sell``. The profitability tilt multiplies that base by
    ``1 + α·(1 − score)`` — when today is unusually cheap (score → 0) end-SoC
    is worth more; when today is at the high end (score → 1) end-SoC gets no
    boost. A missing score is treated as the neutral 0.5. The horizon
    discount caps the optimism: a real next-horizon sell at the in-horizon
    median is not guaranteed.

    Args:
        price_series: Full price series used to estimate "typical" future sell.
        eff: Round-trip efficiency (storage → AC).
        profitability_score: Day-class-normalised peak percentile, or ``None``.
        tilt_alpha: Strength of the tilt; 0.5 = ±50% range across the score.
        horizon_discount: Multiplier applied to the final terminal value;
            0 disables the terminal valuation entirely (matches phase-2/3
            behaviour and lets tests opt out of the look-ahead).

    Returns:
        EUR worth per kWh of storage-side energy held at end-of-horizon.
        Returns 0 when no positive sell price exists in the horizon or when
        the discount collapses it.
    """
    if horizon_discount <= 0:
        return 0.0
    positive_sells = [s.sell_eur_kwh for s in price_series.slots if s.sell_eur_kwh > 0]
    if not positive_sells:
        return 0.0
    base = median(positive_sells) * eff
    score = (
        profitability_score.score
        if profitability_score is not None and profitability_score.score is not None
        else 0.5
    )
    tilt = 1.0 + tilt_alpha * (1.0 - score)
    return base * tilt * horizon_discount


# ---------------------------------------------------------------------------
# Baseload lookup
# ---------------------------------------------------------------------------


def _baseload_per_slot(
    future_slots: list[PriceSlot],
    base_load_profile: BaseLoadProfile | None,
    local_tz: tzinfo | None,
    slot_hours: float,
) -> list[float]:
    """Build the per-slot expected household baseload kWh series.

    Slot kWh = profile.at(slot.start) × slot_hours. When the profile is
    absent (or the local timezone wasn't supplied) we fall back to zero so
    the DP still produces a valid (if optimistic) schedule.

    Args:
        future_slots: Slots to schedule.
        base_load_profile: Per-hour baseload profile, or ``None``.
        local_tz: Local timezone used by ``BaseLoadProfile.at``.
        slot_hours: Slot duration in hours.

    Returns:
        List of expected baseload kWh aligned with ``future_slots``.
    """
    if base_load_profile is None or local_tz is None:
        return [0.0] * len(future_slots)
    return [
        base_load_profile.at(p.start, local_tz) * slot_hours
        for p in future_slots
    ]


# ---------------------------------------------------------------------------
# DP internals
# ---------------------------------------------------------------------------


class _Bucketer:
    """Maps between continuous SoC and discrete bucket indices.

    Buckets are evenly spaced from min_soc to max_soc, inclusive. Bucket 0
    is min_soc; bucket N-1 is max_soc. ``has_envelope`` is False when the
    SoC envelope collapses to a point — the DP must fall back to StandBy.
    """

    def __init__(self, min_soc: float, max_soc: float, n_buckets: int) -> None:
        """Set up the bucketing for the given SoC envelope.

        Args:
            min_soc: Minimum permitted SoC (0..1).
            max_soc: Maximum permitted SoC (0..1).
            n_buckets: Number of discrete buckets.
        """
        self._min = min_soc
        self._max = max_soc
        self._n = n_buckets
        self._span = max_soc - min_soc
        self.has_envelope = self._span > 0 and n_buckets > 1
        self._step = self._span / (n_buckets - 1) if self.has_envelope else 0.0

    @property
    def n_buckets(self) -> int:
        """Number of buckets in the discretization."""
        return self._n

    def to_index(self, soc: float) -> int:
        """Snap a continuous SoC to its nearest bucket index, clamped to range.

        Args:
            soc: Continuous SoC value.

        Returns:
            Bucket index in [0, n_buckets-1].
        """
        if self._step == 0:
            return 0
        idx = int(round((soc - self._min) / self._step))
        if idx < 0:
            return 0
        if idx >= self._n:
            return self._n - 1
        return idx

    def to_soc(self, index: int) -> float:
        """Convert a bucket index back to its representative SoC.

        Args:
            index: Bucket index.

        Returns:
            Continuous SoC at the bucket centre.
        """
        return self._min + index * self._step

    def to_interp(self, soc: float) -> tuple[int, int, float]:
        """Locate ``soc`` between two adjacent buckets for value interpolation.

        Returns the pair of bucket indices that bracket ``soc`` plus the
        fractional position toward the upper one. Callers compute an
        interpolated value as ``(1 - frac) * v[lo] + frac * v[hi]``. When
        ``soc`` falls outside the envelope, ``lo == hi`` and ``frac == 0``
        so the lookup degenerates to the boundary bucket. Without this
        interpolation the DP's value lookup snaps to a single bucket and
        loses sub-bucket SoC gains — e.g. a small solar surplus stored
        each slot looks identical to feeding in, so the planner picks the
        immediate revenue and burns through prime storage opportunities.

        Args:
            soc: Continuous SoC value.

        Returns:
            ``(lo_index, hi_index, frac_to_hi)``.
        """
        if self._step == 0:
            return 0, 0, 0.0
        raw = (soc - self._min) / self._step
        if raw <= 0:
            return 0, 0, 0.0
        if raw >= self._n - 1:
            return self._n - 1, self._n - 1, 0.0
        lo = int(raw)
        return lo, lo + 1, raw - lo


def _run_dp(
    future_slots: list[PriceSlot],
    baseload_kwh: list[float],
    solar_kwh: list[float],
    slot_hours: float,
    battery_config: BatteryConfig,
    cap_kwh: float,
    deg_cost: float,
    export_limit_kw: float | None,
    bucketer: _Bucketer,
    terminal_per_kwh: float,
    mode_change_penalty: float,
    actions: tuple[StorageMode, ...],
    max_discharge_to_grid_kw: float | None = None,
    floor_soc: float | None = None,
) -> list[list[list[StorageMode]]]:
    """Backward DP — compute the optimal mode for every (slot, soc_bucket, prev_mode) cell.

    State is augmented with ``prev_mode`` so the mode-change penalty can be
    charged correctly during recursion. The terminal value
    ``(soc − min_soc) × cap × terminal_per_kwh`` rewards keeping charge in
    the battery at end-of-horizon; the profitability tilt is already baked
    into ``terminal_per_kwh``.

    Args:
        future_slots: Slots to schedule (chronological).
        baseload_kwh: Expected baseload per slot (length matches ``future_slots``).
        solar_kwh: Expected solar per slot (length matches ``future_slots``).
        slot_hours: Slot duration in hours.
        battery_config: Battery limits.
        cap_kwh: Estimated usable capacity.
        deg_cost: Degradation cost EUR/kWh.
        export_limit_kw: Deployment export cap.
        bucketer: SoC bucketization helper.
        terminal_per_kwh: EUR worth per storage-side kWh held at end-of-horizon.
        mode_change_penalty: EUR per storage-kWh moved when mode changes.
        actions: Modes the DP may pick from (possibly filtered by policy).
        max_discharge_to_grid_kw: AC-power cap for Discharge mode; ``None``
            means hardware max.
        floor_soc: Bottom of the DP's SoC envelope, which the forecast reserve
            may have raised above ``battery_config.min_soc``. Terminal value is
            measured from this same floor so held charge is not double-counted
            as being "above minimum". ``None`` falls back to the battery's own
            minimum.

    Returns:
        ``choice[t][b][m]`` — the optimal StorageMode for slot ``t`` when the
        current SoC bucket is ``b`` and the previous slot's mode was index
        ``m`` (or ``len(actions)`` for "no previous mode" at slot 0).
    """
    n_slots = len(future_slots)
    n_buckets = bucketer.n_buckets
    n_modes = len(actions)
    # Slot t's "previous mode" index ranges over 0..n_modes (with n_modes meaning
    # "no previous", used at the very first decision when the inverter mode is
    # unknown). We size every layer the same for symmetry; the extra column
    # costs negligible memory.
    n_prev = n_modes + 1
    sentinel_prev = n_modes
    fallback_mode = actions[0]

    # value[t][b][m] — best total reward from slot t through T given soc b and
    # previous mode index m. value[n_slots] holds the terminal valuation per
    # bucket, identical across all prev_mode columns.
    value: list[list[list[float]]] = [
        [[0.0] * n_prev for _ in range(n_buckets)]
        for _ in range(n_slots + 1)
    ]
    terminal_floor = battery_config.min_soc if floor_soc is None else floor_soc
    for b in range(n_buckets):
        v_term = max(0.0, (bucketer.to_soc(b) - terminal_floor)) \
                 * cap_kwh * terminal_per_kwh
        for m in range(n_prev):
            value[n_slots][b][m] = v_term

    choice: list[list[list[StorageMode]]] = [
        [[fallback_mode] * n_prev for _ in range(n_buckets)]
        for _ in range(n_slots)
    ]

    for t in range(n_slots - 1, -1, -1):
        price = future_slots[t]
        next_layer = value[t + 1]
        for b in range(n_buckets):
            soc_in = bucketer.to_soc(b)
            # Per-action outcome is independent of prev_mode (only the penalty
            # depends on it), so compute outcomes once per (b, action) and
            # reuse across prev_mode columns. The next-layer value is read
            # via linear interpolation between the two buckets bracketing
            # the resulting SoC — snap-to-bucket loses sub-bucket gains
            # (e.g. storing a small solar surplus every slot), which makes
            # the DP miss long arbitrage chains built out of tiny per-slot
            # SoC moves.
            per_action: list[tuple[SlotOutcome, int, int, float, float]] = []
            for action in actions:
                outcome = simulate_slot(
                    soc_in=soc_in,
                    mode=action,
                    solar_kwh=solar_kwh[t],
                    baseload_kwh=baseload_kwh[t],
                    buy_eur_kwh=price.buy_eur_kwh,
                    sell_eur_kwh=price.sell_eur_kwh,
                    slot_hours=slot_hours,
                    battery_cfg=battery_config,
                    est_capacity_kwh=cap_kwh,
                    deg_cost_eur_kwh=deg_cost,
                    export_limit_kw=export_limit_kw,
                    max_discharge_to_grid_kw=max_discharge_to_grid_kw,
                )
                lo, hi, frac = bucketer.to_interp(outcome.soc_out)
                throughput = _storage_throughput(outcome, battery_config.round_trip_efficiency)
                per_action.append((outcome, lo, hi, frac, throughput))

            for prev_m in range(n_prev):
                best_total = float("-inf")
                best_mode = fallback_mode
                for action_idx, action in enumerate(actions):
                    outcome, lo, hi, frac, throughput = per_action[action_idx]
                    # Penalty applies only when the chosen action differs from
                    # the previous mode and the battery actually moves energy.
                    if prev_m == sentinel_prev or prev_m == action_idx:
                        penalty = 0.0
                    else:
                        penalty = mode_change_penalty * throughput
                    v_next = (
                        next_layer[lo][action_idx]
                        + frac * (next_layer[hi][action_idx] - next_layer[lo][action_idx])
                    )
                    total = outcome.reward_eur - penalty + v_next
                    if total > best_total:
                        best_total = total
                        best_mode = action
                value[t][b][prev_m] = best_total
                choice[t][b][prev_m] = best_mode

    return choice


def _storage_throughput(outcome: SlotOutcome, eff: float) -> float:
    """Storage-side throughput in kWh — charge_ac + discharge_ac / eff.

    Args:
        outcome: simulate_slot result.
        eff: Round-trip efficiency.

    Returns:
        Total storage-side energy moved through the battery this slot.
    """
    return outcome.batt_charge_kwh + (
        outcome.batt_discharge_kwh / eff if eff > 0 else 0.0
    )


def _forward_roll(
    future_slots: list[PriceSlot],
    baseload_kwh: list[float],
    solar_kwh: list[float],
    slot_hours: float,
    battery_config: BatteryConfig,
    battery_state: BatteryState,
    cap_kwh: float,
    deg_cost: float,
    export_limit_kw: float | None,
    bucketer: _Bucketer,
    choice: list[list[list[StorageMode]]],
    now: datetime,
    current_mode: StorageMode | None,
    mode_change_penalty: float,
    actions: tuple[StorageMode, ...],
    max_discharge_to_grid_kw: float | None = None,
    forecast_reserve_soc: float = 0.0,
) -> Schedule:
    """Walk the chosen policy forward from the actual current SoC and mode.

    The policy lookup uses the bucket containing the current SoC together with
    the previous slot's mode index, so the mode-change penalty is honoured.
    Per-slot energy flows are re-simulated at the *exact* SoC so the displayed
    SoC trajectory and rewards remain continuous between bucket boundaries.

    Args:
        future_slots: Slots to schedule.
        baseload_kwh: Per-slot baseload kWh.
        solar_kwh: Per-slot solar kWh.
        slot_hours: Slot duration in hours.
        battery_config: Battery limits.
        battery_state: Current SoC and estimated capacity.
        cap_kwh: Estimated usable capacity.
        deg_cost: Degradation cost EUR/kWh.
        export_limit_kw: Deployment export cap.
        bucketer: SoC bucketization helper.
        choice: DP policy ``choice[t][b][m] -> StorageMode``.
        now: Cycle timestamp.
        current_mode: Mode active on the inverter at start of horizon; used to
            charge the first-slot mode-change penalty. ``None`` skips it.
        mode_change_penalty: EUR per storage-kWh moved on a mode change.
        actions: Modes the DP considered (must match ``_run_dp``).
        max_discharge_to_grid_kw: AC-power cap for Discharge mode; ``None``
            means hardware max.

    Returns:
        Schedule with per-slot StorageMode, projected SoC, reward, and reason.
        Per-slot reward includes any mode-change penalty already deducted.
    """
    soc = max(battery_config.min_soc, min(battery_config.max_soc, battery_state.soc))
    schedule_slots: list[ScheduleSlot] = []
    total_profit = 0.0
    sentinel_prev = len(actions)
    prev_idx = _mode_to_index(current_mode, sentinel_prev, actions)

    for t, price in enumerate(future_slots):
        bucket = bucketer.to_index(soc)
        mode = choice[t][bucket][prev_idx]
        outcome = simulate_slot(
            soc_in=soc,
            mode=mode,
            solar_kwh=solar_kwh[t],
            baseload_kwh=baseload_kwh[t],
            buy_eur_kwh=price.buy_eur_kwh,
            sell_eur_kwh=price.sell_eur_kwh,
            slot_hours=slot_hours,
            battery_cfg=battery_config,
            est_capacity_kwh=cap_kwh,
            deg_cost_eur_kwh=deg_cost,
            export_limit_kw=export_limit_kw,
            max_discharge_to_grid_kw=max_discharge_to_grid_kw,
        )
        action_idx = actions.index(mode)
        if prev_idx == sentinel_prev or prev_idx == action_idx:
            penalty = 0.0
        else:
            penalty = mode_change_penalty * _storage_throughput(
                outcome, battery_config.round_trip_efficiency,
            )
        adjusted_reward = outcome.reward_eur - penalty
        schedule_slots.append(_make_schedule_slot(
            price, mode, outcome, slot_hours, adjusted_reward,
        ))
        total_profit += adjusted_reward
        soc = outcome.soc_out
        prev_idx = action_idx

    return Schedule(
        slots=schedule_slots,
        total_expected_profit_eur=total_profit,
        degradation_cost_per_kwh=deg_cost,
        computed_at=now,
    )


def _mode_to_index(
    mode: StorageMode | None,
    sentinel: int,
    actions: tuple[StorageMode, ...],
) -> int:
    """Resolve an external StorageMode to its index in the DP action list.

    Modes that are not in the DP action set (TRACK, UNKNOWN, or any mode
    excluded by policy) collapse to the sentinel so the first slot is not
    penalised for "changing away" from them; this matches the convention used
    during DP construction.

    Args:
        mode: Inverter mode at the start of the forward roll, or ``None``.
        sentinel: Value returned for unknown / off-set modes.
        actions: The active DP action tuple.

    Returns:
        Index into ``actions`` or ``sentinel``.
    """
    if mode is None or mode not in actions:
        return sentinel
    return actions.index(mode)


# ---------------------------------------------------------------------------
# Per-slot record construction
# ---------------------------------------------------------------------------


def _make_schedule_slot(
    price_slot: PriceSlot,
    mode: StorageMode,
    outcome: SlotOutcome,
    slot_hours: float,
    expected_profit_eur: float | None = None,
) -> ScheduleSlot:
    """Pack a per-slot outcome into the public ScheduleSlot record.

    ``power_kw`` exposes the dominant battery flow (charge or discharge),
    converted from per-slot kWh to kW via the slot duration. Modes that move
    no battery energy (StandBy, NoExport at full battery, FeedIn with no over-cap
    surplus) report 0.

    Args:
        price_slot: Source PriceSlot supplying start/end and prices.
        mode: StorageMode the DP picked for this slot.
        outcome: simulate_slot result for the same (mode, slot) pair.
        slot_hours: Slot duration in hours (for kW conversion).
        expected_profit_eur: Optional override (e.g. when the caller has
            already subtracted a mode-change penalty); when ``None`` the
            raw ``outcome.reward_eur`` is reported.

    Returns:
        ScheduleSlot ready for downstream consumers.
    """
    flow_kwh = max(outcome.batt_charge_kwh, outcome.batt_discharge_kwh)
    power_kw = (flow_kwh / slot_hours) if slot_hours > 0 else 0.0
    profit = expected_profit_eur if expected_profit_eur is not None else outcome.reward_eur
    return ScheduleSlot(
        start=price_slot.start,
        end=price_slot.end,
        mode=mode,
        power_kw=power_kw,
        expected_soc_after=outcome.soc_out,
        expected_profit_eur=profit,
        reason=_reason_for(mode, price_slot, outcome),
    )


def _reason_for(
    mode: StorageMode, price_slot: PriceSlot, outcome: SlotOutcome,
) -> str:
    """Build the human-readable reason field for a ScheduleSlot.

    Args:
        mode: Chosen mode for the slot.
        price_slot: Price context (used for buy/sell prices in the message).
        outcome: simulate_slot result (used for the battery-flow magnitude).

    Returns:
        One-line description suitable for the dashboard.
    """
    if mode == StorageMode.GridCharge:
        return f"Grid-charge at {price_slot.buy_eur_kwh:.4f} EUR/kWh"
    if mode == StorageMode.Discharge:
        return f"Discharge to grid at {price_slot.sell_eur_kwh:.4f} EUR/kWh"
    if mode == StorageMode.FeedIn:
        return f"Feed-in priority at {price_slot.sell_eur_kwh:.4f} EUR/kWh"
    if mode == StorageMode.SelfUse:
        return f"Self-use; solar→batt {outcome.batt_charge_kwh:.2f} kWh"
    if mode == StorageMode.NoExport:
        return f"Self-use, no export; solar→batt {outcome.batt_charge_kwh:.2f} kWh"
    if mode == StorageMode.StandBy:
        return "Idle"
    return str(mode.value)


# ---------------------------------------------------------------------------
# Degenerate fallbacks
# ---------------------------------------------------------------------------


def _empty_schedule(degradation_cost: float, now: datetime) -> Schedule:
    """Return an empty Schedule with metadata fields populated."""
    return Schedule(
        slots=[],
        total_expected_profit_eur=0.0,
        degradation_cost_per_kwh=degradation_cost,
        computed_at=now,
    )


def _standby_only_schedule(
    future_slots: list[PriceSlot],
    baseload_kwh: list[float],
    solar_kwh: list[float],
    slot_hours: float,
    battery_config: BatteryConfig,
    cap_kwh: float,
    deg_cost: float,
    export_limit_kw: float | None,
    now: datetime,
) -> Schedule:
    """Emit StandBy for every slot — used when the SoC envelope is degenerate.

    Args:
        future_slots: Slots to schedule.
        baseload_kwh: Per-slot baseload kWh.
        solar_kwh: Per-slot solar kWh.
        slot_hours: Slot duration in hours.
        battery_config: Battery limits.
        cap_kwh: Estimated usable capacity.
        deg_cost: Degradation cost EUR/kWh.
        export_limit_kw: Deployment export cap.
        now: Cycle timestamp.

    Returns:
        Schedule whose every slot is StandBy.
    """
    soc = battery_config.min_soc
    schedule_slots: list[ScheduleSlot] = []
    total = 0.0
    for t, price in enumerate(future_slots):
        outcome = simulate_slot(
            soc_in=soc,
            mode=StorageMode.StandBy,
            solar_kwh=solar_kwh[t],
            baseload_kwh=baseload_kwh[t],
            buy_eur_kwh=price.buy_eur_kwh,
            sell_eur_kwh=price.sell_eur_kwh,
            slot_hours=slot_hours,
            battery_cfg=battery_config,
            est_capacity_kwh=cap_kwh,
            deg_cost_eur_kwh=deg_cost,
            export_limit_kw=export_limit_kw,
        )
        schedule_slots.append(_make_schedule_slot(price, StorageMode.StandBy, outcome, slot_hours))
        total += outcome.reward_eur
        soc = outcome.soc_out
    return Schedule(
        slots=schedule_slots,
        total_expected_profit_eur=total,
        degradation_cost_per_kwh=deg_cost,
        computed_at=now,
    )
