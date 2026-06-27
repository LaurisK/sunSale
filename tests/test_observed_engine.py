"""Tests for inbound/observer/engine.py — pure Python, no HA required."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import SlotKwh
from custom_components.sun_sale.inbound.observer.engine import (
    BAKE_IN_FACTOR_MAX,
    BAKE_IN_FACTOR_MIN,
    BAKE_STATUS_OK,
    BAKE_STATUS_SKIPPED_NO_SOURCE,
    BAKE_STATUS_SKIPPED_OUT_OF_RANGE,
    BAKE_STATUS_SKIPPED_ZERO_SUM,
    ObservedSeriesEngine,
    Side,
)
from tests.conftest import BASE_DT


NOW = BASE_DT  # 2024-01-15 00:00 UTC


# ---------------------------------------------------------------------------
# Minimal stand-in dataclasses for samples + price slots used in engine tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PowerSample:
    """One synthetic power sample with a single magnitude attribute.

    Attributes:
        timestamp: UTC timestamp of the sample.
        kw: Signed power in kW. Generation tests use positive only; grid tests
            use signed values (positive = import, negative = export).
    """
    timestamp: datetime
    kw: float


@dataclass(frozen=True)
class _Slot:
    """Minimal price-grid slot for engine input."""
    start: datetime
    end: datetime


def _hourly_slots(start: datetime, hours: int) -> list[_Slot]:
    """Return ``hours`` consecutive 1-hour slots starting at ``start``."""
    return [
        _Slot(start + timedelta(hours=h), start + timedelta(hours=h + 1))
        for h in range(hours)
    ]


def _gen_side() -> Side:
    """Single-track side returning sample kW as-is (generation convention)."""
    return Side(id="generation", extract=lambda s: max(0.0, s.kw))


def _grid_sides() -> list[Side]:
    """Two-track sides each reading their own non-negative sample stream."""
    return [
        Side(id="grid_import", extract=lambda s: max(0.0, s.kw)),
        Side(id="grid_export", extract=lambda s: max(0.0, s.kw)),
    ]


# ---------------------------------------------------------------------------
# build_slots_for_window
# ---------------------------------------------------------------------------


def test_build_window_empty_samples_zero_slots() -> None:
    """No samples → every slot in window emits 0.0 kWh for every side."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    slots = _hourly_slots(NOW, 3)
    out = engine.build_slots_for_window(
        samples_by_side={"generation": []},
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=3),
    )
    assert list(out.keys()) == ["generation"]
    assert [s.kwh for s in out["generation"]] == [0.0, 0.0, 0.0]


def test_build_window_single_sample_in_slot() -> None:
    """One sample at 2 kW for 1h → 2.0 kWh for that slot."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    slots = _hourly_slots(NOW, 2)
    sample = _PowerSample(timestamp=NOW + timedelta(minutes=30), kw=2.0)
    out = engine.build_slots_for_window(
        samples_by_side={"generation": [sample]},
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=2),
    )
    assert out["generation"][0].kwh == 2.0
    assert out["generation"][1].kwh == 0.0


def test_build_window_multiple_samples_average_to_mean_kw() -> None:
    """Three samples 1, 2, 3 kW in same slot → mean 2 kW × 1 h = 2.0 kWh."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    slots = _hourly_slots(NOW, 1)
    samples = [
        _PowerSample(NOW + timedelta(minutes=10), kw=1.0),
        _PowerSample(NOW + timedelta(minutes=30), kw=2.0),
        _PowerSample(NOW + timedelta(minutes=50), kw=3.0),
    ]
    out = engine.build_slots_for_window(
        samples_by_side={"generation": samples},
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
    )
    assert out["generation"][0].kwh == 2.0


def test_build_window_clamps_slot_end_to_window_end() -> None:
    """A slot whose end exceeds window_end uses the clamped duration for kWh."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    slots = [_Slot(NOW, NOW + timedelta(hours=1))]
    # Two samples in the first 30 min, window ends at 30 min — duration 0.5h.
    samples = [
        _PowerSample(NOW + timedelta(minutes=5), kw=4.0),
        _PowerSample(NOW + timedelta(minutes=15), kw=4.0),
    ]
    out = engine.build_slots_for_window(
        samples_by_side={"generation": samples},
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(minutes=30),
    )
    # 4 kW mean × 0.5 h = 2.0 kWh
    assert out["generation"][0].kwh == 2.0


def test_build_window_drops_slots_outside_window() -> None:
    """Slots whose start lies outside the window are not emitted."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    slots = _hourly_slots(NOW - timedelta(hours=2), 6)  # spans -2h … +4h
    out = engine.build_slots_for_window(
        samples_by_side={"generation": []},
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=3),
    )
    # Only slots starting at NOW, NOW+1h, NOW+2h fall in [NOW, NOW+3h).
    assert len(out["generation"]) == 3


def test_build_window_grid_two_sides_independent_streams() -> None:
    """Each side averages its OWN sample stream — no sign-split needed."""
    engine = ObservedSeriesEngine(_grid_sides(), local_tz=timezone.utc)
    slots = _hourly_slots(NOW, 1)
    # Import side has two readings of 2 kW; export side has two readings of
    # 3 kW. Each side averages over ITS samples (no cross-contamination).
    import_samples = [
        _PowerSample(NOW + timedelta(minutes=10), kw=2.0),
        _PowerSample(NOW + timedelta(minutes=20), kw=2.0),
    ]
    export_samples = [
        _PowerSample(NOW + timedelta(minutes=40), kw=3.0),
        _PowerSample(NOW + timedelta(minutes=50), kw=3.0),
    ]
    out = engine.build_slots_for_window(
        samples_by_side={
            "grid_import": import_samples,
            "grid_export": export_samples,
        },
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
    )
    # Import side mean = 2.0 kW × 1 h = 2.0 kWh.
    assert out["grid_import"][0].kwh == 2.0
    # Export side mean = 3.0 kW × 1 h = 3.0 kWh.
    assert out["grid_export"][0].kwh == 3.0


def test_build_window_side_with_empty_stream_emits_zero() -> None:
    """A side whose stream is empty (or missing) gets all-zero slots."""
    engine = ObservedSeriesEngine(_grid_sides(), local_tz=timezone.utc)
    slots = _hourly_slots(NOW, 1)
    out = engine.build_slots_for_window(
        samples_by_side={
            "grid_import": [_PowerSample(NOW + timedelta(minutes=30), kw=2.0)],
            # export omitted entirely
        },
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
    )
    assert out["grid_import"][0].kwh == 2.0
    assert out["grid_export"][0].kwh == 0.0


def test_build_window_negative_extract_clamped_to_zero() -> None:
    """An extractor returning a negative value contributes 0 to the mean.

    A side's extractor is contracted to return ≥ 0; using ``max(0, ...)`` is the
    standard idiom. Negative inputs round-trip via the extractor as 0.
    """
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    slots = _hourly_slots(NOW, 1)
    samples = [
        _PowerSample(NOW + timedelta(minutes=15), kw=-5.0),  # negative → 0
        _PowerSample(NOW + timedelta(minutes=45), kw=4.0),
    ]
    out = engine.build_slots_for_window(
        samples_by_side={"generation": samples},
        price_slots=slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
    )
    # mean = (0 + 4) / 2 = 2 kW → 2.0 kWh
    assert out["generation"][0].kwh == 2.0


# ---------------------------------------------------------------------------
# apply_proportional_bake_in
# ---------------------------------------------------------------------------


def _slot(idx: int, kwh: float) -> SlotKwh:
    """Helper: construct a SlotKwh at hour ``idx`` of NOW with the given kWh."""
    return SlotKwh(
        start=NOW + timedelta(hours=idx),
        end=NOW + timedelta(hours=idx + 1),
        kwh=kwh,
    )


def test_bake_in_ok_proportional_scales_all_slots() -> None:
    """Counter = 10, slot_sum = 8 → factor = 1.25 applied to every slot."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    raw = {"generation": [_slot(0, 2.0), _slot(1, 4.0), _slot(2, 2.0)]}
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"generation": 10.0},
    )
    slots, status, factor = out["generation"]
    assert status == BAKE_STATUS_OK
    assert factor == 1.25
    assert [s.kwh for s in slots] == [2.5, 5.0, 2.5]


def test_bake_in_ok_preserves_zero_slots() -> None:
    """Zero stays zero — proportional, not additive."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    raw = {"generation": [_slot(0, 0.0), _slot(1, 5.0), _slot(2, 0.0)]}
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"generation": 6.0},
    )
    slots, status, _ = out["generation"]
    assert status == BAKE_STATUS_OK
    assert slots[0].kwh == 0.0
    assert slots[2].kwh == 0.0
    assert slots[1].kwh == 6.0


def test_bake_in_skipped_no_source() -> None:
    """None counter total → status is no_source, slots unchanged."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    raw = {"generation": [_slot(0, 2.0), _slot(1, 3.0)]}
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"generation": None},
    )
    slots, status, factor = out["generation"]
    assert status == BAKE_STATUS_SKIPPED_NO_SOURCE
    assert factor is None
    assert [s.kwh for s in slots] == [2.0, 3.0]


def test_bake_in_skipped_zero_sum() -> None:
    """slot_sum == 0 cannot be scaled up; status reports zero_sum."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    raw = {"generation": [_slot(0, 0.0), _slot(1, 0.0)]}
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"generation": 5.0},
    )
    slots, status, factor = out["generation"]
    assert status == BAKE_STATUS_SKIPPED_ZERO_SUM
    assert factor is None
    assert [s.kwh for s in slots] == [0.0, 0.0]


def test_bake_in_skipped_factor_too_high() -> None:
    """Factor > BAKE_IN_FACTOR_MAX → skipped with factor surfaced for logging."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    raw = {"generation": [_slot(0, 1.0)]}
    # counter = 10, slot_sum = 1 → factor = 10 (way above MAX)
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"generation": 10.0},
    )
    slots, status, factor = out["generation"]
    assert status == BAKE_STATUS_SKIPPED_OUT_OF_RANGE
    assert factor == 10.0
    assert slots[0].kwh == 1.0


def test_bake_in_skipped_factor_too_low() -> None:
    """Factor < BAKE_IN_FACTOR_MIN → skipped with factor surfaced."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    raw = {"generation": [_slot(0, 10.0)]}
    # counter = 1, slot_sum = 10 → factor = 0.1 (below MIN)
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"generation": 1.0},
    )
    slots, status, factor = out["generation"]
    assert status == BAKE_STATUS_SKIPPED_OUT_OF_RANGE
    assert factor == 0.1
    assert slots[0].kwh == 10.0


def test_bake_in_factor_exactly_at_boundaries_accepted() -> None:
    """factor == MIN and factor == MAX are inside the inclusive guard."""
    engine = ObservedSeriesEngine([_gen_side()], local_tz=timezone.utc)
    # factor = MAX
    raw_max = {"generation": [_slot(0, 1.0)]}
    out_max = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw_max,
        counter_totals_per_side={"generation": BAKE_IN_FACTOR_MAX},
    )
    assert out_max["generation"][1] == BAKE_STATUS_OK

    # factor = MIN
    raw_min = {"generation": [_slot(0, 1.0)]}
    out_min = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw_min,
        counter_totals_per_side={"generation": BAKE_IN_FACTOR_MIN},
    )
    assert out_min["generation"][1] == BAKE_STATUS_OK


def test_bake_in_multi_side_independent_statuses() -> None:
    """Each side's bake-in is independent: one can ok while the other skips."""
    engine = ObservedSeriesEngine(_grid_sides(), local_tz=timezone.utc)
    raw = {
        "grid_import": [_slot(0, 2.0), _slot(1, 2.0)],
        "grid_export": [_slot(0, 0.0), _slot(1, 0.0)],   # zero_sum
    }
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={
            "grid_import": 5.0,      # factor = 1.25 → ok
            "grid_export": 3.0,      # zero_sum
        },
    )
    imp_slots, imp_status, imp_factor = out["grid_import"]
    exp_slots, exp_status, exp_factor = out["grid_export"]
    assert imp_status == BAKE_STATUS_OK
    assert imp_factor == 1.25
    assert [s.kwh for s in imp_slots] == [2.5, 2.5]
    assert exp_status == BAKE_STATUS_SKIPPED_ZERO_SUM
    assert exp_factor is None


def test_bake_in_missing_side_in_counter_dict_treated_as_no_source() -> None:
    """A registered side with no entry in counter_totals_per_side → no_source."""
    engine = ObservedSeriesEngine(_grid_sides(), local_tz=timezone.utc)
    raw = {
        "grid_import": [_slot(0, 2.0)],
        "grid_export": [_slot(0, 1.0)],
    }
    out = engine.apply_proportional_bake_in(
        raw_slots_per_side=raw,
        counter_totals_per_side={"grid_import": 3.0},  # export omitted
    )
    assert out["grid_import"][1] == BAKE_STATUS_OK
    assert out["grid_export"][1] == BAKE_STATUS_SKIPPED_NO_SOURCE
