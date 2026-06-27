"""Persistence round-trip + back-compat for the inverter-mode history store.

Guards the ``reg_43110_value`` → ``raw_state`` rename: histories written before
the rename store the value under the legacy ``reg`` key and must still load.
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.sun_sale.contract.models import (
    InverterModeChange,
    InverterModeHistory,
    StorageMode,
)
from custom_components.sun_sale.orchestration import coordinator as c


def test_legacy_reg_key_deserializes():
    legacy = {"samples": [
        {"ts": "2026-06-24T10:00:00+00:00", "mode": "grid_charge", "reg": 33},
    ]}
    h = c._deserialize_mode_history(legacy)
    assert h.samples[0].raw_state == 33
    assert h.samples[0].mode is StorageMode.GridCharge


def test_roundtrip_uses_raw_state_key():
    h = InverterModeHistory(samples=(
        InverterModeChange(
            timestamp=datetime(2026, 6, 24, 10, tzinfo=timezone.utc),
            mode=StorageMode.SelfUse,
            raw_state=1,
        ),
    ))
    blob = c._serialize_mode_history(h)
    assert blob["samples"][0]["raw_state"] == 1
    assert c._deserialize_mode_history(blob).samples[0].raw_state == 1
