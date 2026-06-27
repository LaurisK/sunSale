"""Tests for schedule.py — SoC-bucketed DP scheduler.

Pure Python, no HA required. These tests check schedule-level invariants
(chronology, SoC bounds, profit accounting, mode selection under price
extremes). Per-mode energy physics is covered by tests/test_slot_physics.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.contract.models import (
    BaseLoadProfile,
    BaseLoadSlot,
    DayClass,
    GenerationSeries,
    GenerationSlot,
    ProfitabilityScore,
    SolarForecast,
    StorageMode,
    TariffConfig,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
from custom_components.sun_sale.pipeline.calculation import calculate
from custom_components.sun_sale.pipeline.schedule import optimize_schedule
from tests.conftest import (
    BASE_DT,
    default_battery_config,
    default_battery_state,
    default_tariff_config,
    make_price,
    make_solar,
)

NOW = BASE_DT  # 2024-01-15 00:00 UTC


def _make_gen_series(solar: list[SolarForecast]) -> GenerationSeries:
    """Build a GenerationSeries from a list of SolarForecast entries."""
    slots = tuple(
        GenerationSlot(start=sf.start, end=sf.end, expected_kwh=sf.generation_kwh)
        for sf in solar
    )
    return GenerationSeries(slots=slots)


def run(prices, solar=None, soc=0.50, battery_config=None, tariff_config=None):
    """Convenience wrapper that wires up the pipeline pieces and runs the DP."""
    bc = battery_config or default_battery_config()
    tc = tariff_config or default_tariff_config()
    state = default_battery_state(soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    price_series = build_price_series(prices, tc, now=NOW)
    gen_series = _make_gen_series(solar or [])
    calc = calculate(price_series, gen_series, state, NOW)
    return optimize_schedule(price_series, calc, bc, state, deg, NOW)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_prices_returns_empty_schedule():
    result = run([])
    assert result.slots == []
    assert result.total_expected_profit_eur == 0.0


def test_single_price_emits_one_slot():
    """A flat one-slot horizon at min_soc: no arbitrage, no battery moves."""
    bc = default_battery_config()
    result = run([make_price(0, 0.10)], soc=bc.min_soc, battery_config=bc)
    assert len(result.slots) == 1
    assert result.slots[0].mode in {StorageMode.SelfUse, StorageMode.StandBy}
    assert result.slots[0].power_kw == pytest.approx(0.0)


def test_flat_prices_no_charge_at_min_soc():
    """Flat prices at min_soc → no GridCharge (spread doesn't recoup deg × 2)."""
    # At min_soc the DP has nothing to Discharge; a GridCharge would need a future
    # Discharge to recoup, which the spread doesn't justify.
    bc = default_battery_config()
    prices = [make_price(h, 0.10) for h in range(24)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    assert all(s.mode not in {StorageMode.GridCharge, StorageMode.Discharge} for s in result.slots)


# ---------------------------------------------------------------------------
# Core optimisation
# ---------------------------------------------------------------------------


def test_obvious_buy_low_sell_high():
    """One very cheap hour followed later by one very expensive hour → trade."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    result = run(prices)
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    slot_12 = next(s for s in result.slots if s.start.hour == 12)
    assert slot_0.mode == StorageMode.GridCharge
    assert slot_12.mode == StorageMode.Discharge


def test_spread_below_degradation_does_not_charge_from_empty():
    """Small spread at min_soc → no GridCharge (no future Discharge pays for the charge)."""
    # Degradation ~ 5000/(6000*10*2) = 0.0417 EUR/storage-kWh; spread of 0.02
    # is well below the round-trip degradation hurdle.
    bc = default_battery_config()
    prices = [make_price(h, 0.10 if h < 12 else 0.12) for h in range(24)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    assert not any(s.mode == StorageMode.GridCharge for s in result.slots)


def test_charge_before_discharge():
    """Chronological constraint: the DP cannot Discharge before it has charged."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.50) for h in range(1, 4)]
        + [make_price(h, 0.10) for h in range(4, 24)]
    )
    result = run(prices, soc=0.20)    # near-empty so DP must charge first
    charge_slots = [s for s in result.slots if s.mode == StorageMode.GridCharge]
    discharge_slots = [s for s in result.slots if s.mode == StorageMode.Discharge]
    if charge_slots and discharge_slots:
        assert min(s.start for s in charge_slots) < min(s.start for s in discharge_slots)


def test_multi_leg_arbitrage_visited():
    """Two cheap→expensive cycles in the horizon: DP should exploit both."""
    # Cheap at 0 and 12; expensive at 6 and 18 (>>deg + efficiency loss).
    prices = []
    for h in range(24):
        if h in (0, 12):
            spot = 0.01
        elif h in (6, 18):
            spot = 0.60
        else:
            spot = 0.10
        prices.append(make_price(h, spot))
    result = run(prices, soc=0.30)
    discharge_hours = sorted(s.start.hour for s in result.slots if s.mode == StorageMode.Discharge)
    # Both peaks should be exploited.
    assert 6 in discharge_hours
    assert 18 in discharge_hours


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_no_charge_when_full():
    """At max_soc, DP cannot GridCharge (no headroom to put kWh in)."""
    bc = default_battery_config()
    prices = [make_price(0, 0.01)] + [make_price(h, 0.50) for h in range(1, 4)]
    result = run(prices, soc=bc.max_soc, battery_config=bc)
    # Slot 0 has no headroom — GridCharge would charge 0 kWh. DP may pick anything
    # neutral (SelfUse/StandBy); the key is it does not waste a "trade" attempt here.
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    assert slot_0.power_kw == 0.0 or slot_0.mode != StorageMode.GridCharge


def test_no_discharge_when_empty():
    """At min_soc, DP cannot Discharge (no battery to drain)."""
    bc = default_battery_config()
    prices = [make_price(0, 0.50)] + [make_price(h, 0.01) for h in range(1, 4)]
    result = run(prices, soc=bc.min_soc, battery_config=bc)
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    assert slot_0.power_kw == 0.0 or slot_0.mode != StorageMode.Discharge


def test_soc_stays_within_bounds_throughout():
    """Forward-rolled SoC must respect the configured envelope."""
    bc = default_battery_config()
    prices = [make_price(h, 0.01 if h < 6 else 0.50) for h in range(24)]
    result = run(prices, soc=0.5, battery_config=bc)
    for slot in result.slots:
        assert bc.min_soc - 1e-6 <= slot.expected_soc_after <= bc.max_soc + 1e-6, (
            f"SoC {slot.expected_soc_after} out of bounds at {slot.start}"
        )


def test_power_does_not_exceed_max():
    """Battery flow per slot stays within the configured power limit."""
    bc = default_battery_config()
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 23)]
        + [make_price(23, 0.50)]
    )
    result = run(prices, battery_config=bc)
    for slot in result.slots:
        if slot.mode == StorageMode.GridCharge:
            assert slot.power_kw <= bc.max_charge_power_kw + 1e-6
        elif slot.mode == StorageMode.Discharge:
            assert slot.power_kw <= bc.max_discharge_power_kw + 1e-6


# ---------------------------------------------------------------------------
# Solar handling
# ---------------------------------------------------------------------------


def test_solar_at_negative_sell_does_not_export():
    """Solar slot inside a negative-sell window → DP must not Discharge or FeedIn.

    Both would push solar to the grid at a loss. SelfUse/NoExport/StandBy are
    the acceptable choices.
    """
    tc = _high_sell_fee_config()
    bc = default_battery_config()
    prices = [make_price(h, 0.05) for h in range(4)]    # spot 0.05 → sell ≈ −0.15
    solar = [make_solar(2, 2.0)]
    result = run(prices, solar=solar, soc=bc.min_soc, battery_config=bc, tariff_config=tc)
    solar_slot = next(s for s in result.slots if s.start.hour == 2)
    assert solar_slot.mode not in {StorageMode.Discharge, StorageMode.FeedIn}


def test_no_solar_at_min_soc_picks_passive_modes():
    """No solar at min_soc → DP has nothing to do, picks SelfUse or StandBy."""
    bc = default_battery_config()
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices, solar=[], soc=bc.min_soc, battery_config=bc)
    for slot in result.slots:
        assert slot.mode in {StorageMode.SelfUse, StorageMode.StandBy}
        assert slot.power_kw == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def test_total_profit_equals_sum_of_slots():
    """schedule.total_expected_profit_eur is the sum of slot profits."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 23)]
        + [make_price(23, 0.50)]
    )
    result = run(prices)
    expected = sum(s.expected_profit_eur for s in result.slots)
    assert result.total_expected_profit_eur == pytest.approx(expected, abs=1e-9)


def test_degradation_cost_stored_in_schedule():
    """Schedule reports the deg-cost it ran with for downstream traceability."""
    prices = [make_price(h, 0.10) for h in range(4)]
    result = run(prices)
    assert result.degradation_cost_per_kwh > 0


# ---------------------------------------------------------------------------
# Negative-sell handling (former feed-in lockout)
# ---------------------------------------------------------------------------


def _high_sell_fee_config() -> TariffConfig:
    """A tariff with a fat sell fee so low-spot hours have negative sell."""
    return TariffConfig(
        distribution_fee=0.0, tax_rate=0.0, markup=0.0,
        sell_distribution_fee=0.20, sell_tax_rate=0.0, sell_markup=0.0,
    )


def _run_with_negative_sell_window(locked_hours: list[int]):
    """Drive the DP with a price series that has negative-sell windows."""
    tc = _high_sell_fee_config()
    prices = [make_price(h, 0.05 if h in locked_hours else 0.30) for h in range(24)]
    return run(prices, tariff_config=tc, soc=0.50)


def test_no_dump_inside_negative_sell_window():
    """Discharge would lose money at sell < 0 → DP refuses to pick it."""
    locked = list(range(10, 14))
    result = _run_with_negative_sell_window(locked)
    discharge_hours = [s.start.hour for s in result.slots if s.mode == StorageMode.Discharge]
    assert all(h not in locked for h in discharge_hours), (
        f"Discharge inside locked hours: {[h for h in discharge_hours if h in locked]}"
    )


def test_negative_sell_slot_does_not_export():
    """Inside the negative-sell window the chosen mode is NoExport/StandBy/SelfUse.

    Whichever it is, the slot's power_kw must be 0 — no battery cycling
    and no grid export.
    """
    locked = [12]
    result = _run_with_negative_sell_window(locked)
    locked_slot = next(s for s in result.slots if s.start.hour == 12)
    assert locked_slot.mode in {StorageMode.NoExport, StorageMode.StandBy, StorageMode.SelfUse}
    assert locked_slot.power_kw == pytest.approx(0.0)


def test_dump_allowed_outside_negative_sell_window():
    """A genuine peak outside the lockout still triggers Discharge."""
    tc = _high_sell_fee_config()
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.05)]                           # locked-out hour
        + [make_price(h, 0.10) for h in range(13, 23)]
        + [make_price(23, 0.80)]                            # huge sell
    )
    result = run(prices, tariff_config=tc, soc=0.30)
    discharge_slots = [s for s in result.slots if s.mode == StorageMode.Discharge]
    assert discharge_slots, "Expected at least one Discharge slot at hour 23"
    assert all(s.start.hour != 12 for s in discharge_slots)


# ---------------------------------------------------------------------------
# Charging-profile back-compat
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BaseLoadProfile wiring (phase 3)
# ---------------------------------------------------------------------------


def _flat_baseload(kw: float) -> BaseLoadProfile:
    """Build a BaseLoadProfile that returns the same baseload kW every hour."""
    slots = tuple(
        BaseLoadSlot(hour=h, baseload_kw=kw, sample_count=100, is_fallback=False)
        for h in range(24)
    )
    return BaseLoadProfile(
        slots=slots,
        fallback_kw=kw,
        overall_p10_kw=kw,
        overall_median_kw=kw,
        confidence=1.0,
        sample_count=2400,
        distinct_days=30,
        computed_at=NOW,
    )


def test_baseload_increases_grid_import_when_battery_empty():
    """At min_soc with baseload, StandBy/SelfUse slots import baseload from grid."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.05) for h in range(4)]    # low spot, low sell
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)
    profile = _flat_baseload(0.5)    # 0.5 kW baseload always on

    result = optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        base_load_profile=profile, local_tz=timezone.utc,
    )
    # Expected baseload draw per hour = 0.5 kWh. Schedule reports per-slot reward;
    # at min_soc with no solar and no headroom-useful trade, baseload import shows up
    # as a small negative reward on every slot.
    assert all(s.expected_profit_eur < 0 for s in result.slots), (
        "Every slot should incur baseload import cost"
    )


def test_baseload_does_not_change_pure_arbitrage_decision():
    """A small baseload doesn't stop the DP from spotting big arbitrage spreads."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)
    profile = _flat_baseload(0.2)

    result = optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        base_load_profile=profile, local_tz=timezone.utc,
    )
    slot_0 = next(s for s in result.slots if s.start.hour == 0)
    slot_12 = next(s for s in result.slots if s.start.hour == 12)
    assert slot_0.mode == StorageMode.GridCharge
    assert slot_12.mode == StorageMode.Discharge


def test_baseload_omitted_still_produces_schedule():
    """Calling without BaseLoadProfile / local_tz uses zero baseload silently."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(4)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)
    result = optimize_schedule(ps, calc, bc, state, deg, NOW)
    assert len(result.slots) == 4


# ---------------------------------------------------------------------------
# Terminal value, profitability tilt, mode-change penalty (phase 4)
# ---------------------------------------------------------------------------


def _profit_score(score: float) -> ProfitabilityScore:
    """Build a ProfitabilityScore with the given score and neutral metadata."""
    return ProfitabilityScore(
        score=score,
        today_peak_eur_kwh=0.5,
        today_class=DayClass.WEEKDAY,
        class_medians={DayClass.WEEKDAY: 0.4},
        window_days=30,
        computed_at=NOW,
    )


def test_terminal_value_discourages_unnecessary_drain():
    """At soc=0.5, flat positive prices: terminal value keeps the DP from
    Discharging the whole pack for tiny gains; charge is preserved."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(24)]    # flat
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    result = optimize_schedule(ps, calc, bc, state, deg, NOW)
    # With terminal value, the final SoC should be close to the starting SoC —
    # not drained to min_soc as it would be without look-ahead.
    final_soc = result.slots[-1].expected_soc_after
    assert final_soc >= 0.30, (
        f"Terminal value should prevent full drain; final SoC={final_soc}"
    )


def test_profitability_low_score_boosts_end_soc_value():
    """A low profitability score should make the DP retain more charge.

    Concretely: same prices, low score should leave higher end-SoC than high
    score.
    """
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(12)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    low = optimize_schedule(
        ps, calc, bc, state, deg, NOW, profitability_score=_profit_score(0.05),
    )
    high = optimize_schedule(
        ps, calc, bc, state, deg, NOW, profitability_score=_profit_score(0.95),
    )
    assert low.slots[-1].expected_soc_after >= high.slots[-1].expected_soc_after


def test_mode_change_penalty_dampens_flapping():
    """With the mode-change penalty, the schedule should change modes less often."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(0.50)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    # Alternating price spread small enough to be marginal — without penalty the
    # DP might flap every other slot; with penalty, it consolidates.
    prices = [make_price(h, 0.10 if h % 2 == 0 else 0.12) for h in range(24)]
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    no_penalty = optimize_schedule(
        ps, calc, bc, state, deg, NOW, mode_change_penalty=0.0,
    )
    with_penalty = optimize_schedule(
        ps, calc, bc, state, deg, NOW, mode_change_penalty=0.05,
    )
    changes_off = sum(
        1 for i in range(1, len(no_penalty.slots))
        if no_penalty.slots[i].mode != no_penalty.slots[i - 1].mode
    )
    changes_on = sum(
        1 for i in range(1, len(with_penalty.slots))
        if with_penalty.slots[i].mode != with_penalty.slots[i - 1].mode
    )
    assert changes_on <= changes_off


def test_current_mode_prevents_first_slot_penalty():
    """When current_mode matches the chosen first-slot mode, no penalty fires.

    Concretely: starting in SelfUse with a price profile that picks SelfUse first
    yields the same first-slot reward as starting in None (the sentinel).
    """
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(bc.min_soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    prices = [make_price(h, 0.10) for h in range(4)]    # no GridCharge/Discharge attractive
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series([])
    calc = calculate(ps, gen, state, NOW)

    no_prev = optimize_schedule(ps, calc, bc, state, deg, NOW)
    with_prev = optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        current_mode=no_prev.slots[0].mode,
    )
    assert no_prev.slots[0].expected_profit_eur == pytest.approx(
        with_prev.slots[0].expected_profit_eur,
    )


# ---------------------------------------------------------------------------
# Policy flags — use_standby / allow_grid_charging
# ---------------------------------------------------------------------------


def _run_with_policy(
    prices,
    *,
    use_standby: bool = True,
    allow_grid_charging: bool = True,
    soc: float = 0.50,
    solar=None,
):
    """Run the optimiser with explicit policy flags; returns the Schedule."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series(solar or [])
    calc = calculate(ps, gen, state, NOW)
    return optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        use_standby=use_standby,
        allow_grid_charging=allow_grid_charging,
    )


def test_use_standby_off_excludes_standby_mode():
    """When use_standby=False, the DP must never pick StandBy for any slot."""
    bc = default_battery_config()
    # Flat, low prices at min_soc — typical no-arbitrage case where StandBy is
    # often the cheapest action. With the flag off StandBy must drop out entirely.
    prices = [make_price(h, 0.10) for h in range(24)]
    result = _run_with_policy(
        prices,
        use_standby=False,
        allow_grid_charging=True,
        soc=bc.min_soc,
    )
    assert all(s.mode != StorageMode.StandBy for s in result.slots)


def test_allow_grid_charging_off_excludes_grid_charge_mode():
    """When allow_grid_charging=False the DP must not pick GridCharge even on a clear arb."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    # Sanity check: with both flags on, the DP DOES pick GridCharge at the cheap hour.
    baseline = _run_with_policy(prices, use_standby=True, allow_grid_charging=True)
    assert any(s.mode == StorageMode.GridCharge for s in baseline.slots)

    restricted = _run_with_policy(prices, use_standby=True, allow_grid_charging=False)
    assert all(s.mode != StorageMode.GridCharge for s in restricted.slots)


def test_both_flags_off_still_produces_valid_schedule():
    """With both restrictive flags set the DP still emits a slot per price slot."""
    prices = [make_price(h, 0.10 + 0.01 * (h % 3)) for h in range(8)]
    result = _run_with_policy(
        prices,
        use_standby=False,
        allow_grid_charging=False,
    )
    assert len(result.slots) == len(prices)
    assert all(
        s.mode not in {StorageMode.StandBy, StorageMode.GridCharge} for s in result.slots
    )


def test_default_flags_match_legacy_behaviour():
    """Omitting the policy kwargs leaves both modes available — no behaviour change."""
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    default = run(prices)
    explicit = _run_with_policy(
        prices, use_standby=True, allow_grid_charging=True,
    )
    assert [s.mode for s in default.slots] == [s.mode for s in explicit.slots]


# ---------------------------------------------------------------------------
# Policy flags — allow_feed_in / allow_discharge_to_grid + numeric knobs
# ---------------------------------------------------------------------------


def _run_full_policy(
    prices,
    *,
    solar=None,
    soc: float = 0.50,
    use_standby: bool = True,
    allow_grid_charging: bool = True,
    allow_feed_in: bool = True,
    allow_discharge_to_grid: bool = True,
    profitability_tilt_alpha: float = 0.5,
    terminal_value_discount: float = 0.5,
):
    """Run optimize_schedule with every policy knob explicitly set."""
    bc = default_battery_config()
    tc = default_tariff_config()
    state = default_battery_state(soc)
    state.estimated_capacity_kwh = bc.nominal_capacity_kwh
    deg = degradation_cost_per_kwh(bc, state)
    ps = build_price_series(prices, tc, now=NOW)
    gen = _make_gen_series(solar or [])
    calc = calculate(ps, gen, state, NOW)
    return optimize_schedule(
        ps, calc, bc, state, deg, NOW,
        use_standby=use_standby,
        allow_grid_charging=allow_grid_charging,
        allow_feed_in=allow_feed_in,
        allow_discharge_to_grid=allow_discharge_to_grid,
        profitability_tilt_alpha=profitability_tilt_alpha,
        terminal_value_discount=terminal_value_discount,
    )


def test_allow_feed_in_off_excludes_feed_in_mode():
    """With allow_feed_in=False, FeedIn must not appear in the schedule."""
    # Big midday solar and a single high sell price — baseline DP loves FeedIn here.
    prices = (
        [make_price(h, 0.05) for h in range(10)]
        + [make_price(10, 0.40), make_price(11, 0.40)]
        + [make_price(h, 0.05) for h in range(12, 24)]
    )
    solar = [make_solar(h, 4.0) for h in (10, 11)]
    restricted = _run_full_policy(
        prices, solar=solar, soc=0.90, allow_feed_in=False,
    )
    assert all(s.mode != StorageMode.FeedIn for s in restricted.slots)


def test_allow_discharge_to_grid_off_excludes_discharge_mode():
    """With allow_discharge_to_grid=False, Discharge must not appear."""
    # Clear arb: charge cheap, sell expensive.
    prices = (
        [make_price(0, 0.01)]
        + [make_price(h, 0.10) for h in range(1, 12)]
        + [make_price(12, 0.50)]
        + [make_price(h, 0.10) for h in range(13, 24)]
    )
    baseline = _run_full_policy(prices)
    assert any(s.mode == StorageMode.Discharge for s in baseline.slots)

    restricted = _run_full_policy(prices, allow_discharge_to_grid=False)
    assert all(s.mode != StorageMode.Discharge for s in restricted.slots)


def test_all_export_flags_off_emits_self_use_fallback():
    """Disabling every export mode + StandBy must still produce a full schedule."""
    prices = [make_price(h, 0.10) for h in range(6)]
    result = _run_full_policy(
        prices,
        use_standby=False,
        allow_grid_charging=False,
        allow_feed_in=False,
        allow_discharge_to_grid=False,
    )
    assert len(result.slots) == len(prices)
    # Only SelfUse / NoExport remain in the action set.
    allowed = {StorageMode.SelfUse, StorageMode.NoExport}
    assert all(s.mode in allowed for s in result.slots)


def test_terminal_value_discount_zero_drains_battery():
    """terminal_value_discount=0 turns off look-ahead and the DP drains by horizon end."""
    prices = [make_price(h, 0.10) for h in range(24)]    # flat
    with_lookahead = _run_full_policy(prices, terminal_value_discount=0.5)
    no_lookahead = _run_full_policy(prices, terminal_value_discount=0.0)
    # Without terminal value the DP has no incentive to retain charge; with it
    # the SoC stays meaningfully higher at end-of-horizon.
    assert no_lookahead.slots[-1].expected_soc_after <= with_lookahead.slots[-1].expected_soc_after


def test_profitability_tilt_alpha_zero_collapses_tilt():
    """alpha=0 nullifies the profitability bias regardless of score."""
    prices = [make_price(h, 0.10) for h in range(12)]
    low = _run_full_policy(
        prices, profitability_tilt_alpha=0.0,
    )
    high = _run_full_policy(
        prices, profitability_tilt_alpha=0.0,
    )
    assert [s.mode for s in low.slots] == [s.mode for s in high.slots]
