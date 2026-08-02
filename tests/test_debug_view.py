"""Tests for the /api/sun_sale/debug view."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.contract.models import (
    BatteryState,
    CalculationResult,
    GenerationSeries,
    PriceSeries,
    Schedule,
    ScheduleSlot,
    SlotDecision,
    StorageMode,
    TariffConfig,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from custom_components.sun_sale.orchestration.debug_view import (
    SunSaleDebugView,
    _coordinator_to_dict,
)
from tests.conftest import BASE_DT, default_tariff_config, make_price

BASE = datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC)
NOW = BASE_DT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_slot(hour_offset: int = 0, mode: StorageMode = StorageMode.SelfUse) -> ScheduleSlot:
    start = BASE + timedelta(hours=hour_offset)
    return ScheduleSlot(
        start=start,
        end=start + timedelta(hours=1),
        mode=mode,
        power_kw=0.0,
        expected_soc_after=0.5,
        expected_profit_eur=0.0,
        reason="test",
    )


def _make_price_series() -> PriceSeries:
    prices = [make_price(h, 0.10) for h in range(4)]
    return build_price_series(prices, default_tariff_config(), now=NOW)


def _make_gen_series() -> GenerationSeries:
    return GenerationSeries(slots=())


def _make_calculation(price_series: PriceSeries) -> CalculationResult:
    slots = tuple(
        SlotDecision(
            start=s.start, end=s.end,
            expected_solar_kwh=0.0,
            expected_solar_negative_sale_kwh=0.0,
            notes=(),
        )
        for s in price_series.slots
    )
    return CalculationResult(
        slots=slots, feed_in_lockout_windows=(), total_negative_sale_kwh=0.0, computed_at=NOW
    )


def make_coordinator(
    *,
    automation_enabled: bool = True,
    include_schedule: bool = True,
    last_dispatched_action: str | None = "idle",
    last_dispatched_at: datetime | None = None,
    use_standby: bool = True,
    allow_grid_charging: bool = True,
    allow_feed_in: bool = True,
    allow_discharge_to_grid: bool = True,
    mode_change_penalty_eur_per_kwh: float = 0.005,
    profitability_tilt_alpha: float = 0.5,
    terminal_value_discount: float = 0.5,
) -> MagicMock:
    coord = MagicMock()
    coord.automation_enabled = automation_enabled
    coord.last_dispatched_action = last_dispatched_action
    coord.last_dispatched_at = last_dispatched_at or BASE
    coord.use_standby = use_standby
    coord.allow_grid_charging = allow_grid_charging
    coord.allow_feed_in = allow_feed_in
    coord.allow_discharge_to_grid = allow_discharge_to_grid
    coord.mode_change_penalty_eur_per_kwh = mode_change_penalty_eur_per_kwh
    coord.profitability_tilt_alpha = profitability_tilt_alpha
    coord.terminal_value_discount = terminal_value_discount

    tariff_cfg = TariffConfig(
        distribution_fee=0.03,
        tax_rate=0.21,
        markup=0.01,
        sell_distribution_fee=0.02,
        sell_tax_rate=0.0,
        sell_markup=0.005,
    )
    coord.tariff_config = tariff_cfg

    schedule = Schedule(
        slots=[make_slot(0, StorageMode.GridCharge), make_slot(1, StorageMode.Discharge)],
        total_expected_profit_eur=0.42,
        degradation_cost_per_kwh=0.02,
        computed_at=BASE,
    ) if include_schedule else None

    ps = _make_price_series()
    gs = _make_gen_series()
    calc = _make_calculation(ps)

    coord.data = {
        "pricing": ps,
        "forecast": gs,
        "calculation": calc,
        "schedule": schedule,
        "battery_state": BatteryState(soc=0.62, estimated_capacity_kwh=9.8),
        "degradation_cost": 0.018,
        "estimated_capacity": 9.8,
        "prices": [],
        "grid_power_kw": 0.1,
    }
    return coord


# ---------------------------------------------------------------------------
# _coordinator_to_dict shape tests
# ---------------------------------------------------------------------------

def test_required_top_level_keys_present():
    coord = make_coordinator()
    result = _coordinator_to_dict("entry_abc", coord)

    for key in ("entry_id", "timestamp", "automation_enabled", "inputs",
                "pipeline", "outputs", "last_dispatched_action", "last_dispatched_at"):
        assert key in result, f"missing top-level key: {key}"


def test_entry_id_matches():
    coord = make_coordinator()
    result = _coordinator_to_dict("my_entry", coord)
    assert result["entry_id"] == "my_entry"


def test_automation_enabled_propagated():
    result_on = _coordinator_to_dict("e", make_coordinator(automation_enabled=True))
    result_off = _coordinator_to_dict("e", make_coordinator(automation_enabled=False))
    assert result_on["automation_enabled"] is True
    assert result_off["automation_enabled"] is False


def test_schedule_slots_serialised():
    coord = make_coordinator(include_schedule=True)
    result = _coordinator_to_dict("e", coord)

    schedule = result["outputs"]["schedule"]
    assert schedule is not None
    assert len(schedule["slots"]) == 2

    slot = schedule["slots"][0]
    for key in ("start", "end", "mode", "power_kw", "expected_profit_eur", "reason"):
        assert key in slot, f"missing slot key: {key}"

    assert slot["mode"] == "grid_charge"
    assert isinstance(slot["start"], str)


def test_schedule_none_when_no_schedule():
    coord = make_coordinator(include_schedule=False)
    result = _coordinator_to_dict("e", coord)
    assert result["outputs"]["schedule"] is None


def test_pipeline_pricing_present():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    pricing = result["pipeline"]["pricing"]
    assert pricing is not None
    assert "slot_count" in pricing
    assert "slots" in pricing
    assert len(pricing["slots"]) == 4
    slot = pricing["slots"][0]
    for key in ("start", "buy", "sell", "spot"):
        assert key in slot, f"missing pricing slot key: {key}"


def test_pipeline_forecast_present():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    forecast = result["pipeline"]["forecast"]
    assert forecast is not None
    assert forecast["slots"] == []


def test_pipeline_calculation_present():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    calc = result["pipeline"]["calculation"]
    assert calc is not None
    assert "total_negative_sale_kwh" in calc
    assert "feed_in_lockout_windows" in calc
    assert "slots" in calc


def test_battery_state_in_inputs():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    battery = result["inputs"]["battery"]
    assert battery is not None
    assert abs(battery["soc"] - 0.62) < 1e-9
    assert battery["estimated_capacity_kwh"] is not None


def test_tariff_config_serialised():
    coord = make_coordinator()
    result = _coordinator_to_dict("e", coord)
    tc = result["inputs"]["tariff_config"]
    assert tc is not None
    assert "distribution_fee" in tc
    assert "tax_rate" in tc


def test_last_dispatched_fields():
    coord = make_coordinator(
        last_dispatched_action="charge_from_grid",
        last_dispatched_at=BASE,
    )
    result = _coordinator_to_dict("e", coord)
    assert result["last_dispatched_action"] == "charge_from_grid"
    assert result["last_dispatched_at"] == BASE.isoformat()


def test_last_dispatched_at_none_is_null():
    coord = make_coordinator(last_dispatched_at=None)
    coord.last_dispatched_at = None
    result = _coordinator_to_dict("e", coord)
    assert result["last_dispatched_at"] is None


# ---------------------------------------------------------------------------
# Schedule-policy block — reflects coordinator toggle + knob state.
# ---------------------------------------------------------------------------


def test_schedule_policy_present_and_default():
    coord = make_coordinator()
    policy = _coordinator_to_dict("e", coord)["pipeline"]["schedule_policy"]
    assert policy["use_standby"] is True
    assert policy["allow_grid_charging"] is True
    assert policy["allow_feed_in"] is True
    assert policy["allow_discharge_to_grid"] is True
    assert policy["mode_change_penalty_eur_per_kwh"] == pytest.approx(0.005)
    assert policy["profitability_tilt_alpha"] == pytest.approx(0.5)
    assert policy["terminal_value_discount"] == pytest.approx(0.5)


def test_schedule_policy_reflects_disabled_flags():
    coord = make_coordinator(
        use_standby=False,
        allow_grid_charging=False,
        allow_feed_in=False,
        allow_discharge_to_grid=False,
        mode_change_penalty_eur_per_kwh=0.02,
        profitability_tilt_alpha=0.0,
        terminal_value_discount=0.25,
    )
    policy = _coordinator_to_dict("e", coord)["pipeline"]["schedule_policy"]
    assert policy["use_standby"] is False
    assert policy["allow_grid_charging"] is False
    assert policy["allow_feed_in"] is False
    assert policy["allow_discharge_to_grid"] is False
    assert policy["mode_change_penalty_eur_per_kwh"] == pytest.approx(0.02)
    assert policy["profitability_tilt_alpha"] == pytest.approx(0.0)
    assert policy["terminal_value_discount"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# SunSaleDebugView: verify url / auth attributes
# ---------------------------------------------------------------------------

def test_view_url_and_auth():
    view = SunSaleDebugView()
    assert view.url == "/api/sun_sale/debug"
    assert view.requires_auth is True
    assert view.name == "api:sun_sale:debug"
