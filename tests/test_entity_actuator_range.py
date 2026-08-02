"""Tests for the actuator's range clamp and per-write failure isolation.

Both behaviours exist because the *integration* on the other side of a
``number`` entity owns its bounds and can change them under us: solis_modbus
v4.2.0 started honouring the declared 0–200 A ceiling on registers 43117/43118
that its inverter-rating derivation had previously discarded, which turned
sunSale's ~293 A battery-current write into a ``ServiceValidationError`` — and,
because that exception propagated, silently skipped every later write in
``apply_mode`` including the RC engage/release block.

So the contract pinned here is:

  * a number target is clamped into the entity's advertised ``[min, max]``
    *before* the readback comparison, so a clamped write also becomes idempotent
    rather than re-firing every cycle;
  * a service call that raises is logged, not propagated — the verify loop is
    the designed detector for a write that did not take.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.outbound.entity_control import (
    EntityActuator,
    EntityWrite,
    ServiceWrite,
)

# --- Fakes ------------------------------------------------------------------ #


class _State:
    """Minimal HA state stub carrying arbitrary attributes."""

    def __init__(self, value: str, **attributes: object) -> None:
        self.state = value
        self.attributes: dict = dict(attributes)


class _Hass:
    """Hass stub with per-entity state lookup + AsyncMock service tracking."""

    def __init__(self, states: dict[str, _State] | None = None) -> None:
        self._states: dict[str, _State] = states or {}
        self.services = MagicMock()
        self.services.async_call = AsyncMock()
        self.states = MagicMock()
        self.states.get = self._states.get


_ROLES = {
    "battery_max_charge_current": "number.solis_battery_max_charge_current",
    "rc_setpoint": "number.solis_rc_active_power",
    "mode_select": "select.solis_rc_grid_adjustment",
    "self_use_switch": "switch.solis_self_use_mode",
}

_CHARGE_ENTITY = _ROLES["battery_max_charge_current"]
_RC_ENTITY = _ROLES["rc_setpoint"]


def _actuator(states: dict[str, _State] | None = None) -> tuple[EntityActuator, _Hass]:
    """Return an actuator over the stub hass plus the stub itself."""
    hass = _Hass(states)
    return EntityActuator(hass, _ROLES), hass


def _number_writes(hass: _Hass) -> list[tuple[str, float]]:
    """Return ``(entity_id, value)`` for every ``number.set_value`` call."""
    return [
        (c.args[2]["entity_id"], c.args[2]["value"])
        for c in hass.services.async_call.call_args_list
        if c.args[0] == "number" and c.args[1] == "set_value"
    ]


# --- Clamping --------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_clamps_above_advertised_max() -> None:
    # 15 kW at 51.2 V = 292.97 A, against solis_modbus's declared 0-200 A.
    act, hass = _actuator({_CHARGE_ENTITY: _State("0.0", min=0, max=200)})
    await act.set_number("battery_max_charge_current", 292.96875, force=True)
    assert _number_writes(hass) == [(_CHARGE_ENTITY, 200.0)]


@pytest.mark.asyncio
async def test_clamps_below_advertised_min() -> None:
    # GridCharge's RC setpoint is -max_charge_w, below the entity's -10 kW floor.
    act, hass = _actuator({_RC_ENTITY: _State("0", min=-10000, max=10000)})
    await act.set_number("rc_setpoint", -15000.0, force=True)
    assert _number_writes(hass) == [(_RC_ENTITY, -10000.0)]


@pytest.mark.asyncio
async def test_in_range_target_is_untouched() -> None:
    act, hass = _actuator({_RC_ENTITY: _State("0", min=-10000, max=10000)})
    await act.set_number("rc_setpoint", 9500.0, force=True)
    assert _number_writes(hass) == [(_RC_ENTITY, 9500.0)]


@pytest.mark.asyncio
async def test_entity_without_bounds_is_untouched() -> None:
    act, hass = _actuator({_CHARGE_ENTITY: _State("0.0")})
    await act.set_number("battery_max_charge_current", 292.96875, force=True)
    assert _number_writes(hass) == [(_CHARGE_ENTITY, 292.96875)]


@pytest.mark.asyncio
async def test_unknown_entity_still_writes_unclamped() -> None:
    # Not in the state machine (integration still starting up) — no bounds to
    # apply, and the write is left to HA to accept or reject.
    act, hass = _actuator()
    await act.set_number("battery_max_charge_current", 292.96875, force=True)
    assert _number_writes(hass) == [(_CHARGE_ENTITY, 292.96875)]


@pytest.mark.asyncio
async def test_non_numeric_bounds_are_ignored() -> None:
    act, hass = _actuator({_CHARGE_ENTITY: _State("0.0", min=0, max="n/a")})
    await act.set_number("battery_max_charge_current", 292.96875, force=True)
    assert _number_writes(hass) == [(_CHARGE_ENTITY, 292.96875)]


@pytest.mark.asyncio
async def test_clamped_target_is_idempotent_against_the_readback() -> None:
    # Readback already sits at the ceiling. Without clamping first, 293 vs 200
    # would exceed the tolerance and re-issue a doomed write every cycle.
    act, hass = _actuator({_CHARGE_ENTITY: _State("200.0", min=0, max=200)})
    await act.set_number("battery_max_charge_current", 292.96875)
    assert _number_writes(hass) == []


@pytest.mark.asyncio
async def test_clamp_warns_once_per_target(caplog: pytest.LogCaptureFixture) -> None:
    act, hass = _actuator({_CHARGE_ENTITY: _State("200.0", min=0, max=200)})
    with caplog.at_level(logging.WARNING):
        await act.set_number("battery_max_charge_current", 292.96875, force=True)
        await act.set_number("battery_max_charge_current", 292.96875, force=True)
        # A *different* out-of-range target is a new condition — warn again.
        await act.set_number("battery_max_charge_current", 400.0, force=True)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "clamping to 200" in warnings[0].getMessage()
    assert len(_number_writes(hass)) == 3


@pytest.mark.asyncio
async def test_back_in_range_target_rearms_the_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    act, _ = _actuator({_CHARGE_ENTITY: _State("200.0", min=0, max=200)})
    with caplog.at_level(logging.WARNING):
        await act.set_number("battery_max_charge_current", 292.96875, force=True)
        await act.set_number("battery_max_charge_current", 100.0, force=True)
        await act.set_number("battery_max_charge_current", 292.96875, force=True)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


# --- Failure isolation ------------------------------------------------------ #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write",
    [
        EntityWrite("battery_max_charge_current", "number", 100.0, "Charge current"),
        EntityWrite("mode_select", "select", "OFF", "RC adjustment"),
        EntityWrite("self_use_switch", "switch", True, "Self use"),
    ],
    ids=["number", "select", "switch"],
)
async def test_failed_write_is_logged_not_raised(
    write: EntityWrite, caplog: pytest.LogCaptureFixture
) -> None:
    act, hass = _actuator()
    hass.services.async_call.side_effect = RuntimeError("rejected by HA")
    with caplog.at_level(logging.ERROR):
        await act.apply_write(write, force=True)
    assert hass.services.async_call.await_count == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_failed_service_write_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    act, hass = _actuator()
    hass.services.async_call.side_effect = RuntimeError("rejected by HA")
    with caplog.at_level(logging.ERROR):
        await act.call_service(
            ServiceWrite(
                domain="huawei_solar",
                service="forcible_charge",
                data={"power": 5000},
                target_role="self_use_switch",
            )
        )
    assert hass.services.async_call.await_count == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_a_failed_write_does_not_abort_the_ones_after_it() -> None:
    act, hass = _actuator()
    hass.services.async_call.side_effect = [RuntimeError("rejected by HA"), None, None]
    await act.set_number("battery_max_charge_current", 100.0, force=True)
    await act.set_select("mode_select", "Inverter AC Grid Port", force=True)
    await act.set_number("rc_setpoint", 9500.0, force=True)
    assert hass.services.async_call.await_count == 3
