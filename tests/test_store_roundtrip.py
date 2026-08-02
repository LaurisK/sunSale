"""Round-trip + golden-payload tests for every persisted store type.

Two guards against the schema-drift class that poisoned the capacity estimate
(fixed 2026-06-13):

1. **Round-trip** — for every store spec (rolling sample histories in
   ``ALL_HISTORY_SPECS`` and singletons in ``SINGLETON_STORE_SPECS``), plus the
   bespoke capacity estimator, assert ``deserialize(json(serialize(x))) == x``.
   The JSON hop (not just a dict round-trip) catches non-JSON-safe values —
   HA's ``Store`` persists JSON.

2. **Golden payloads** — ``tests/fixtures/store_payloads/<storage_key>.json``
   holds one frozen, realistic payload per singleton store. A test deserialises
   each and asserts its key fields. **These fixtures are append-only:** changing
   one means a stored-schema change and must come with a ``STORAGE_VERSION``
   bump or an explicit in-codec migration path (as ``CapacityEstimator.from_dict``
   already does for the schema-version purge). Regenerate them only via
   ``tools/gen_store_fixtures`` when that condition is met.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from custom_components.sun_sale.contract.const import (
    STORAGE_KEY_BAKED_OBSERVED,
    STORAGE_KEY_CONSUMPTION_DAILY,
    STORAGE_KEY_COUNTER_SNAPSHOT,
    STORAGE_KEY_FORECAST_QUALITY,
    STORAGE_KEY_MODE_HISTORY,
    STORAGE_KEY_MONTHLY_BILL,
    STORAGE_KEY_PRICE_HISTORY,
    STORAGE_KEY_YESTERDAY,
)
from custom_components.sun_sale.contract.models import (
    AccuracyBucketState,
    BakedDayRecord,
    BakedObservedHistory,
    CapacityObservation,
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
    CounterSnapshotHistory,
    CounterSnapshotRecord,
    DailyPeak,
    DayClass,
    ForecastQualityStore,
    InverterModeChange,
    InverterModeHistory,
    MonthlyBillState,
    PriceEntry,
    SlotKwh,
    SolarEntry,
    StorageMode,
)
from custom_components.sun_sale.orchestration.history_stores import ALL_HISTORY_SPECS
from custom_components.sun_sale.orchestration.store_codecs import (
    SINGLETON_STORE_SPECS,
    YesterdayBuckets,
)
from custom_components.sun_sale.pipeline.battery import (
    _CAPACITY_SCHEMA_VERSION,
    CapacityEstimator,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "store_payloads"

_T0 = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
_T1 = datetime(2026, 7, 10, 8, 5, tzinfo=UTC)
_T2 = datetime(2026, 7, 11, 12, 30, tzinfo=UTC)


def _json_roundtrip(payload: dict) -> dict:
    """Return ``payload`` after a JSON encode/decode hop (mirrors HA's Store)."""
    return json.loads(json.dumps(payload))


# ---------------------------------------------------------------------------
# Representative-value factories — 2–3 instances each, edge cases included
# ---------------------------------------------------------------------------

def _rep_history(spec) -> list:
    """Build a representative reading list for a history spec from its field_map.

    Each spec's ``reading_type`` is ``timestamp`` + the ``field_map`` value
    fields. We assign distinct floats (including a negative and a zero) so the
    round-trip exercises sign and empty-ish edges regardless of the concrete
    reading type.
    """
    value_rows = (
        {attr: 0.0 for _jk, attr in spec.field_map},
        {attr: 1.5 for _jk, attr in spec.field_map},
        {attr: -2.25 for _jk, attr in spec.field_map},
    )
    stamps = (_T0, _T1, _T2)
    return [
        spec.reading_type(timestamp=ts, **vals)
        for ts, vals in zip(stamps, value_rows, strict=True)
    ]


def _rep_consumption_daily() -> ConsumptionDailyBuckets:
    """Return a two-day consumption rollup with full 24-hour tuples."""
    kwh = tuple(float(h) * 0.1 for h in range(24))
    cov = tuple(1.0 for _ in range(24))
    zero_cov = tuple(0.0 for _ in range(24))
    return ConsumptionDailyBuckets(records=(
        ConsumptionDayRecord(
            local_date=date(2026, 7, 9), hour_kwh=kwh,
            hour_completeness=cov, finalised_at=_T0,
        ),
        ConsumptionDayRecord(
            local_date=date(2026, 7, 10), hour_kwh=tuple(0.0 for _ in range(24)),
            hour_completeness=zero_cov, finalised_at=_T2,
        ),
    ))


def _rep_price_history() -> list[DailyPeak]:
    """Return daily peaks covering all three day-classes, incl. a negative peak."""
    return [
        DailyPeak(day=date(2026, 7, 9), peak_eur_kwh=0.2531, day_class=DayClass.WEEKDAY),
        DailyPeak(day=date(2026, 7, 11), peak_eur_kwh=-0.014, day_class=DayClass.WEEKEND),
        DailyPeak(day=date(2026, 7, 13), peak_eur_kwh=0.0, day_class=DayClass.HOLIDAY),
    ]


def _rep_forecast_quality() -> ForecastQualityStore:
    """Return a quality store with a populated bucket and a pending record."""
    return ForecastQualityStore(
        group1={"0": AccuracyBucketState(ema_error=0.1, ema_abs_error=0.2, n=5)},
        group2={"1": AccuracyBucketState(ema_obs=1.5, ema_obs_sq=2.25, n=3)},
        group3={"0": AccuracyBucketState(n=0)},
        group3_pending=[{"target_date": "2026-07-11", "horizon": 1, "forecast_kwh": 2.5}],
    )


def _rep_monthly_bill() -> MonthlyBillState:
    """Return a ledger with multiple days, incl. a negative (net-revenue) day."""
    return MonthlyBillState(finalized_days=(
        ("2026-07-09", 1.25),
        ("2026-07-10", -0.5),
        ("2026-07-11", 0.0),
    ))


def _rep_yesterday() -> YesterdayBuckets:
    """Return two-bucket state with populated yesterday + today slices."""
    return YesterdayBuckets(
        yesterday_date="2026-07-09",
        yesterday_nordpool=[PriceEntry(start=_T0, end=_T1, price_eur_kwh=0.12)],
        yesterday_solar=[SolarEntry(start=_T0, end=_T1, expected_kwh=0.4, source="fcst")],
        today_date="2026-07-10",
        today_nordpool=[PriceEntry(start=_T1, end=_T2, price_eur_kwh=-0.03)],
        today_solar=[SolarEntry(start=_T1, end=_T2, expected_kwh=0.0, source="fcst")],
    )


def _rep_mode_history() -> InverterModeHistory:
    """Return a mode history spanning several distinct modes."""
    return InverterModeHistory(samples=(
        InverterModeChange(timestamp=_T0, mode=StorageMode.SelfUse, raw_state=1),
        InverterModeChange(timestamp=_T1, mode=StorageMode.GridCharge, raw_state=33),
        InverterModeChange(timestamp=_T2, mode=StorageMode.UNKNOWN, raw_state=999),
    ))


def _rep_counter_snapshot() -> CounterSnapshotHistory:
    """Return counter snapshots for all three sides."""
    return CounterSnapshotHistory(records=(
        CounterSnapshotRecord(side_id="generation", captured_at=_T0, today_total_kwh=12.3),
        CounterSnapshotRecord(side_id="grid_import", captured_at=_T0, today_total_kwh=0.0),
        CounterSnapshotRecord(side_id="grid_export", captured_at=_T0, today_total_kwh=4.56),
    ))


def _rep_baked_observed() -> BakedObservedHistory:
    """Return baked records incl. an empty-slots failed-source record."""
    return BakedObservedHistory(records=(
        BakedDayRecord(
            date_str="2026-07-09", side_id="generation", counter_total_used=20.0,
            source_kind="dedicated_sensor",
            baked_slots=(SlotKwh(start=_T0, end=_T1, kwh=1.0), SlotKwh(start=_T1, end=_T2, kwh=2.0)),
            baked_sum=3.0, baked_at=_T2,
        ),
        BakedDayRecord(
            date_str="2026-07-09", side_id="grid_export", counter_total_used=0.0,
            source_kind="failed_no_source", baked_slots=(), baked_sum=0.0, baked_at=_T2,
        ),
    ))


# storage_key -> representative singleton value
_SINGLETON_REPRESENTATIVE = {
    STORAGE_KEY_CONSUMPTION_DAILY: _rep_consumption_daily,
    STORAGE_KEY_PRICE_HISTORY: _rep_price_history,
    STORAGE_KEY_FORECAST_QUALITY: _rep_forecast_quality,
    STORAGE_KEY_MONTHLY_BILL: _rep_monthly_bill,
    STORAGE_KEY_YESTERDAY: _rep_yesterday,
    STORAGE_KEY_MODE_HISTORY: _rep_mode_history,
    STORAGE_KEY_COUNTER_SNAPSHOT: _rep_counter_snapshot,
    STORAGE_KEY_BAKED_OBSERVED: _rep_baked_observed,
}


# ---------------------------------------------------------------------------
# Round-trip: history specs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ALL_HISTORY_SPECS, ids=lambda s: s.storage_key)
def test_history_spec_json_roundtrip(spec):
    """A history spec reconstructs an identical reading list after a JSON hop."""
    readings = _rep_history(spec)
    restored = spec.deserialize(_json_roundtrip(spec.serialize(readings)))
    assert restored == readings


@pytest.mark.parametrize("spec", ALL_HISTORY_SPECS, ids=lambda s: s.storage_key)
def test_history_spec_empty_roundtrip(spec):
    """An empty history serialises and reloads to an empty list."""
    assert spec.deserialize(_json_roundtrip(spec.serialize([]))) == []


# ---------------------------------------------------------------------------
# Round-trip: singleton specs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SINGLETON_STORE_SPECS, ids=lambda s: s.storage_key)
def test_singleton_spec_json_roundtrip(spec):
    """A singleton spec reconstructs an equal value after a JSON hop."""
    value = _SINGLETON_REPRESENTATIVE[spec.storage_key]()
    restored = spec.deserialize(_json_roundtrip(spec.serialize(value)))
    assert restored == value


@pytest.mark.parametrize("spec", SINGLETON_STORE_SPECS, ids=lambda s: s.storage_key)
def test_singleton_spec_default_is_empty(spec):
    """When a spec declares a default factory it produces an empty container."""
    if spec.default is None:
        pytest.skip("no default declared (absent value is meaningful)")
    empty = spec.default()
    # Re-serialising the empty default and reloading it must be a fixed point.
    assert spec.deserialize(_json_roundtrip(spec.serialize(empty))) == empty


def test_every_singleton_spec_has_a_representative():
    """Guard: a newly added singleton store must gain a representative here."""
    covered = {s.storage_key for s in SINGLETON_STORE_SPECS}
    assert covered == set(_SINGLETON_REPRESENTATIVE)


# ---------------------------------------------------------------------------
# Round-trip: capacity estimator (bespoke, config-closed codec)
# ---------------------------------------------------------------------------

def test_capacity_estimator_json_roundtrip():
    """The capacity estimator reloads identically with matching rt-eff/nominal."""
    est = CapacityEstimator(
        nominal_capacity_kwh=28.0,
        observations=[
            CapacityObservation(timestamp=_T0, soc_start=0.2, soc_end=0.8,
                                energy_kwh=15.0, direction="charge"),
            CapacityObservation(timestamp=_T1, soc_start=0.9, soc_end=0.3,
                                energy_kwh=14.0, direction="discharge"),
        ],
        round_trip_efficiency=0.92,
    )
    payload = _json_roundtrip(est.to_dict())
    restored = CapacityEstimator.from_dict(payload, 0.92, 28.0)
    assert restored.to_dict() == est.to_dict()


# ---------------------------------------------------------------------------
# Golden payloads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SINGLETON_STORE_SPECS, ids=lambda s: s.storage_key)
def test_golden_payload_deserializes(spec):
    """The frozen on-disk fixture for each store deserialises to its value.

    Golden fixtures are append-only — see the module docstring. A codec change
    that renames or drops a field breaks this test, which is the point.
    """
    fixture = FIXTURE_DIR / f"{spec.storage_key}.json"
    assert fixture.exists(), f"missing golden fixture: {fixture}"
    payload = json.loads(fixture.read_text())
    restored = spec.deserialize(payload)
    # The golden payload is the serialised representative — deserialising it
    # must reproduce that same value.
    assert restored == _SINGLETON_REPRESENTATIVE[spec.storage_key]()


def test_capacity_stale_schema_purges_to_nominal():
    """A pre-fix (schema_version 1) capacity fixture resets to nominal on load.

    Pins the exact 2026-06-13 bug: pre-schema-2 observations were built from a
    fabricated 0.5-SoC fallback and are unrecoverable poison, so ``from_dict``
    must discard them and fall back to the nominal capacity.
    """
    fixture = FIXTURE_DIR / "capacity_stale_schema.json"
    payload = json.loads(fixture.read_text())
    assert payload["schema_version"] < _CAPACITY_SCHEMA_VERSION
    restored = CapacityEstimator.from_dict(payload, 0.92, 28.0)
    # Poison observations dropped; estimate collapses back to nominal.
    assert restored.to_dict()["observations"] == []
    assert restored.estimated_capacity_kwh == pytest.approx(28.0)
