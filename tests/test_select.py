"""Tests for select.py — manual StorageMode override entity."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.sun_sale.contract.models import StorageMode
from custom_components.sun_sale.select import (
    _OVERRIDE_OPTIONS,
    MODE_OVERRIDE_SUNSALE,
    ModeOverrideSelect,
)


def _make_select(mode_override: StorageMode | None = None) -> tuple[ModeOverrideSelect, MagicMock]:
    """Build a ModeOverrideSelect wired to a MagicMock coordinator."""
    coord = MagicMock()
    coord.mode_override = mode_override
    coord.dispatch_mode_override = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return ModeOverrideSelect(coord, entry), coord


def test_options_include_sunsale_sentinel_and_all_dispatchable_modes():
    assert MODE_OVERRIDE_SUNSALE in _OVERRIDE_OPTIONS
    for mode in (
        StorageMode.SelfUse,
        StorageMode.NoExport,
        StorageMode.StandBy,
        StorageMode.GridCharge,
        StorageMode.Discharge,
        StorageMode.FeedIn,
    ):
        assert mode.value in _OVERRIDE_OPTIONS
    # UNKNOWN, TRACK are not exposed as overrides.
    assert StorageMode.UNKNOWN.value not in _OVERRIDE_OPTIONS
    assert StorageMode.TRACK.value not in _OVERRIDE_OPTIONS
    assert _OVERRIDE_OPTIONS.count(MODE_OVERRIDE_SUNSALE) == 1


def test_current_option_returns_sunsale_when_no_override():
    sel, _ = _make_select(mode_override=None)
    assert sel.current_option == MODE_OVERRIDE_SUNSALE


def test_current_option_returns_mode_value_when_override_set():
    sel, _ = _make_select(mode_override=StorageMode.Discharge)
    assert sel.current_option == "discharge"


async def test_select_option_clears_override_when_sunsale():
    sel, coord = _make_select(mode_override=StorageMode.Discharge)
    await sel.async_select_option(MODE_OVERRIDE_SUNSALE)
    assert coord.mode_override is None
    coord.dispatch_mode_override.assert_awaited_once()


@pytest.mark.parametrize(
    "option,expected",
    [
        ("self_use", StorageMode.SelfUse),
        ("discharge", StorageMode.Discharge),
        ("grid_charge", StorageMode.GridCharge),
        ("stand_by", StorageMode.StandBy),
        ("feed_in", StorageMode.FeedIn),
    ],
)
async def test_select_option_sets_override_to_chosen_mode(option, expected):
    sel, coord = _make_select(mode_override=None)
    await sel.async_select_option(option)
    assert coord.mode_override == expected
    coord.dispatch_mode_override.assert_awaited_once()


async def test_select_option_ignores_unknown_label():
    sel, coord = _make_select(mode_override=StorageMode.SelfUse)
    await sel.async_select_option("bogus")
    assert coord.mode_override == StorageMode.SelfUse
    coord.dispatch_mode_override.assert_not_awaited()


def test_unique_id_uses_entry_id():
    sel, _ = _make_select()
    assert sel._attr_unique_id == "test_entry_mode_override"


def test_device_info_contains_domain_identifier():
    sel, _ = _make_select()
    info = sel.device_info
    assert any("sun_sale" in str(ident) for ident in info["identifiers"])


async def test_restore_state_applies_persisted_override(monkeypatch):
    sel, coord = _make_select(mode_override=None)

    async def _fake_last_state():
        return SimpleNamespace(state="discharge")

    monkeypatch.setattr(sel, "async_get_last_state", _fake_last_state)
    await sel.async_added_to_hass()

    assert coord.mode_override == StorageMode.Discharge


async def test_restore_state_clears_override_when_sunsale(monkeypatch):
    sel, coord = _make_select(mode_override=StorageMode.Discharge)

    async def _fake_last_state():
        return SimpleNamespace(state=MODE_OVERRIDE_SUNSALE)

    monkeypatch.setattr(sel, "async_get_last_state", _fake_last_state)
    await sel.async_added_to_hass()

    assert coord.mode_override is None


async def test_restore_state_unknown_leaves_coordinator_default(monkeypatch):
    sel, coord = _make_select(mode_override=None)

    async def _fake_last_state():
        return SimpleNamespace(state="unknown")

    monkeypatch.setattr(sel, "async_get_last_state", _fake_last_state)
    await sel.async_added_to_hass()

    assert coord.mode_override is None
