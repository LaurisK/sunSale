"""Tests for the capability seam — see ``docs/capability_seam.md``.

The seam exists because sunSale used to model only *what I want* (the composed
``StorageModeSpec``) and *what I see* (the readback), with no representation of
*what the hardware will accept*. That gap produced four failures that all trace
to the same missing abstraction. The regressions pinned here are, concretely:

  * ``apply_mode`` wrote a target clamped into the entity's advertised range
    while ``control_surface`` compared the readback against the *unclamped*
    composition, so a spec above an upstream bound could never verify. Observed
    live on 2026-08-03: ``mismatched=[('charge_a', 292.96875, 200.0)]``, wedged
    since solis_modbus v4.2.0 began honouring the 0–200 A ceiling on 43117/43118.
  * An *unreadable* control point was reported as ``match=False``, i.e. as drift,
    which pinned ``_registers_drifted()`` True for as long as an entity stayed
    unavailable and starved the stale-verdict self-heal.
  * The DP planned against configured caps that the dispatcher then clamped, so
    planned and achievable export silently diverged.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import (
    UNKNOWN_LIMIT,
    BatteryConfig,
    InverterCapability,
    Limit,
    StorageMode,
)
from custom_components.sun_sale.outbound.entity_control import EntityActuator
from custom_components.sun_sale.outbound.solis_driver import SolisDriver

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

# The reference install's geometry, so the numbers in these tests are the ones
# actually observed on hardware rather than the shared 48 V / 5 kW fixture.
_BUS_V = 51.2
_BATTERY_KW = 15.0
# 15 kW at 51.2 V — the composed battery-current target.
_COMPOSED_A = 292.96875
# 200 A at 51.2 V — what the advertised ceiling projects back to.
_CEILING_KW = 10.24


def _battery_config() -> BatteryConfig:
    """Return the reference install's battery config (51.2 V bus, 15 kW legs)."""
    return BatteryConfig(
        nominal_capacity_kwh=42.0,
        purchase_price_eur=5000.0,
        rated_cycle_life=6000,
        max_charge_power_kw=_BATTERY_KW,
        max_discharge_power_kw=_BATTERY_KW,
        min_soc=0.10,
        max_soc=0.95,
        round_trip_efficiency=0.90,
        nominal_voltage_v=_BUS_V,
    )


class _State:
    """Minimal HA state stub carrying arbitrary attributes."""

    def __init__(self, value: str, **attributes: object) -> None:
        self.state = value
        self.attributes: dict = dict(attributes)


class _Hass:
    """Hass stub with per-entity state lookup."""

    def __init__(self, states: dict[str, _State] | None = None) -> None:
        self._states: dict[str, _State] = states or {}
        self.services = MagicMock()
        self.services.async_call = AsyncMock()
        self.states = MagicMock()
        self.states.get = self._states.get


_ROLES = {
    "battery_max_charge_current": "number.solis_battery_max_charge_current",
    "battery_max_discharge_current": "number.solis_battery_max_discharge_current",
    "backflow_power": "number.solis_backflow_power",
    "rc_setpoint": "number.solis_rc_active_power",
}


def _actuator(states: dict[str, _State] | None = None) -> EntityActuator:
    """Return an actuator over the stub hass."""
    return EntityActuator(_Hass(states), _ROLES)


def _driver(limits: dict[str, Limit] | None = None) -> tuple[SolisDriver, MagicMock]:
    """Return a SolisDriver whose inverter advertises ``limits`` per role."""
    inv = MagicMock()
    inv.apply_mode = AsyncMock()
    inv.refresh_rc = AsyncMock()
    resolved = limits or {}
    inv.limit_for = MagicMock(side_effect=lambda role: resolved.get(role, UNKNOWN_LIMIT))
    # Readbacks default to None (unreadable) unless a test wires them.
    for getter in (
        "get_storage_control_word", "get_charge_current_a", "get_discharge_current_a",
        "get_backflow_power_w", "get_rc_setpoint_w", "get_rc_adjustment_value",
    ):
        setattr(inv, getter, MagicMock(return_value=None))
    return SolisDriver(inv, _battery_config(), 10_000, 10_000), inv


# --- Limit ------------------------------------------------------------------ #


def test_limit_clamp_and_constrains() -> None:
    assert Limit(0.0, 200.0, True).clamp(292.96875) == 200.0
    assert Limit(0.0, 200.0, True).clamp(150.0) == 150.0
    assert Limit(-10_000.0, 10_000.0, True).clamp(15_000.0) == 10_000.0
    assert Limit(-10_000.0, 10_000.0, True).clamp(-15_000.0) == -10_000.0
    # One-sided bounds only constrain their own side.
    assert Limit(None, 200.0, True).clamp(-5.0) == -5.0
    assert Limit(0.0, None, True).clamp(1e9) == 1e9
    assert Limit(0.0, 200.0, True).constrains is True
    assert Limit(None, None, True).constrains is False


def test_unknown_limit_never_clamps() -> None:
    """An unresolvable bound must pass the target through untouched.

    Silently reducing a target because we *could not read* a bound would be the
    same category of error as the seam fixes — inventing a fact from an absence.
    """
    assert UNKNOWN_LIMIT.known is False
    assert UNKNOWN_LIMIT.clamp(292.96875) == 292.96875
    assert UNKNOWN_LIMIT.constrains is False


# --- EntityActuator.limit_for ---------------------------------------------- #


def test_limit_for_reads_advertised_bounds() -> None:
    act = _actuator({
        _ROLES["battery_max_charge_current"]: _State("0.0", min=0, max=200),
    })
    limit = act.limit_for("battery_max_charge_current")
    assert (limit.low, limit.high, limit.known) == (0.0, 200.0, True)


def test_limit_for_unmapped_or_absent_role_is_unknown() -> None:
    act = _actuator()  # role maps to an entity that isn't in the state machine
    assert act.limit_for("battery_max_charge_current") == UNKNOWN_LIMIT
    assert act.limit_for("no_such_role") == UNKNOWN_LIMIT


def test_limit_for_tolerates_unparseable_and_missing_bounds() -> None:
    act = _actuator({
        _ROLES["battery_max_charge_current"]: _State("0.0", min=0, max="not-a-number"),
        _ROLES["rc_setpoint"]: _State("0", min=None, max=None),
    })
    assert act.limit_for("battery_max_charge_current") == UNKNOWN_LIMIT
    # Present entity advertising no bounds is *known* but unconstraining — it is
    # a real answer ("no ceiling declared"), unlike an absent entity.
    rc = act.limit_for("rc_setpoint")
    assert rc.known is True
    assert rc.constrains is False


def test_limit_for_is_resolved_live_not_cached() -> None:
    """A bound that moves between calls must be re-read.

    solis_modbus v4.2.2 resolves these from BMS mirror registers via a live
    property, so caching would pin sunSale to a stale ceiling.
    """
    state = _State("0.0", min=0, max=200)
    act = _actuator({_ROLES["battery_max_charge_current"]: state})
    assert act.limit_for("battery_max_charge_current").high == 200.0
    state.attributes["max"] = 112.0  # BMS mirror arrives, reports a lower limit
    assert act.limit_for("battery_max_charge_current").high == 112.0


# --- effective_spec -------------------------------------------------------- #


def test_effective_spec_reduces_currents_to_advertised_ceiling() -> None:
    driver, _ = _driver({
        "battery_max_charge_current": Limit(0.0, 200.0, True),
        "battery_max_discharge_current": Limit(0.0, 200.0, True),
    })
    declared = driver.declared_spec(StorageMode.SelfUse)
    effective = driver.effective_spec(StorageMode.SelfUse)
    assert declared.charge_a == pytest.approx(_COMPOSED_A)
    assert effective.charge_a == 200.0
    assert effective.discharge_a == 200.0
    # Untouched fields survive the reduction.
    assert effective.reg_43110_value == declared.reg_43110_value


def test_effective_spec_reduces_rc_setpoint() -> None:
    """The RC leg is bounded by a declared ±10 kW literal upstream."""
    driver, _ = _driver({"rc_setpoint": Limit(-10_000.0, 10_000.0, True)})
    driver_15kw, _ = _driver({"rc_setpoint": Limit(-10_000.0, 10_000.0, True)})
    driver_15kw._specs[StorageMode.Discharge] = type(
        driver_15kw._specs[StorageMode.Discharge],
    )(
        reg_43110_value=64, export_limit_w=10_000, charge_a=0.0,
        discharge_a=200.0, rc_setpoint_w=15_000,
    )
    assert driver.effective_spec(StorageMode.Discharge).rc_setpoint_w == 10_000
    assert driver_15kw.effective_spec(StorageMode.Discharge).rc_setpoint_w == 10_000


def test_effective_spec_is_identity_without_bounds() -> None:
    """No advertised bounds ⇒ the composed spec passes through unchanged."""
    driver, _ = _driver()
    for mode in (StorageMode.SelfUse, StorageMode.Discharge, StorageMode.GridCharge):
        assert driver.effective_spec(mode) == driver.declared_spec(mode)


def test_spec_for_returns_effective_not_declared() -> None:
    """The control module gets the reduced spec — that is the contract."""
    driver, _ = _driver({"battery_max_charge_current": Limit(0.0, 200.0, True)})
    assert driver.spec_for(StorageMode.SelfUse).charge_a == 200.0
    assert driver.declared_spec(StorageMode.SelfUse).charge_a == pytest.approx(_COMPOSED_A)


def test_effective_spec_unsupported_mode_is_none() -> None:
    driver, _ = _driver()
    assert driver.effective_spec(StorageMode.UNKNOWN) is None
    assert driver.declared_spec(StorageMode.UNKNOWN) is None


# --- control_surface: the regression that wedged verify --------------------- #


def test_control_surface_matches_when_readback_equals_clamped_target() -> None:
    """The core regression: a clamped write must verify against the clamped value.

    Composed 292.97 A, entity ceiling 200 A, inverter reports 200 A. Before the
    seam this row compared 200.0 against 292.96875 and reported ``mismatch``
    forever, which is exactly what parked the live control loop.
    """
    driver, inv = _driver({
        "battery_max_charge_current": Limit(0.0, 200.0, True),
        "battery_max_discharge_current": Limit(0.0, 200.0, True),
    })
    inv.get_storage_control_word = MagicMock(return_value=1)
    inv.get_charge_current_a = MagicMock(return_value=200.0)
    inv.get_discharge_current_a = MagicMock(return_value=200.0)
    inv.get_backflow_power_w = MagicMock(return_value=10_000)
    inv.get_rc_setpoint_w = MagicMock(return_value=0)

    rows = {r.name: r for r in driver.control_surface(StorageMode.SelfUse)}
    assert rows["charge_a"].status == "match"
    assert rows["charge_a"].desired == 200.0
    assert rows["discharge_a"].status == "match"


def test_control_surface_unreadable_row_is_unknown_not_mismatch() -> None:
    driver, inv = _driver()
    inv.get_storage_control_word = MagicMock(return_value=1)
    inv.get_backflow_power_w = MagicMock(return_value=0)
    inv.get_rc_setpoint_w = MagicMock(return_value=0)
    # charge/discharge readbacks stay None — the live 2026-08-03 outage.
    rows = {r.name: r for r in driver.control_surface(StorageMode.StandBy)}
    assert rows["charge_a"].status == "unknown"
    assert rows["charge_a"].match is False  # legacy flag stays falsy
    assert rows["charge_a"].readable is False
    assert rows["reg_43110"].status == "match"


def test_control_surface_genuine_disagreement_is_mismatch() -> None:
    driver, inv = _driver()
    inv.get_storage_control_word = MagicMock(return_value=64)  # wanted 1
    inv.get_charge_current_a = MagicMock(return_value=_COMPOSED_A)
    inv.get_discharge_current_a = MagicMock(return_value=_COMPOSED_A)
    inv.get_backflow_power_w = MagicMock(return_value=10_000)
    inv.get_rc_setpoint_w = MagicMock(return_value=0)
    rows = {r.name: r for r in driver.control_surface(StorageMode.SelfUse)}
    assert rows["reg_43110"].status == "mismatch"
    assert rows["reg_43110"].match is False


def test_control_row_no_target_keeps_none_match() -> None:
    """A no-target row must stay ``match=None`` so the panel renders it neutral."""
    driver, inv = _driver()
    inv.get_storage_control_word = MagicMock(return_value=1)
    rows = {r.name: r for r in driver.control_surface(StorageMode.TRACK)}
    assert rows["export_limit_w"].status == "no_target"
    assert rows["export_limit_w"].match is None


def test_control_row_as_dict_carries_both_status_and_legacy_match() -> None:
    driver, inv = _driver()
    inv.get_storage_control_word = MagicMock(return_value=1)
    row = next(r for r in driver.control_surface(StorageMode.StandBy)
               if r.name == "reg_43110")
    d = row.as_dict()
    assert d["status"] == "match"
    assert d["match"] is True
    assert set(d) == {"name", "label", "desired", "observed", "match", "status"}


# --- capability ------------------------------------------------------------- #


def test_capability_projects_amps_to_kw_at_bus_voltage() -> None:
    driver, _ = _driver({
        "battery_max_charge_current": Limit(0.0, 200.0, True),
        "battery_max_discharge_current": Limit(0.0, 200.0, True),
    })
    cap = driver.capability(NOW)
    # 200 A x 51.2 V = 10.24 kW
    assert cap.max_battery_charge_kw == pytest.approx(_CEILING_KW)
    assert cap.max_battery_discharge_kw == pytest.approx(_CEILING_KW)
    assert cap.resolved_at == NOW


def test_capability_grid_discharge_takes_the_binding_leg() -> None:
    """Three legs bound discharge-to-grid; the smallest wins.

    The reference install's numbers: battery 200 A x 51.2 V = 10.24 kW, RC
    ±10 kW, export 20 kW. RC binds at 10.0 — which is why lifting only the
    200 A current ceiling would have changed nothing.
    """
    driver, _ = _driver({
        "battery_max_discharge_current": Limit(0.0, 200.0, True),
        "rc_setpoint": Limit(-10_000.0, 10_000.0, True),
        "backflow_power": Limit(0.0, 20_000.0, True),
    })
    cap = driver.capability(NOW)
    assert cap.max_battery_discharge_kw == pytest.approx(_CEILING_KW)
    assert cap.max_export_kw == pytest.approx(20.0)
    assert cap.max_grid_discharge_kw == pytest.approx(10.0)


def test_capability_reports_degraded_roles() -> None:
    driver, _ = _driver({"backflow_power": Limit(0.0, 20_000.0, True)})
    cap = driver.capability(NOW)
    assert set(cap.degraded) == {
        "battery_max_charge_current", "battery_max_discharge_current", "rc_setpoint",
    }
    assert cap.max_export_kw == pytest.approx(20.0)
    # Unresolved legs are None — "unknown", not "unlimited" and not "zero".
    assert cap.max_battery_charge_kw is None
    assert cap.max_grid_discharge_kw == pytest.approx(20.0)


def test_capability_all_unknown_is_all_none() -> None:
    driver, _ = _driver()
    cap = driver.capability(NOW)
    assert cap.max_battery_charge_kw is None
    assert cap.max_battery_discharge_kw is None
    assert cap.max_export_kw is None
    assert cap.max_grid_discharge_kw is None
    assert len(cap.degraded) == 4


# --- InverterCapability.reduce --------------------------------------------- #


def test_reduce_only_lowers_never_raises() -> None:
    assert InverterCapability.reduce(15.0, 10.0) == 10.0
    assert InverterCapability.reduce(5.0, 10.0) == 5.0
    # A configured cap of 30 kW cannot buy capability the hardware lacks.
    assert InverterCapability.reduce(30.0, 10.0) == 10.0


def test_reduce_passes_through_when_either_side_is_none() -> None:
    assert InverterCapability.reduce(None, 10.0) == 10.0
    assert InverterCapability.reduce(10.0, None) == 10.0
    assert InverterCapability.reduce(None, None) is None


# --- Coordinator reduction -------------------------------------------------- #
#
# The planner-facing half: the DP must receive min(configured, capable) per leg,
# recomputed each cycle. Exercised through the real ``_read_schedule_knobs`` with
# a stub driver, since that method is where the reduction lives.


class _KnobCoordinator:
    """Minimal stand-in exposing just what ``_read_schedule_knobs`` reads."""

    use_standby = True
    allow_grid_charging = True
    allow_feed_in = True
    allow_discharge_to_grid = True
    mode_change_penalty_eur_per_kwh = 0.005
    profitability_tilt_alpha = 0.5
    terminal_value_discount = 0.5

    def __init__(self, driver, max_discharge_to_grid_kw, export_limit_w) -> None:
        self._inverter_driver = driver
        self.max_discharge_to_grid_kw = max_discharge_to_grid_kw
        self._export_limit_w = export_limit_w

    # Bind the real implementations under test.
    from custom_components.sun_sale.orchestration.coordinator import (  # noqa: PLC0415
        SunSaleCoordinator as _C,
    )
    _read_schedule_knobs = _C._read_schedule_knobs
    _current_capability = _C._current_capability


def _knobs(limits, max_discharge_to_grid_kw=15.0, export_limit_w=20_000):
    """Return the ScheduleKnobs the DP would receive for ``limits``."""
    driver, _ = _driver(limits)
    return _KnobCoordinator(driver, max_discharge_to_grid_kw, export_limit_w
                            )._read_schedule_knobs()


def test_planner_caps_reduced_to_binding_capability() -> None:
    """The DP must not be handed 15 kW when the RC leg caps export at 10 kW."""
    knobs = _knobs({
        "battery_max_discharge_current": Limit(0.0, 200.0, True),
        "rc_setpoint": Limit(-10_000.0, 10_000.0, True),
        "backflow_power": Limit(0.0, 20_000.0, True),
    })
    assert knobs.max_discharge_to_grid_kw == pytest.approx(10.0)
    assert knobs.export_limit_kw == pytest.approx(20.0)


def test_planner_caps_untouched_when_capability_unresolved() -> None:
    """No advertised bounds ⇒ pre-seam behaviour (configured caps reach the DP)."""
    knobs = _knobs({})
    assert knobs.max_discharge_to_grid_kw == pytest.approx(15.0)
    assert knobs.export_limit_kw == pytest.approx(20.0)


def test_planner_cap_below_capability_is_respected() -> None:
    """A deliberately conservative operator cap must not be raised to capability."""
    knobs = _knobs(
        {"rc_setpoint": Limit(-10_000.0, 10_000.0, True)},
        max_discharge_to_grid_kw=3.0,
    )
    assert knobs.max_discharge_to_grid_kw == pytest.approx(3.0)


def test_planner_caps_survive_a_raising_driver() -> None:
    """A driver that raises must leave the DP on configured caps, not crash."""
    broken = MagicMock()
    broken.capability = MagicMock(side_effect=RuntimeError("modbus down"))
    knobs = _KnobCoordinator(broken, 15.0, 20_000)._read_schedule_knobs()
    assert knobs.max_discharge_to_grid_kw == pytest.approx(15.0)
    assert knobs.export_limit_kw == pytest.approx(20.0)


def test_planner_caps_track_a_moving_bound() -> None:
    """Re-resolved per cycle: a BMS mirror arriving mid-run must reach the DP."""
    state = _State("0.0", min=0, max=20_000)
    hass = _Hass({_ROLES["rc_setpoint"]: state})
    inv = MagicMock()
    inv.limit_for = EntityActuator(hass, _ROLES).limit_for
    driver = SolisDriver(inv, _battery_config(), 10_000, 10_000)
    coord = _KnobCoordinator(driver, 15.0, 20_000)

    assert coord._read_schedule_knobs().max_discharge_to_grid_kw == pytest.approx(15.0)
    state.attributes["max"] = 10_000  # upstream tightens the RC ceiling
    assert coord._read_schedule_knobs().max_discharge_to_grid_kw == pytest.approx(10.0)


# --- Protocol conformance --------------------------------------------------- #


def test_both_drivers_satisfy_the_extended_control_driver_protocol() -> None:
    """Every driver must implement the seam's additions, not just Solis.

    ``spec_for`` returning an *effective* spec is part of the
    ``InverterControlDriver`` contract, so a driver that forgot the reduction
    would silently reintroduce the verify wedge on its platform.
    """
    from custom_components.sun_sale.outbound.entity_control import EntityControlDriver

    required = (
        "spec_for", "declared_spec", "effective_spec", "capability",
        "needs_keepalive", "control_surface", "decode_observed", "observe",
        "observed_raw_state", "apply_mode", "refresh_rc", "get_grid_power",
    )
    for cls in (SolisDriver, EntityControlDriver):
        missing = [m for m in required if not hasattr(cls, m)]
        assert not missing, f"{cls.__name__} missing {missing}"


def test_entity_driver_capability_is_unresolved_not_zero() -> None:
    """Entity platforms report ``None`` per leg, which must leave caps untouched.

    Reporting 0.0 would silently forbid all transfer on six platforms; reporting
    ``None`` keeps them on their configured caps exactly as before the seam.
    """
    from custom_components.sun_sale.outbound.entity_control import EntityControlDriver

    cap = EntityControlDriver.capability(MagicMock(), NOW)
    assert cap.max_battery_charge_kw is None
    assert cap.max_battery_discharge_kw is None
    assert cap.max_export_kw is None
    assert cap.max_grid_discharge_kw is None
    assert InverterCapability.reduce(15.0, cap.max_grid_discharge_kw) == 15.0


# --- Battery envelope reaches the DP ---------------------------------------- #


def test_capped_battery_reduces_slot_energy_envelope() -> None:
    """The DP's per-slot battery envelope must follow capability, not config.

    ``slot_physics`` sizes each slot from ``battery_cfg.max_charge_power_kw``, so
    leaving it at the configured 15 kW while the hardware accepts 10.24 kW lets
    the DP budget 3.75 kWh of charge in a 15-min slot against an achievable
    2.56 kWh. This was the charge-side half of the over-planning bug.
    """
    from custom_components.sun_sale.contract.models import SchedulePolicy
    from custom_components.sun_sale.pipeline.nodes.tier4 import _capped_battery

    cfg = _battery_config()
    assert cfg.max_charge_power_kw == 15.0

    capped = _capped_battery(cfg, SchedulePolicy(
        max_battery_charge_kw=_CEILING_KW, max_battery_discharge_kw=_CEILING_KW,
    ))
    assert capped.max_charge_power_kw == pytest.approx(_CEILING_KW)
    assert capped.max_discharge_power_kw == pytest.approx(_CEILING_KW)
    # Everything else is preserved.
    assert capped.nominal_voltage_v == cfg.nominal_voltage_v
    assert capped.round_trip_efficiency == cfg.round_trip_efficiency


def test_capped_battery_is_identity_without_capability() -> None:
    """A bare policy leaves BatteryConfig untouched (pre-seam behaviour)."""
    from custom_components.sun_sale.contract.models import SchedulePolicy
    from custom_components.sun_sale.pipeline.nodes.tier4 import _capped_battery

    cfg = _battery_config()
    assert _capped_battery(cfg, SchedulePolicy()) is cfg


def test_capped_battery_never_raises_a_lower_configured_limit() -> None:
    """Capability may only reduce: a 5 kW config stays 5 kW against a 10 kW ceiling."""
    from custom_components.sun_sale.contract.models import SchedulePolicy
    from custom_components.sun_sale.pipeline.nodes.tier4 import _capped_battery

    cfg = replace(_battery_config(), max_charge_power_kw=5.0, max_discharge_power_kw=5.0)
    capped = _capped_battery(cfg, SchedulePolicy(
        max_battery_charge_kw=_CEILING_KW, max_battery_discharge_kw=_CEILING_KW,
    ))
    assert capped.max_charge_power_kw == 5.0
    assert capped.max_discharge_power_kw == 5.0
