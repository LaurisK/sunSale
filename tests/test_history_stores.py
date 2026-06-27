"""Tests for the declarative history-store registry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.orchestration.history_stores import (
    ALL_HISTORY_SPECS,
    DERIVED_POWER_SPEC,
    GENERATION_SPEC,
    GRID_EXPORT_POWER_SPEC,
    GRID_EXPORT_TOTAL_SPEC,
    GRID_IMPORT_POWER_SPEC,
    GRID_IMPORT_TOTAL_SPEC,
    PV_POWER_SPEC,
    SAMPLE_HISTORY_SPECS,
    append_and_inject,
)
from custom_components.sun_sale.contract.models import (
    DerivedPowerSample,
    GenerationReading,
    GridExportPowerReading,
    GridExportTodayReading,
    GridImportPowerReading,
    GridImportTodayReading,
    PvPowerReading,
)

T0 = datetime(2026, 6, 11, 8, 0, 0, tzinfo=timezone.utc)


class _FakeStore:
    """Minimal stand-in for PersistentStore covering append_and_inject's surface."""

    def __init__(self, value=None):
        """Seed the in-memory sample list."""
        self._value = list(value or [])

    @property
    def value(self):
        """Return the current sample list."""
        return self._value

    async def append_and_trim(self, item, cutoff, ts_fn):
        """Mirror PersistentStore.append_and_trim semantics in memory."""
        items = [x for x in self._value if ts_fn(x) >= cutoff]
        items.append(item)
        self._value = items


# --- Round-trip serialisation --------------------------------------------

def _sample_for(spec):
    """Build one representative reading for ``spec`` with distinct field values."""
    if spec is GENERATION_SPEC:
        return GenerationReading(today_total_kwh=12.5, timestamp=T0)
    if spec is PV_POWER_SPEC:
        return PvPowerReading(power_w=3400.0, timestamp=T0)
    if spec is GRID_IMPORT_POWER_SPEC:
        return GridImportPowerReading(power_kw=1.2, timestamp=T0)
    if spec is GRID_EXPORT_POWER_SPEC:
        return GridExportPowerReading(power_kw=0.8, timestamp=T0)
    if spec is GRID_IMPORT_TOTAL_SPEC:
        return GridImportTodayReading(today_total_kwh=5.5, timestamp=T0)
    if spec is GRID_EXPORT_TOTAL_SPEC:
        return GridExportTodayReading(today_total_kwh=4.4, timestamp=T0)
    if spec is DERIVED_POWER_SPEC:
        return DerivedPowerSample(
            timestamp=T0,
            ac_port_kw_signed=1.1,
            backup_kw=0.2,
            grid_net_kw_signed=-0.3,
            solar_kw=2.0,
            battery_kw_signed=0.5,
        )
    raise AssertionError(f"no sample builder for {spec.storage_key}")


@pytest.mark.parametrize("spec", ALL_HISTORY_SPECS, ids=lambda s: s.storage_key)
def test_serialize_deserialize_round_trips(spec):
    """Each spec serialises then deserialises back to an equal reading."""
    sample = _sample_for(spec)
    payload = spec.serialize([sample])
    restored = spec.deserialize(payload)
    assert restored == [sample]


@pytest.mark.parametrize("spec", ALL_HISTORY_SPECS, ids=lambda s: s.storage_key)
def test_serialize_uses_ts_key_and_field_map(spec):
    """Serialised rows carry the ``ts`` key plus exactly the field_map keys."""
    row = spec.serialize([_sample_for(spec)])["samples"][0]
    assert row["ts"] == T0.isoformat()
    assert set(row) == {"ts", *(jk for jk, _ in spec.field_map)}


def test_generation_legacy_wire_format():
    """Generation keeps the on-disk shape {"ts", "kwh"} the old serializer wrote."""
    sample = GenerationReading(today_total_kwh=7.0, timestamp=T0)
    assert GENERATION_SPEC.serialize([sample]) == {
        "samples": [{"ts": T0.isoformat(), "kwh": 7.0}],
    }


def test_derived_legacy_wire_format():
    """Derived keeps the compact 5-field keys the old serializer wrote."""
    sample = _sample_for(DERIVED_POWER_SPEC)
    assert DERIVED_POWER_SPEC.serialize([sample])["samples"][0] == {
        "ts": T0.isoformat(),
        "ac": 1.1,
        "bu": 0.2,
        "gn": -0.3,
        "sol": 2.0,
        "bat": 0.5,
    }


def test_deserialize_empty_payload_is_empty_list():
    """A payload with no ``samples`` key deserialises to an empty list."""
    assert GENERATION_SPEC.deserialize({}) == []


def test_derived_spec_excluded_from_sample_loop():
    """The composed derived sample is driven explicitly, not by the cycle loop."""
    assert DERIVED_POWER_SPEC not in SAMPLE_HISTORY_SPECS
    assert DERIVED_POWER_SPEC in ALL_HISTORY_SPECS


# --- append_and_inject ----------------------------------------------------

@pytest.mark.asyncio
async def test_append_and_inject_appends_and_publishes():
    """A present reading is appended and the window injected as the history type."""
    store = _FakeStore()
    primary: dict = {}
    current = GenerationReading(today_total_kwh=3.0, timestamp=T0)

    await append_and_inject(GENERATION_SPEC, store, primary, current, T0)

    assert store.value == [current]
    history = primary[GENERATION_SPEC.history_type]
    assert history.samples == (current,)


@pytest.mark.asyncio
async def test_append_and_inject_trims_old_samples():
    """Samples older than the spec's retention window are trimmed on append."""
    old = GenerationReading(
        today_total_kwh=1.0,
        timestamp=T0 - timedelta(days=GENERATION_SPEC.retention_days + 1),
    )
    store = _FakeStore([old])
    primary: dict = {}
    current = GenerationReading(today_total_kwh=2.0, timestamp=T0)

    await append_and_inject(GENERATION_SPEC, store, primary, current, T0)

    assert store.value == [current]
    assert primary[GENERATION_SPEC.history_type].samples == (current,)


@pytest.mark.asyncio
async def test_append_and_inject_none_current_still_injects_window():
    """A missing reading appends nothing but still injects the existing window."""
    existing = GenerationReading(today_total_kwh=9.0, timestamp=T0)
    store = _FakeStore([existing])
    primary: dict = {}

    await append_and_inject(GENERATION_SPEC, store, primary, None, T0)

    assert store.value == [existing]
    assert primary[GENERATION_SPEC.history_type].samples == (existing,)


@pytest.mark.asyncio
async def test_append_and_inject_none_store_injects_empty_history():
    """With no store yet (pre-setup) an empty history is injected."""
    primary: dict = {}
    current = GenerationReading(today_total_kwh=1.0, timestamp=T0)

    await append_and_inject(GENERATION_SPEC, None, primary, current, T0)

    assert primary[GENERATION_SPEC.history_type].samples == ()
