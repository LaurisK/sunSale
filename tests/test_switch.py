"""Tests for switch.py — automation master and schedule-policy toggles."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.switch import (
    AllowDischargeToGridSwitch,
    AllowFeedInSwitch,
    AllowGridChargingSwitch,
    AutomationSwitch,
    UseStandbySwitch,
)


def make_switch(automation_enabled: bool = True):
    coord = MagicMock()
    coord.automation_enabled = automation_enabled
    entry = MagicMock()
    entry.entry_id = "test_entry"
    sw = AutomationSwitch(coord, entry)
    return sw, coord


def test_is_on_when_enabled():
    sw, _ = make_switch(automation_enabled=True)
    assert sw.is_on is True


def test_is_off_when_disabled():
    sw, _ = make_switch(automation_enabled=False)
    assert sw.is_on is False


async def test_turn_on_enables_automation():
    sw, coord = make_switch(automation_enabled=False)
    await sw.async_turn_on()
    assert coord.automation_enabled is True


async def test_turn_off_disables_automation():
    sw, coord = make_switch(automation_enabled=True)
    await sw.async_turn_off()
    assert coord.automation_enabled is False


def test_unique_id_uses_entry_id():
    sw, _ = make_switch()
    assert sw._attr_unique_id == "test_entry_enabled"


def test_device_info_contains_domain_identifier():
    sw, _ = make_switch()
    info = sw.device_info
    assert any("sun_sale" in str(ident) for ident in info["identifiers"])


# ---------------------------------------------------------------------------
# Policy switches (use_standby / allow_grid_charging / allow_feed_in /
# allow_discharge_to_grid)
# ---------------------------------------------------------------------------


def _make_policy_switch(cls, **coord_attrs):
    """Build a policy switch wired to a MagicMock coordinator with the given attrs."""
    coord = MagicMock()
    for k, v in coord_attrs.items():
        setattr(coord, k, v)
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return cls(coord, entry), coord


@pytest.mark.parametrize(
    ("cls", "attr", "suffix"),
    [
        (UseStandbySwitch, "use_standby", "use_standby"),
        (AllowGridChargingSwitch, "allow_grid_charging", "allow_grid_charging"),
        (AllowFeedInSwitch, "allow_feed_in", "allow_feed_in"),
        (AllowDischargeToGridSwitch, "allow_discharge_to_grid", "allow_discharge_to_grid"),
    ],
)
def test_policy_switch_mirrors_coordinator(cls, attr, suffix):
    sw, coord = _make_policy_switch(cls, **{attr: True})
    assert sw.is_on is True
    assert sw._attr_unique_id == f"test_entry_{suffix}"
    setattr(coord, attr, False)
    assert sw.is_on is False


@pytest.mark.parametrize(
    ("cls", "attr"),
    [
        (UseStandbySwitch, "use_standby"),
        (AllowGridChargingSwitch, "allow_grid_charging"),
        (AllowFeedInSwitch, "allow_feed_in"),
        (AllowDischargeToGridSwitch, "allow_discharge_to_grid"),
    ],
)
async def test_policy_switch_turn_on_off(cls, attr):
    sw, coord = _make_policy_switch(cls, **{attr: False})
    await sw.async_turn_on()
    assert getattr(coord, attr) is True
    await sw.async_turn_off()
    assert getattr(coord, attr) is False


# ---------------------------------------------------------------------------
# Restore-state fix — unknown/unavailable must not flip a default-True flag
# ---------------------------------------------------------------------------


async def test_restore_unknown_state_preserves_default(monkeypatch):
    """`unknown` carries no policy decision; coordinator default must survive."""
    sw, coord = _make_policy_switch(UseStandbySwitch, use_standby=True)

    async def _fake_last_state():
        return SimpleNamespace(state="unknown")

    monkeypatch.setattr(sw, "async_get_last_state", _fake_last_state)
    await sw.async_added_to_hass()

    assert coord.use_standby is True


async def test_restore_unavailable_state_preserves_default(monkeypatch):
    sw, coord = _make_policy_switch(AllowGridChargingSwitch, allow_grid_charging=True)

    async def _fake_last_state():
        return SimpleNamespace(state="unavailable")

    monkeypatch.setattr(sw, "async_get_last_state", _fake_last_state)
    await sw.async_added_to_hass()

    assert coord.allow_grid_charging is True


async def test_restore_explicit_off_state_persists(monkeypatch):
    """An explicit 'off' restore must flip the coord_attr, no matter the default."""
    sw, coord = _make_policy_switch(AllowFeedInSwitch, allow_feed_in=True)

    async def _fake_last_state():
        return SimpleNamespace(state="off")

    monkeypatch.setattr(sw, "async_get_last_state", _fake_last_state)
    await sw.async_added_to_hass()

    assert coord.allow_feed_in is False


async def test_restore_explicit_on_state_persists(monkeypatch):
    sw, coord = _make_policy_switch(AllowDischargeToGridSwitch, allow_discharge_to_grid=False)

    async def _fake_last_state():
        return SimpleNamespace(state="on")

    monkeypatch.setattr(sw, "async_get_last_state", _fake_last_state)
    await sw.async_added_to_hass()

    assert coord.allow_discharge_to_grid is True
