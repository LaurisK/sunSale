"""Tests for inbound/observer/grid.py — ObservedGridSeries assembly.

After the per-direction split, the legacy ``GridObserver`` is gone; the
builder consumes two non-negative histories (import / export) directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import (
    GridExportPowerHistory,
    GridExportPowerReading,
    GridImportPowerHistory,
    GridImportPowerReading,
    PriceEntry,
    PriceSeries,
)
from custom_components.sun_sale.inbound.observer.grid import build_observed_grid_series
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_tariff_config

NOW = BASE_DT
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


def _hourly_72h_price_series() -> PriceSeries:
    """72h hourly price grid covering yesterday 00:00 → tomorrow 23:59 (UTC)."""
    base = NOW - timedelta(days=1)
    entries = [
        PriceEntry(
            start=base + timedelta(hours=h),
            end=base + timedelta(hours=h + 1),
            price_eur_kwh=0.10,
        )
        for h in range(72)
    ]
    return build_price_series(entries, default_tariff_config(), now=NOW)


def _imp(t: datetime, kw: float) -> GridImportPowerReading:
    """Build an import-power sample (non-negative kW)."""
    return GridImportPowerReading(power_kw=kw, timestamp=t)


def _exp(t: datetime, kw: float) -> GridExportPowerReading:
    """Build an export-power sample (non-negative kW)."""
    return GridExportPowerReading(power_kw=kw, timestamp=t)


def _empty_export() -> GridExportPowerHistory:
    return GridExportPowerHistory(samples=())


def _empty_import() -> GridImportPowerHistory:
    return GridImportPowerHistory(samples=())


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------

def test_empty_history_yields_empty_series():
    series = build_observed_grid_series(
        _empty_import(), _empty_export(),
        _hourly_72h_price_series().slots,
        now=NOW,
    )
    assert series.slots == ()


def test_empty_price_grid_yields_empty_series():
    history = GridImportPowerHistory(samples=(_imp(NOW, 1.0),))
    series = build_observed_grid_series(history, _empty_export(), (), now=NOW)
    assert series.slots == ()


# ---------------------------------------------------------------------------
# Per-slot averaging — import side
# ---------------------------------------------------------------------------

def test_pure_import_slot_averages_to_imported_kwh():
    """A slot with steady 2 kW import for one hour → 2 kWh imported."""
    now = NOW.replace(hour=12)
    import_samples = tuple(
        _imp(NOW.replace(hour=10, minute=m), 2.0) for m in range(60)
    )
    series = build_observed_grid_series(
        GridImportPowerHistory(samples=import_samples), _empty_export(),
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    assert abs(slot.imported_kwh - 2.0) < 1e-6
    assert slot.exported_kwh == 0.0


def test_pure_export_slot_averages_to_exported_kwh():
    """A slot with steady 3 kW export for one hour → 3 kWh exported."""
    now = NOW.replace(hour=12)
    export_samples = tuple(
        _exp(NOW.replace(hour=10, minute=m), 3.0) for m in range(60)
    )
    series = build_observed_grid_series(
        _empty_import(), GridExportPowerHistory(samples=export_samples),
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    assert slot.imported_kwh == 0.0
    assert abs(slot.exported_kwh - 3.0) < 1e-6


# ---------------------------------------------------------------------------
# Mixed slot — both streams populated
# ---------------------------------------------------------------------------

def test_mixed_slot_with_both_streams_preserves_gross_flows():
    """A slot where the meter chain reported both import and export samples.

    Each direction averages its OWN samples — no sign-split needed since
    the streams are already direction-pure.
    """
    now = NOW.replace(hour=12)
    base = NOW.replace(hour=10)
    # 30 import samples at 2 kW (first half-hour) + 30 export samples at 2 kW
    # (second half-hour). Each side's stream is independent: import averages
    # 2 kW over 30 readings, export averages 2 kW over 30 readings.
    import_samples = tuple(
        _imp(base + timedelta(minutes=m), 2.0) for m in range(30)
    )
    export_samples = tuple(
        _exp(base + timedelta(minutes=m), 2.0) for m in range(30, 60)
    )
    series = build_observed_grid_series(
        GridImportPowerHistory(samples=import_samples),
        GridExportPowerHistory(samples=export_samples),
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    # Each side: mean = 2 kW over its 30 samples × 1 h = 2.0 kWh
    assert abs(slot.imported_kwh - 2.0) < 1e-6
    assert abs(slot.exported_kwh - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# Partial slot at "now"
# ---------------------------------------------------------------------------

def test_partial_slot_is_clamped_at_now():
    """A slot only half-elapsed by `now` should report half of the kWh."""
    now = NOW.replace(hour=10, minute=30)
    import_samples = tuple(
        _imp(NOW.replace(hour=10, minute=m), 4.0) for m in range(30)
    )
    series = build_observed_grid_series(
        GridImportPowerHistory(samples=import_samples), _empty_export(),
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    slot = by_hour[(TODAY, 10)]
    # 4 kW × 0.5h = 2 kWh
    assert abs(slot.imported_kwh - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# Yesterday window inclusion
# ---------------------------------------------------------------------------

def test_yesterday_slots_present_in_series():
    """Yesterday's samples produce raw slots in the series (pre-bake-in)."""
    yest_h10 = (NOW - timedelta(days=1)).replace(hour=10)
    now = NOW.replace(hour=2)
    import_samples = tuple(
        _imp(yest_h10 + timedelta(minutes=m), 1.0) for m in range(60)
    )
    series = build_observed_grid_series(
        GridImportPowerHistory(samples=import_samples), _empty_export(),
        _hourly_72h_price_series().slots, now=now,
    )
    by_hour = {(s.start.date(), s.start.hour): s for s in series.slots}
    assert abs(by_hour[(YESTERDAY, 10)].imported_kwh - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Series-level totals
# ---------------------------------------------------------------------------

def test_series_totals_match_per_slot_sums():
    """Series totals equal the sum of per-slot values for each side."""
    now = NOW.replace(hour=12)
    yest_h10 = (NOW - timedelta(days=1)).replace(hour=10)
    import_samples = tuple(
        _imp(yest_h10 + timedelta(minutes=m), 2.0) for m in range(60)
    )    # yesterday: 1 h pure import @ 2 kW = 2 kWh imp
    export_samples = tuple(
        _exp(NOW.replace(hour=10, minute=m), 1.5) for m in range(60)
    )    # today: 1 h pure export @ 1.5 kW = 1.5 kWh exp
    series = build_observed_grid_series(
        GridImportPowerHistory(samples=import_samples),
        GridExportPowerHistory(samples=export_samples),
        _hourly_72h_price_series().slots, now=now,
    )
    assert abs(series.total_yesterday_imported_kwh - 2.0) < 1e-4
    assert series.total_yesterday_exported_kwh == 0.0
    assert series.total_today_imported_kwh == 0.0
    assert abs(series.total_today_exported_kwh - 1.5) < 1e-4
