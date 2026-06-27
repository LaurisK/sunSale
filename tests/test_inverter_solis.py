"""Tests for the StorageMode state-machine controller in outbound/inverter.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import (
    BatteryConfig,
    StorageMode,
)
from custom_components.sun_sale.ha_state import normalize_power_to_kw
from custom_components.sun_sale.outbound.inverter import (
    InverterController,
    InverterPlatform,
)
from custom_components.sun_sale.outbound.storage_mode_specs import build_specs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SOLIS_ENTITY_IDS = {
    "battery_soc":                "sensor.solis_battery_soc",
    "battery_power":              "sensor.solis_battery_power",
    "battery_power_signed":       "sensor.solis_battery_power_net",
    "battery_charge_energy_today":    "sensor.solis_today_battery_charge_energy",
    "battery_discharge_energy_today": "sensor.solis_today_battery_discharge_energy",
    "grid_power":                 "sensor.solis_grid_power_net",
    "grid_power_fallback":        "sensor.solis_ac_grid_port_power",
    "storage_control_readback":   "sensor.solis_storage_control_word",
    "battery_max_charge_current":    "number.solis_battery_max_charge_current",
    "battery_max_discharge_current": "number.solis_battery_max_discharge_current",
    "rc_setpoint":                "number.solis_rc_active_power",
    "rc_timeout":                 "number.solis_rc_timeout",
    "rc_grid_adjustment_select":  "select.solis_rc_grid_adjustment",
    "backflow_power":             "number.solis_backflow_power",
    "self_use_switch":            "switch.solis_self_use_mode",
    "tou_mode_switch":            "switch.solis_time_of_use_mode",
    "allow_grid_charge_switch":   "switch.solis_allow_grid_to_charge_the_battery",
    "feed_in_priority_switch":    "switch.solis_feed_in_priority_mode",
}


def make_battery_config(
    nominal_voltage_v: float = 48.0,
    max_charge_kw: float = 5.0,
    max_discharge_kw: float = 5.0,
) -> BatteryConfig:
    return BatteryConfig(
        nominal_capacity_kwh=10.0,
        purchase_price_eur=5000.0,
        rated_cycle_life=6000,
        max_charge_power_kw=max_charge_kw,
        max_discharge_power_kw=max_discharge_kw,
        min_soc=0.10,
        max_soc=0.95,
        round_trip_efficiency=0.90,
        nominal_voltage_v=nominal_voltage_v,
    )


class _State:
    """Minimal HA state stub with .state and .attributes."""

    def __init__(self, value: str, unit: str | None = None) -> None:
        self.state = value
        self.attributes = {"unit_of_measurement": unit} if unit is not None else {}


class _Hass:
    """Hass stub allowing per-entity state lookup + AsyncMock service tracking."""

    def __init__(self, state_map: dict[str, _State] | None = None) -> None:
        self._states = state_map or {}
        self.services = MagicMock()
        self.services.async_call = AsyncMock()
        self.states = MagicMock()
        self.states.get = self._get

    def _get(self, entity_id: str):
        return self._states.get(entity_id)

    def set_state(self, entity_id: str, value: str, unit: str | None = None) -> None:
        self._states[entity_id] = _State(value, unit)


def make_controller(
    state_map: dict[str, _State] | None = None,
    battery_config: BatteryConfig | None = None,
    platform: InverterPlatform = InverterPlatform.SOLIS,
    entity_ids: dict[str, str] | None = None,
) -> tuple[InverterController, _Hass]:
    hass = _Hass(state_map)
    controller = InverterController(
        hass,
        platform,
        entity_ids or SOLIS_ENTITY_IDS,
        battery_config or make_battery_config(),
    )
    return controller, hass


def _calls(hass: _Hass) -> list[tuple[str, str, dict]]:
    """Return service call args as (domain, service, data) tuples."""
    return [(c.args[0], c.args[1], c.args[2]) for c in hass.services.async_call.call_args_list]


def _switches_toggled(hass: _Hass) -> dict[str, str]:
    """Return ``{entity_id: 'turn_on'|'turn_off'}`` for each switch call."""
    result: dict[str, str] = {}
    for dom, svc, data in _calls(hass):
        if dom == "switch":
            result[data["entity_id"]] = svc
    return result


def _numbers_written(hass: _Hass) -> dict[str, float]:
    """Return ``{entity_id: value}`` for each number.set_value call."""
    result: dict[str, float] = {}
    for dom, svc, data in _calls(hass):
        if dom == "number" and svc == "set_value":
            result[data["entity_id"]] = data["value"]
    return result


def _selects_written(hass: _Hass) -> dict[str, str]:
    """Return ``{entity_id: option}`` for each select.select_option call."""
    result: dict[str, str] = {}
    for dom, svc, data in _calls(hass):
        if dom == "select" and svc == "select_option":
            result[data["entity_id"]] = data["option"]
    return result


# ---------------------------------------------------------------------------
# apply_mode — Solis register-level state machine
# ---------------------------------------------------------------------------


def _specs():
    """Default spec table for the test fixtures."""
    return build_specs(make_battery_config(), export_max_w=10_000, inverter_max_power_w=10_000)


@pytest.mark.asyncio
async def test_apply_mode_grid_charge_starts_from_self_use_1():
    # Inverter currently at 43110=1 (SelfUse). Apply GridCharge (target 33 = SelfUse|GridCharge).
    # Only bit 5 (allow_grid_charge) needs to flip on.
    hass_state = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("1"),
    }
    controller, hass = make_controller(state_map=hass_state)
    await controller.apply_mode(StorageMode.GridCharge, _specs()[StorageMode.GridCharge])

    toggled = _switches_toggled(hass)
    assert toggled.get(SOLIS_ENTITY_IDS["allow_grid_charge_switch"]) == "turn_on"
    # bits 0 (self_use), 1 (tou), 6 (feed_in) all already match (current=1, target=33)
    assert SOLIS_ENTITY_IDS["self_use_switch"] not in toggled
    assert SOLIS_ENTITY_IDS["tou_mode_switch"] not in toggled
    assert SOLIS_ENTITY_IDS["feed_in_priority_switch"] not in toggled


@pytest.mark.asyncio
async def test_apply_mode_discharge_clears_self_use_and_sets_feed_in():
    # Current 43110=1 (SelfUse). Apply Discharge (target 64 = FeedIn) — bits 0 off, bit 6 on.
    hass_state = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("1"),
    }
    controller, hass = make_controller(state_map=hass_state)
    await controller.apply_mode(StorageMode.Discharge, _specs()[StorageMode.Discharge])

    toggled = _switches_toggled(hass)
    assert toggled.get(SOLIS_ENTITY_IDS["self_use_switch"]) == "turn_off"
    assert toggled.get(SOLIS_ENTITY_IDS["feed_in_priority_switch"]) == "turn_on"
    # bit 5 (grid charge) already off; bit 1 (TOU) already off
    assert SOLIS_ENTITY_IDS["allow_grid_charge_switch"] not in toggled
    assert SOLIS_ENTITY_IDS["tou_mode_switch"] not in toggled


@pytest.mark.asyncio
async def test_apply_mode_idempotent_when_readback_matches_target():
    # Inverter already in GridCharge state — bits, numbers, and the RC
    # selector / timeout all read back at their targets.
    spec = _specs()[StorageMode.GridCharge]
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State(str(spec.reg_43110_value)),
        SOLIS_ENTITY_IDS["battery_max_charge_current"]: _State(str(spec.charge_a)),
        SOLIS_ENTITY_IDS["battery_max_discharge_current"]: _State(str(spec.discharge_a)),
        SOLIS_ENTITY_IDS["backflow_power"]: _State(str(spec.export_limit_w)),
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State(str(spec.rc_setpoint_w)),
        SOLIS_ENTITY_IDS["rc_timeout"]: _State("30"),
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("Inverter AC Grid Port"),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.GridCharge, spec)
    # No service calls — everything already at the target value.
    assert _calls(hass) == []


@pytest.mark.asyncio
async def test_apply_mode_force_bypasses_cache_idempotency():
    """force=True must write every register even when cache already matches."""
    spec = _specs()[StorageMode.GridCharge]
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State(str(spec.reg_43110_value)),
        SOLIS_ENTITY_IDS["battery_max_charge_current"]: _State(str(spec.charge_a)),
        SOLIS_ENTITY_IDS["battery_max_discharge_current"]: _State(str(spec.discharge_a)),
        SOLIS_ENTITY_IDS["backflow_power"]: _State(str(spec.export_limit_w)),
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State(str(spec.rc_setpoint_w)),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.GridCharge, spec, force=True)
    # All four bit switches were force-toggled even though cache claimed they
    # already matched — covers the case where solis_modbus cache lags reality.
    toggled = _switches_toggled(hass)
    assert SOLIS_ENTITY_IDS["self_use_switch"] in toggled
    assert SOLIS_ENTITY_IDS["tou_mode_switch"] in toggled
    assert SOLIS_ENTITY_IDS["allow_grid_charge_switch"] in toggled
    assert SOLIS_ENTITY_IDS["feed_in_priority_switch"] in toggled
    # All four number entities were force-written.
    writes = _numbers_written(hass)
    assert SOLIS_ENTITY_IDS["battery_max_charge_current"] in writes
    assert SOLIS_ENTITY_IDS["battery_max_discharge_current"] in writes
    assert SOLIS_ENTITY_IDS["backflow_power"] in writes
    assert SOLIS_ENTITY_IDS["rc_setpoint"] in writes


@pytest.mark.asyncio
async def test_apply_mode_writes_numbers_only_when_outside_tolerance():
    spec = _specs()[StorageMode.SelfUse]
    # Readback says 43110 already at target. Currents differ; export limit matches.
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State(str(spec.reg_43110_value)),
        SOLIS_ENTITY_IDS["battery_max_charge_current"]: _State("0"),       # differs from target
        SOLIS_ENTITY_IDS["battery_max_discharge_current"]: _State(str(spec.discharge_a)),
        SOLIS_ENTITY_IDS["backflow_power"]: _State(str(spec.export_limit_w)),
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State(str(spec.rc_setpoint_w)),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.SelfUse, spec)

    writes = _numbers_written(hass)
    # Only the charge current should be rewritten — the rest were within tolerance.
    assert SOLIS_ENTITY_IDS["battery_max_charge_current"] in writes
    assert SOLIS_ENTITY_IDS["battery_max_discharge_current"] not in writes
    assert SOLIS_ENTITY_IDS["backflow_power"] not in writes
    assert SOLIS_ENTITY_IDS["rc_setpoint"] not in writes


@pytest.mark.asyncio
async def test_apply_mode_skips_export_limit_when_spec_is_none():
    # TRACK leaves the export limit at the hardware default → export_limit_w is None.
    spec = _specs()[StorageMode.TRACK]
    assert spec.export_limit_w is None
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("1"),
        SOLIS_ENTITY_IDS["backflow_power"]: _State("12345"),  # arbitrary
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State("0"),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.TRACK, spec)

    writes = _numbers_written(hass)
    assert SOLIS_ENTITY_IDS["backflow_power"] not in writes


@pytest.mark.asyncio
async def test_apply_mode_no_op_on_non_solis_platforms():
    controller, hass = make_controller(
        platform=InverterPlatform.HUAWEI_SOLAR,
        entity_ids={"battery_soc": "sensor.x"},
    )
    await controller.apply_mode(StorageMode.GridCharge, _specs()[StorageMode.GridCharge])
    assert _calls(hass) == []


@pytest.mark.asyncio
async def test_apply_mode_skips_role_with_empty_entity_id():
    # Remove the allow_grid_charge_switch role entirely.
    entity_ids = {k: v for k, v in SOLIS_ENTITY_IDS.items() if k != "allow_grid_charge_switch"}
    state_map = {
        entity_ids["storage_control_readback"]: _State("1"),
    }
    controller, hass = make_controller(state_map=state_map, entity_ids=entity_ids)
    # GridCharge needs bit 5 → on. With the switch missing, the call should be silently skipped.
    await controller.apply_mode(StorageMode.GridCharge, _specs()[StorageMode.GridCharge])
    toggled = _switches_toggled(hass)
    assert "switch.solis_allow_grid_to_charge_the_battery" not in toggled


# ---------------------------------------------------------------------------
# RC function engagement (43132 selector / 43282 timeout / 43128 setpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_mode_rc_mode_engages_selector_then_timeout_then_setpoint():
    # GridCharge from a cold RC state: the selector must be engaged before
    # the timeout, and the timeout before the setpoint (solis_modbus#352 —
    # a timeout written first does not latch).
    spec = _specs()[StorageMode.GridCharge]
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("33"),
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("OFF"),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.GridCharge, spec)

    selects = _selects_written(hass)
    writes = _numbers_written(hass)
    assert selects[SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]] == "Inverter AC Grid Port"
    assert writes[SOLIS_ENTITY_IDS["rc_timeout"]] == 30.0
    assert writes[SOLIS_ENTITY_IDS["rc_setpoint"]] == float(spec.rc_setpoint_w)
    ordered = [
        data["entity_id"] for _, _, data in _calls(hass)
        if data["entity_id"] in (
            SOLIS_ENTITY_IDS["rc_grid_adjustment_select"],
            SOLIS_ENTITY_IDS["rc_timeout"],
            SOLIS_ENTITY_IDS["rc_setpoint"],
        )
    ]
    assert ordered == [
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"],
        SOLIS_ENTITY_IDS["rc_timeout"],
        SOLIS_ENTITY_IDS["rc_setpoint"],
    ]


@pytest.mark.asyncio
async def test_apply_mode_non_rc_mode_zeros_setpoint_then_releases_selector():
    # Leaving an RC-backed mode: the setpoint is zeroed while the function is
    # still engaged, then the selector is released to OFF.
    spec = _specs()[StorageMode.SelfUse]
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("1"),
        SOLIS_ENTITY_IDS["battery_max_charge_current"]: _State(str(spec.charge_a)),
        SOLIS_ENTITY_IDS["battery_max_discharge_current"]: _State(str(spec.discharge_a)),
        SOLIS_ENTITY_IDS["backflow_power"]: _State(str(spec.export_limit_w)),
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State("-5000"),  # stale GridCharge setpoint
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("Inverter AC Grid Port"),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.SelfUse, spec)

    writes = _numbers_written(hass)
    selects = _selects_written(hass)
    assert writes[SOLIS_ENTITY_IDS["rc_setpoint"]] == 0.0
    assert selects[SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]] == "OFF"
    ordered = [
        data["entity_id"] for _, _, data in _calls(hass)
        if data["entity_id"] in (
            SOLIS_ENTITY_IDS["rc_grid_adjustment_select"],
            SOLIS_ENTITY_IDS["rc_setpoint"],
        )
    ]
    assert ordered == [
        SOLIS_ENTITY_IDS["rc_setpoint"],
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"],
    ]


@pytest.mark.asyncio
async def test_apply_mode_selector_skipped_when_already_engaged():
    spec = _specs()[StorageMode.GridCharge]
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("33"),
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("Inverter AC Grid Port"),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.apply_mode(StorageMode.GridCharge, spec)
    assert SOLIS_ENTITY_IDS["rc_grid_adjustment_select"] not in _selects_written(hass)


@pytest.mark.asyncio
async def test_refresh_rc_reengages_selector_even_when_readback_matches():
    # The keep-alive must re-issue the *selector* every time, not just the
    # timeout/setpoint: re-issuing 43132 is what re-arms the inverter-side RC
    # function. Skipping it when the cached readback still reads "engaged" is
    # what let the function expire underneath us (prod capture 2026-06-15) —
    # a setpoint-only rewrite does not restart an already-expired function.
    spec = _specs()[StorageMode.Discharge]
    state_map = {
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("Inverter AC Grid Port"),
        SOLIS_ENTITY_IDS["rc_timeout"]: _State("30"),
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State(str(spec.rc_setpoint_w)),
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.refresh_rc(spec)
    writes = _numbers_written(hass)
    assert writes[SOLIS_ENTITY_IDS["rc_timeout"]] == 30.0
    assert writes[SOLIS_ENTITY_IDS["rc_setpoint"]] == float(spec.rc_setpoint_w)
    # The selector is re-issued even though the readback already reads engaged.
    assert (
        _selects_written(hass)[SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]]
        == "Inverter AC Grid Port"
    )


@pytest.mark.asyncio
async def test_refresh_rc_reengages_dropped_selector():
    spec = _specs()[StorageMode.GridCharge]
    state_map = {
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("OFF"),  # expired
    }
    controller, hass = make_controller(state_map=state_map)
    await controller.refresh_rc(spec)
    selects = _selects_written(hass)
    assert selects[SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]] == "Inverter AC Grid Port"


@pytest.mark.asyncio
async def test_refresh_rc_no_op_for_non_rc_modes():
    controller, hass = make_controller()
    await controller.refresh_rc(_specs()[StorageMode.SelfUse])
    assert _calls(hass) == []


def test_get_rc_adjustment_value_maps_option_labels():
    state_map = {
        SOLIS_ENTITY_IDS["rc_grid_adjustment_select"]: _State("Inverter AC Grid Port"),
    }
    controller, hass = make_controller(state_map=state_map)
    assert controller.get_rc_adjustment_value() == 2
    hass.set_state(SOLIS_ENTITY_IDS["rc_grid_adjustment_select"], "OFF")
    assert controller.get_rc_adjustment_value() == 0
    hass.set_state(SOLIS_ENTITY_IDS["rc_grid_adjustment_select"], "unavailable")
    assert controller.get_rc_adjustment_value() is None


# ---------------------------------------------------------------------------
# Telemetry / readback helpers used by the InverterModeTranslator
# ---------------------------------------------------------------------------


def test_get_storage_control_word_returns_int():
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("33"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_storage_control_word() == 33


def test_get_storage_control_word_none_when_absent():
    controller, _ = make_controller()
    assert controller.get_storage_control_word() is None


def test_get_storage_control_word_none_when_unavailable():
    state_map = {
        SOLIS_ENTITY_IDS["storage_control_readback"]: _State("unavailable"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_storage_control_word() is None


def test_get_charge_current_returns_float():
    state_map = {
        SOLIS_ENTITY_IDS["battery_max_charge_current"]: _State("50.5"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_charge_current_a() == pytest.approx(50.5)


def test_get_rc_setpoint_returns_int():
    state_map = {
        SOLIS_ENTITY_IDS["rc_setpoint"]: _State("-3000"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_rc_setpoint_w() == -3000


def test_get_battery_soc_normalises_percent():
    state_map = {SOLIS_ENTITY_IDS["battery_soc"]: _State("62.0")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_soc() == pytest.approx(0.62)


def test_get_battery_soc_passes_through_fraction():
    state_map = {SOLIS_ENTITY_IDS["battery_soc"]: _State("0.62")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_soc() == pytest.approx(0.62)


def test_get_battery_soc_percent_unit_divides_exact_one_percent():
    # A "%" sensor reading exactly 1 is 1 %, not the fraction 1.0 = 100 %.
    # The unit pins it down where the magnitude heuristic alone could not.
    state_map = {SOLIS_ENTITY_IDS["battery_soc"]: _State("1", "%")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_soc() == pytest.approx(0.01)


def test_get_battery_soc_percent_unit_divides_full_charge():
    state_map = {SOLIS_ENTITY_IDS["battery_soc"]: _State("100", "%")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_soc() == pytest.approx(1.0)


def test_get_battery_soc_none_when_unavailable():
    # No fabricated 0.5 fallback: SoC has no safe default, so an absent sensor
    # yields None (which drops the whole BatteryReading downstream).
    controller, _ = make_controller()
    assert controller.get_battery_soc() is None


def test_get_battery_soc_none_when_state_unavailable():
    state_map = {SOLIS_ENTITY_IDS["battery_soc"]: _State("unavailable")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_soc() is None


def test_get_battery_charge_energy_today_kwh():
    state_map = {
        SOLIS_ENTITY_IDS["battery_charge_energy_today"]: _State("24.8", "kWh"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_charge_energy_today() == pytest.approx(24.8)


def test_get_battery_discharge_energy_today_kwh():
    state_map = {
        SOLIS_ENTITY_IDS["battery_discharge_energy_today"]: _State("19.8", "kWh"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_discharge_energy_today() == pytest.approx(19.8)


def test_get_battery_charge_energy_today_normalises_wh():
    # A Wh-unit counter is rescaled to kWh.
    state_map = {
        SOLIS_ENTITY_IDS["battery_charge_energy_today"]: _State("24800", "Wh"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_charge_energy_today() == pytest.approx(24.8)


def test_get_battery_energy_today_none_when_unmapped():
    # No entity mapped → None, so the estimator can tell "no counter" from "0".
    controller, _ = make_controller()
    assert controller.get_battery_charge_energy_today() is None
    assert controller.get_battery_discharge_energy_today() is None


# ---------------------------------------------------------------------------
# Power-unit normalisation (unchanged)
# ---------------------------------------------------------------------------


def test_normalize_power_to_kw_handles_known_units():
    assert normalize_power_to_kw(3920.0, "W") == pytest.approx(3.92)
    assert normalize_power_to_kw(3.92, "kW") == pytest.approx(3.92)
    assert normalize_power_to_kw(0.0039, "MW") == pytest.approx(3.9)
    assert normalize_power_to_kw(3920.0, "") == pytest.approx(3920.0)
    assert normalize_power_to_kw(3920.0, "garbage") == pytest.approx(3920.0)


def test_get_grid_power_reads_grid_power_net_without_sign_flip():
    # The derived ``grid_power_net`` sensor already follows sunSale's
    # positive=import convention — controller passes the value through.
    state_map = {SOLIS_ENTITY_IDS["grid_power"]: _State("3920", "W")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_grid_power() == pytest.approx(3.92)


def test_get_battery_power_prefers_signed_net_and_flips_charging():
    # solis_modbus's ``battery_power_net`` is discharge-positive (negative while
    # charging); the controller flips it to sunSale's positive=charging.
    state_map = {SOLIS_ENTITY_IDS["battery_power_signed"]: _State("-2304", "W")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_power() == pytest.approx(2.304)


def test_get_battery_power_signed_net_discharge_is_negative():
    # Discharging: net sensor reads positive → sunSale convention negative.
    state_map = {SOLIS_ENTITY_IDS["battery_power_signed"]: _State("1500", "W")}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_power() == pytest.approx(-1.5)


def test_get_battery_power_falls_back_to_magnitude_when_signed_unavailable():
    # Without the signed net sensor the controller reads the ``battery_power``
    # role as-is (magnitude on a real Solis; the test value is arbitrary).
    state_map = {
        SOLIS_ENTITY_IDS["battery_power_signed"]: _State("unavailable", "W"),
        SOLIS_ENTITY_IDS["battery_power"]: _State("-1.579", "kW"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_battery_power() == pytest.approx(-1.579)


def test_get_grid_power_falls_back_to_port_with_sign_flip_when_net_unavailable():
    # Primary net sensor missing → controller reads the AC-port fallback and
    # flips its sign to match sunSale convention (positive=import).
    state_map = {
        SOLIS_ENTITY_IDS["grid_power"]: _State("unavailable", "W"),
        SOLIS_ENTITY_IDS["grid_power_fallback"]: _State("3920", "W"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_grid_power() == pytest.approx(-3.92)


def test_get_grid_power_returns_zero_when_both_primary_and_fallback_unavailable():
    state_map = {
        SOLIS_ENTITY_IDS["grid_power"]: _State("unavailable", "W"),
        SOLIS_ENTITY_IDS["grid_power_fallback"]: _State("unavailable", "W"),
    }
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_grid_power() == 0.0


def test_get_grid_power_assumes_kw_when_unit_missing():
    state_map = {SOLIS_ENTITY_IDS["grid_power"]: _State("3.92", None)}
    controller, _ = make_controller(state_map=state_map)
    assert controller.get_grid_power() == pytest.approx(3.92)
