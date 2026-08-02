"""Tests for coordinator helpers and NordpoolTranslator parsing."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.sun_sale.contract.const import (
    CONF_NORDPOOL_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
)
from custom_components.sun_sale.contract.models import BatteryReading
from custom_components.sun_sale.inbound.pricing import NordpoolTranslator
from custom_components.sun_sale.inbound.telemetry import GenericCodec, SolisCodec
from custom_components.sun_sale.orchestration.coordinator import SunSaleCoordinator
from custom_components.sun_sale.outbound.inverter import InverterPlatform

BASE = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


def _make_coordinator():
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {
        CONF_NORDPOOL_ENTITY: "sensor.nordpool",
        CONF_SOLAR_FORECAST_ENTITY: "sensor.solar",
    }
    coord = SunSaleCoordinator(hass, entry)
    coord._config = dict(entry.data)
    return coord, hass


def _make_translator(entity_id: str = "sensor.nordpool"):
    return NordpoolTranslator(entity_id=entity_id)


def _state(attrs: dict) -> MagicMock:
    s = MagicMock()
    s.attributes = attrs
    return s


def _hass_with(entity_id: str, attrs: dict) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: _state(attrs) if eid == entity_id else None
    return hass


# ---------------------------------------------------------------------------
# NordpoolTranslator.parse — raw_today / raw_tomorrow format
# ---------------------------------------------------------------------------

def test_nordpool_empty_when_entity_missing():
    t = _make_translator()
    hass = MagicMock()
    hass.states.get.return_value = None
    result = t.parse(hass, now=BASE)
    assert result.entries == []


def test_nordpool_parses_raw_today():
    t = _make_translator()
    entry = {"start": "2024-01-15T10:00:00+00:00", "value": 0.10}
    hass = _hass_with("sensor.nordpool", {"raw_today": [entry], "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    # At least one entry is parsed; zero-fill may add more to reach 48h coverage
    assert len(result.entries) >= 1
    real_entries = [e for e in result.entries if e.price_eur_kwh != 0.0]
    assert len(real_entries) == 1
    assert abs(real_entries[0].price_eur_kwh - 0.10) < 1e-9


def test_nordpool_resolution_detected():
    t = _make_translator()
    entries = [
        {"start": "2024-01-15T10:00:00+00:00", "value": 0.10},
        {"start": "2024-01-15T10:15:00+00:00", "value": 0.11},
    ]
    hass = _hass_with("sensor.nordpool", {"raw_today": entries, "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    assert result.resolution == timedelta(minutes=15)


def test_nordpool_deduplicates_slots():
    t = _make_translator()
    dup = {"start": "2024-01-15T10:00:00+00:00", "value": 0.10}
    hass = _hass_with("sensor.nordpool", {"raw_today": [dup, dup], "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    # Only one unique non-fill entry; the 48h fill adds zero-priced entries.
    real_entries = [e for e in result.entries if e.price_eur_kwh != 0.0]
    assert len(real_entries) == 1


def test_nordpool_includes_tomorrow():
    t = _make_translator()
    today_e = {"start": "2024-01-15T10:00:00+00:00", "value": 0.10}
    tomorrow_e = {"start": "2024-01-16T10:00:00+00:00", "value": 0.09}
    hass = _hass_with("sensor.nordpool", {"raw_today": [today_e], "raw_tomorrow": [tomorrow_e]})
    result = t.parse(hass, now=BASE)
    assert len(result.entries) == 2


def test_nordpool_slot_duration_15min():
    t = _make_translator()
    entries = [
        {"start": "2024-01-15T10:00:00+00:00", "value": 0.10},
        {"start": "2024-01-15T10:15:00+00:00", "value": 0.11},
    ]
    hass = _hass_with("sensor.nordpool", {"raw_today": entries, "raw_tomorrow": []})
    result = t.parse(hass, now=BASE)
    assert result.entries[0].end == result.entries[0].start + timedelta(minutes=15)


# ---------------------------------------------------------------------------
# NordpoolTranslator.parse — legacy flat list format
# ---------------------------------------------------------------------------

def test_nordpool_legacy_parses_today():
    t = _make_translator()
    hass = _hass_with("sensor.nordpool", {"today": [0.10, 0.12, 0.08], "tomorrow": []})
    result = t.parse(hass, now=BASE)
    real_entries = [e for e in result.entries if e.price_eur_kwh != 0.0]
    assert len(real_entries) == 3
    assert abs(real_entries[0].price_eur_kwh - 0.10) < 1e-9


def test_nordpool_legacy_skips_none_entries():
    t = _make_translator()
    hass = _hass_with("sensor.nordpool", {"today": [0.10, None, 0.08], "tomorrow": []})
    result = t.parse(hass, now=BASE)
    real_entries = [e for e in result.entries if e.price_eur_kwh != 0.0]
    assert len(real_entries) == 2


def test_nordpool_legacy_slot_spans_one_hour():
    t = _make_translator()
    hass = _hass_with("sensor.nordpool", {"today": [0.10], "tomorrow": []})
    result = t.parse(hass, now=BASE)
    today_entries = [e for e in result.entries if e.start.date().isoformat() == "2024-01-15"]
    assert today_entries[0].end == today_entries[0].start + timedelta(hours=1)


# ---------------------------------------------------------------------------
# _build_capacity_observation — anchor accumulator + counter energy
# ---------------------------------------------------------------------------

def _reading(
    soc: float, charge_kwh: float = 0.0, discharge_kwh: float = 0.0,
    power_kw: float = 0.0,
) -> BatteryReading:
    return BatteryReading(
        soc=soc, power_kw=power_kw, grid_power_kw=0.0, household_load_kw=0.2,
        charge_energy_today_kwh=charge_kwh, discharge_energy_today_kwh=discharge_kwh,
    )


def _set_anchor(coord, reading, at=BASE):
    coord._cap_anchor_reading = reading
    coord._cap_anchor_at = at


def test_capacity_obs_first_call_anchors_and_returns_none():
    coord, _ = _make_coordinator()
    assert coord._build_capacity_observation(_reading(0.30, 1.0, 0.5), BASE) is None
    assert coord._cap_anchor_reading is not None
    assert coord._cap_anchor_at == BASE


def test_capacity_obs_small_swing_holds_anchor():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.30, 1.0, 0.5))
    # +5% swing (< 10% emit threshold) → no obs, anchor unchanged.
    assert coord._build_capacity_observation(
        _reading(0.35, 1.6, 0.5), BASE + timedelta(minutes=20)) is None
    assert coord._cap_anchor_reading.soc == 0.30


def test_capacity_obs_missing_counters_reanchors_no_learning():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.30, 1.0, 0.5))
    no_counter = BatteryReading(
        soc=0.45, power_kw=0.0, grid_power_kw=0.0, household_load_kw=0.2)
    assert coord._build_capacity_observation(
        no_counter, BASE + timedelta(minutes=30)) is None


def test_capacity_obs_clean_charge_emits_counter_energy():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.28, charge_kwh=2.3, discharge_kwh=4.4))
    # +30% charge with the discharge counter frozen (pure), +7.3 kWh charged.
    obs = coord._build_capacity_observation(
        _reading(0.58, charge_kwh=9.6, discharge_kwh=4.4), BASE + timedelta(hours=6))
    assert obs is not None
    assert obs.direction == "charge"
    assert abs(obs.soc_start - 0.28) < 1e-9
    assert abs(obs.soc_end - 0.58) < 1e-9
    assert abs(obs.energy_kwh - 7.3) < 1e-9
    assert coord._cap_anchor_reading.soc == 0.58  # anchor advanced


def test_capacity_obs_clean_discharge_emits_counter_energy():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.38, charge_kwh=2.1, discharge_kwh=1.0))
    obs = coord._build_capacity_observation(
        _reading(0.26, charge_kwh=2.1, discharge_kwh=4.3),
        BASE + timedelta(hours=1, minutes=30))
    assert obs is not None
    assert obs.direction == "discharge"
    assert abs(obs.energy_kwh - 3.3) < 1e-9


def test_capacity_obs_counter_reset_reanchors():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.20, charge_kwh=10.1, discharge_kwh=14.6))
    # Midnight: counters reset to ~0 even though the SoC swing would qualify.
    obs = coord._build_capacity_observation(
        _reading(0.35, charge_kwh=0.0, discharge_kwh=0.1), BASE + timedelta(hours=1))
    assert obs is None
    assert coord._cap_anchor_reading.soc == 0.35


def test_capacity_obs_impure_window_discarded():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.30, charge_kwh=2.0, discharge_kwh=2.0))
    # +12% net charge but the discharge counter also advanced a lot (battery
    # oscillated): off (2.0) > 0.15 × on (3.0) → discard, advance past it.
    obs = coord._build_capacity_observation(
        _reading(0.42, charge_kwh=5.0, discharge_kwh=4.0), BASE + timedelta(hours=2))
    assert obs is None
    assert coord._cap_anchor_reading.soc == 0.42


def test_capacity_obs_stale_window_reanchors():
    coord, _ = _make_coordinator()
    _set_anchor(coord, _reading(0.30, charge_kwh=2.0, discharge_kwh=2.0))
    # Window older than CAPACITY_OBS_MAX_WINDOW_S (12 h) → abandon, re-anchor.
    obs = coord._build_capacity_observation(
        _reading(0.60, charge_kwh=9.0, discharge_kwh=2.0), BASE + timedelta(hours=13))
    assert obs is None
    assert coord._cap_anchor_reading.soc == 0.60


# ---------------------------------------------------------------------------
# Pipeline stage keys in coordinator.data (built manually, no coordinator run)
# ---------------------------------------------------------------------------

def _make_pipeline_data() -> dict:
    from custom_components.sun_sale.contract.models import GenerationSeries
    from custom_components.sun_sale.inbound.pricing import build_price_series
    from custom_components.sun_sale.pipeline.battery import degradation_cost_per_kwh
    from custom_components.sun_sale.pipeline.calculation import calculate
    from custom_components.sun_sale.pipeline.schedule import optimize_schedule
    from tests.conftest import (
        default_battery_config,
        default_battery_state,
        default_tariff_config,
        make_price,
    )

    now = BASE
    prices = [make_price(h, 0.10) for h in range(4)]
    ps = build_price_series(prices, default_tariff_config(), now=now)
    gen = GenerationSeries(slots=())
    bs = default_battery_state()
    calc = calculate(ps, gen, bs, now)
    bc = default_battery_config()
    deg = degradation_cost_per_kwh(bc, bs)
    schedule = optimize_schedule(ps, calc, bc, bs, deg, now)
    return {"pricing": ps, "forecast": gen, "calculation": calc, "schedule": schedule}


def test_pipeline_keys_present_in_coordinator_data():
    data = _make_pipeline_data()
    for key in ("pricing", "forecast", "calculation", "schedule"):
        assert key in data


def test_pipeline_pricing_is_price_series():
    from custom_components.sun_sale.contract.models import PriceSeries
    assert isinstance(_make_pipeline_data()["pricing"], PriceSeries)


def test_pipeline_forecast_is_generation_series():
    from custom_components.sun_sale.contract.models import GenerationSeries
    assert isinstance(_make_pipeline_data()["forecast"], GenerationSeries)


def test_pipeline_calculation_is_calculation_result():
    from custom_components.sun_sale.contract.models import CalculationResult
    assert isinstance(_make_pipeline_data()["calculation"], CalculationResult)


def test_pipeline_schedule_is_schedule():
    from custom_components.sun_sale.contract.models import Schedule
    assert isinstance(_make_pipeline_data()["schedule"], Schedule)


# ---------------------------------------------------------------------------
# dispatch_mode_override — lightweight panel-button dispatch path
# ---------------------------------------------------------------------------

async def test_dispatch_mode_override_noop_before_control_module():
    """Returns quietly when the control module isn't built yet (early setup)."""
    coord, _ = _make_coordinator()
    coord._control_module = None
    await coord.dispatch_mode_override()  # must not raise


async def test_dispatch_mode_override_forwards_to_control_module():
    """Forwards override + schedule to dispatch_override without a full refresh."""
    from unittest.mock import AsyncMock

    from custom_components.sun_sale.contract.models import StorageMode

    coord, _ = _make_coordinator()
    module = MagicMock()
    module.dispatch_override = AsyncMock()
    coord._control_module = module
    coord.mode_override = StorageMode.Discharge
    coord.automation_enabled = False
    schedule = _make_pipeline_data()["schedule"]
    coord.data = {"schedule": schedule}
    coord.async_request_refresh = AsyncMock()
    coord.async_update_listeners = MagicMock()

    await coord.dispatch_mode_override()

    module.dispatch_override.assert_awaited_once()
    kwargs = module.dispatch_override.await_args.kwargs
    assert kwargs["mode_override"] == StorageMode.Discharge
    assert kwargs["schedule"] is schedule
    assert kwargs["automation_enabled"] is False
    # The lightweight path must not trigger a full DAG refresh...
    coord.async_request_refresh.assert_not_awaited()
    # ...but must push fresh state to the panel.
    coord.async_update_listeners.assert_called_once()


# ---------------------------------------------------------------------------
# Accounting isolation — a bookkeeping failure must never abort the cycle or
# block the inverter dispatch (_guarded + best-effort _persist_secondary_outputs)
# ---------------------------------------------------------------------------

def test_guarded_swallows_exception():
    """A raising body inside _guarded does not propagate out of the with-block."""
    coord, _ = _make_coordinator()
    with coord._guarded("boom"):
        raise ValueError("simulated corrupt record")
    # Reaching this line means the exception was contained.


def test_guarded_passes_through_success():
    """_guarded does not interfere with a normally-completing body."""
    coord, _ = _make_coordinator()
    ran = []
    with coord._guarded("ok"):
        ran.append(1)
    assert ran == [1]


async def test_persist_secondary_outputs_isolates_block_failure():
    """A failing persistence block is contained; later blocks still run."""
    from unittest.mock import AsyncMock

    from custom_components.sun_sale.contract.models import (
        ForecastAccuracyResult,
        MonthlyBillResult,
    )

    coord, _ = _make_coordinator()
    # Forecast-quality save blows up (e.g. disk error); the monthly-bill save
    # that follows must still be attempted — each block is guarded on its own.
    fq_store = MagicMock()
    fq_store.save = AsyncMock(side_effect=RuntimeError("disk full"))
    bill_store = MagicMock()
    bill_store.save = AsyncMock()
    coord._forecast_quality_store = fq_store
    coord._monthly_bill_store = bill_store

    acc = MagicMock()
    bill = MagicMock()
    secondary = {ForecastAccuracyResult: acc, MonthlyBillResult: bill}

    # Must not raise despite the forecast-quality save failing.
    await coord._persist_secondary_outputs(primary={}, secondary=secondary, now=BASE)

    fq_store.save.assert_awaited_once()
    bill_store.save.assert_awaited_once()


async def test_dispatch_inverter_mode_noop_without_control_module():
    """Dispatch is a quiet no-op before the control module is built."""
    coord, _ = _make_coordinator()
    coord._control_module = None
    await coord._dispatch_inverter_mode(primary={}, secondary={}, now=BASE)  # must not raise


# ---------------------------------------------------------------------------
# _build_live_flow_sources — per-leg entity / scale / sign specs for the panel
# ---------------------------------------------------------------------------

def _coord_for_flow_sources(platform, ids, pv="", ac_port="", backup="", units=None):
    """Build a coordinator with the flow-source attrs + a unit-returning hass."""
    coord, hass = _make_coordinator()
    coord._inverter_platform = platform
    coord._telemetry_codec = (
        SolisCodec() if platform == InverterPlatform.SOLIS else GenericCodec()
    )
    coord._inverter_entity_ids = ids
    coord._pv_power_entity_id = pv
    coord._ac_port_power_entity_id = ac_port
    coord._backup_power_entity_id = backup
    unit_map = units or {}
    hass.states.get.side_effect = (
        lambda eid: _state({"unit_of_measurement": unit_map[eid]}) if eid in unit_map else None
    )
    return coord


def test_live_flow_sources_solis_signed_roles():
    """Solis prefers signed battery/grid roles; net sensors flip vs as-is."""
    coord = _coord_for_flow_sources(
        InverterPlatform.SOLIS,
        ids={"battery_power_signed": "sensor.bp", "grid_power": "sensor.gp"},
        pv="sensor.pv", ac_port="sensor.acp", backup="sensor.bk",
        units={
            "sensor.bp": "W", "sensor.gp": "kW", "sensor.pv": "W",
            "sensor.acp": "W", "sensor.bk": "W",
        },
    )
    src = coord._build_live_flow_sources()
    # Solis battery net sensor is discharge-positive → flipped to +charging.
    assert src["battery"] == {"entity_id": "sensor.bp", "scale": 0.001, "sign": -1}
    # Grid net already matches +import → no flip; unit kW → scale 1.0.
    assert src["grid"] == {"entity_id": "sensor.gp", "scale": 1.0, "sign": 1}
    # Source-only legs carry a clamp_min of 0; ac_port stays signed.
    assert src["solar"] == {"entity_id": "sensor.pv", "scale": 0.001, "sign": 1, "clamp_min": 0.0}
    assert src["ac_port"] == {"entity_id": "sensor.acp", "scale": 0.001, "sign": 1}
    assert src["backup"] == {"entity_id": "sensor.bk", "scale": 0.001, "sign": 1, "clamp_min": 0.0}


def test_live_flow_sources_solis_fallback_roles():
    """Without the net roles, Solis falls back to magnitude / ac-port with the right signs."""
    coord = _coord_for_flow_sources(
        InverterPlatform.SOLIS,
        ids={"battery_power": "sensor.bmag", "grid_power_fallback": "sensor.acgrid"},
        units={"sensor.bmag": "W", "sensor.acgrid": "W"},
    )
    src = coord._build_live_flow_sources()
    # Magnitude battery has no direction → read as-is (+1).
    assert src["battery"] == {"entity_id": "sensor.bmag", "scale": 0.001, "sign": 1}
    # ac-port grid fallback is inverter→grid positive → flipped on Solis.
    assert src["grid"] == {"entity_id": "sensor.acgrid", "scale": 0.001, "sign": -1}


def test_live_flow_sources_generic_platform_no_flip():
    """A non-Solis platform applies no sign flips to signed/fallback roles."""
    coord = _coord_for_flow_sources(
        InverterPlatform.GENERIC,
        ids={"battery_power_signed": "sensor.bp", "grid_power_fallback": "sensor.acgrid"},
        units={"sensor.bp": "kW", "sensor.acgrid": "kW"},
    )
    src = coord._build_live_flow_sources()
    assert src["battery"]["sign"] == 1
    assert src["grid"]["sign"] == 1


def test_live_flow_sources_solar_unit_rule():
    """Solar uses the PV translator's unit rule: only an exact 'W' is watts."""
    coord = _coord_for_flow_sources(
        InverterPlatform.SOLIS, ids={}, pv="sensor.pv", units={"sensor.pv": "kW"},
    )
    assert coord._build_live_flow_sources()["solar"]["scale"] == 1.0


def test_live_flow_sources_none_when_unmapped():
    """No mapped power entity → None, so the panel falls back to live_flows."""
    coord = _coord_for_flow_sources(InverterPlatform.SOLIS, ids={})
    assert coord._build_live_flow_sources() is None
