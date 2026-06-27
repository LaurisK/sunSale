"""Tests for number.py — schedule-policy tuning knob entities."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.sun_sale.number import (
    ModeChangePenaltyNumber,
    ProfitabilityTiltAlphaNumber,
    TerminalValueDiscountNumber,
)


def _make_number(cls, attr: str, initial: float):
    """Construct a Number entity bound to a MagicMock coordinator."""
    coord = MagicMock()
    setattr(coord, attr, initial)
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return cls(coord, entry), coord


@pytest.mark.parametrize(
    ("cls", "attr", "suffix", "initial"),
    [
        (ModeChangePenaltyNumber, "mode_change_penalty_eur_per_kwh", "mode_change_penalty", 0.005),
        (ProfitabilityTiltAlphaNumber, "profitability_tilt_alpha", "profitability_tilt_alpha", 0.5),
        (TerminalValueDiscountNumber, "terminal_value_discount", "terminal_value_discount", 0.5),
    ],
)
def test_native_value_mirrors_coordinator(cls, attr, suffix, initial):
    n, _ = _make_number(cls, attr, initial)
    assert n.native_value == pytest.approx(initial)
    assert n._attr_unique_id == f"test_entry_{suffix}"


@pytest.mark.parametrize(
    ("cls", "attr"),
    [
        (ModeChangePenaltyNumber, "mode_change_penalty_eur_per_kwh"),
        (ProfitabilityTiltAlphaNumber, "profitability_tilt_alpha"),
        (TerminalValueDiscountNumber, "terminal_value_discount"),
    ],
)
async def test_set_native_value_writes_to_coordinator(cls, attr):
    n, coord = _make_number(cls, attr, 0.0)
    await n.async_set_native_value(0.25)
    assert getattr(coord, attr) == pytest.approx(0.25)


@pytest.mark.parametrize(
    "cls",
    [ModeChangePenaltyNumber, ProfitabilityTiltAlphaNumber, TerminalValueDiscountNumber],
)
def test_device_info_groups_under_sunsale(cls):
    n, _ = _make_number(cls, "x", 0.0)
    info = n.device_info
    assert any("sun_sale" in str(ident) for ident in info["identifiers"])


# ---------------------------------------------------------------------------
# Restore logic — only numeric strings should overwrite the init default.
# ---------------------------------------------------------------------------


async def test_restore_unknown_state_preserves_default(monkeypatch):
    n, coord = _make_number(
        ModeChangePenaltyNumber, "mode_change_penalty_eur_per_kwh", 0.005,
    )

    async def _fake_last_state():
        return SimpleNamespace(state="unknown")

    monkeypatch.setattr(n, "async_get_last_state", _fake_last_state)
    await n.async_added_to_hass()

    assert coord.mode_change_penalty_eur_per_kwh == pytest.approx(0.005)


async def test_restore_non_numeric_state_preserves_default(monkeypatch):
    n, coord = _make_number(
        ProfitabilityTiltAlphaNumber, "profitability_tilt_alpha", 0.5,
    )

    async def _fake_last_state():
        return SimpleNamespace(state="not-a-number")

    monkeypatch.setattr(n, "async_get_last_state", _fake_last_state)
    await n.async_added_to_hass()

    assert coord.profitability_tilt_alpha == pytest.approx(0.5)


async def test_restore_numeric_state_overrides_default(monkeypatch):
    n, coord = _make_number(
        TerminalValueDiscountNumber, "terminal_value_discount", 0.5,
    )

    async def _fake_last_state():
        return SimpleNamespace(state="0.25")

    monkeypatch.setattr(n, "async_get_last_state", _fake_last_state)
    await n.async_added_to_hass()

    assert coord.terminal_value_discount == pytest.approx(0.25)
