"""Tests for inbound/observer/derived.py — pure Python, no HA required.

Covers the formula correctness for both derived sides (consumption + losses),
the cycle-composition helper that drops partial samples, and the slot-builder
end-to-end with a realistic mix of import/export/grid-down samples.
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.sun_sale.contract.models import (
    AcPortPowerReading,
    BackupPowerReading,
    BatteryReading,
    DerivedPowerHistory,
    DerivedPowerSample,
    PriceEntry,
    PriceSeries,
    PvPowerReading,
)
from custom_components.sun_sale.inbound.observer.derived import (
    CONSUMPTION_SIDE_ID,
    LOSSES_SIDE_ID,
    _consumption_extract,
    _losses_extract,
    build_derived_engine,
    build_derived_power_sample,
    build_observed_consumption_series,
    build_observed_losses_series,
)
from custom_components.sun_sale.inbound.pricing import build_price_series
from tests.conftest import BASE_DT, default_tariff_config, make_price


NOW = BASE_DT
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


def _sample(
    t: datetime,
    *,
    ac: float = 0.0,
    bu: float = 0.0,
    gn: float = 0.0,
    sol: float = 0.0,
    bat: float = 0.0,
) -> DerivedPowerSample:
    """Build a DerivedPowerSample with named-default fields, all in kW."""
    return DerivedPowerSample(
        timestamp=t,
        ac_port_kw_signed=ac,
        backup_kw=bu,
        grid_net_kw_signed=gn,
        solar_kw=sol,
        battery_kw_signed=bat,
    )


def _hourly_72h_price_series() -> PriceSeries:
    """72h hourly grid covering yesterday → tomorrow (UTC)."""
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


# ---------------------------------------------------------------------------
# Formula correctness — per-sample extracts in the codebase sign convention
# ---------------------------------------------------------------------------


def test_consumption_solar_excess_exporting():
    """Solar producing, exporting 2 kW → home consumes the in-home share.

    AC_port=+5 (out to grid), backup=0, grid_net=-2 (sunSale: export → −).
    Expected: 5 + 0 + (−2) = 3 kW home consumption.
    """
    s = _sample(NOW, ac=5.0, bu=0.0, gn=-2.0)
    assert _consumption_extract(s) == pytest.approx(3.0)


def test_consumption_grid_import_home_load():
    """No solar, home pulls 4 kW from grid → 4 kW consumption.

    AC_port=0, backup=0, grid_net=+4 (import).
    """
    s = _sample(NOW, ac=0.0, bu=0.0, gn=4.0)
    assert _consumption_extract(s) == pytest.approx(4.0)


def test_consumption_backup_supplies_during_outage():
    """Grid down, backup powers home with 2 kW → 2 kW consumption.

    AC_port=0, backup=2, grid_net=0.
    """
    s = _sample(NOW, ac=0.0, bu=2.0, gn=0.0)
    assert _consumption_extract(s) == pytest.approx(2.0)


def test_consumption_clamps_at_zero_for_signed_inconsistency():
    """Cross-cycle inconsistency that would yield a negative is clamped."""
    s = _sample(NOW, ac=1.0, bu=0.0, gn=-3.0)
    assert _consumption_extract(s) == pytest.approx(0.0)


def test_losses_solar_charging_battery_no_export():
    """Solar 5, charging at 2, AC out 3 (no loss assumed) → losses 0.

    losses = solar − battery − ac − backup = 5 − 2 − 3 − 0 = 0.
    """
    s = _sample(NOW, sol=5.0, bat=2.0, ac=3.0, bu=0.0)
    assert _losses_extract(s) == pytest.approx(0.0)


def test_losses_discharging_to_home_no_solar():
    """Battery discharging 2 to home, AC out 2 → losses 0.

    losses = solar − battery − ac − backup = 0 − (−2) − 2 − 0 = 0.
    """
    s = _sample(NOW, sol=0.0, bat=-2.0, ac=2.0, bu=0.0)
    assert _losses_extract(s) == pytest.approx(0.0)


def test_losses_with_real_inverter_efficiency():
    """Solar 5, charging 2, AC out only 2.9 (~3.3% loss in conversion) → 0.1.

    Sanity-checks that physically realistic numbers produce a small positive
    loss, not a sign-error spike.
    """
    s = _sample(NOW, sol=5.0, bat=2.0, ac=2.9, bu=0.0)
    assert _losses_extract(s) == pytest.approx(0.1)


def test_losses_clamps_at_zero_for_signed_inconsistency():
    """A spurious negative result (sensor lag) is clamped to 0."""
    s = _sample(NOW, sol=0.0, bat=0.0, ac=2.0, bu=0.0)
    assert _losses_extract(s) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Sample composition — drop partial cycles
# ---------------------------------------------------------------------------


def _ac(t: datetime, kw: float) -> AcPortPowerReading:
    return AcPortPowerReading(power_kw=kw, timestamp=t)


def _backup(t: datetime, kw: float) -> BackupPowerReading:
    return BackupPowerReading(power_kw=kw, timestamp=t)


def _battery(power: float, grid: float) -> BatteryReading:
    return BatteryReading(
        soc=0.5, power_kw=power, grid_power_kw=grid, household_load_kw=0.2,
    )


def _pv(t: datetime, w: float) -> PvPowerReading:
    return PvPowerReading(power_w=w, timestamp=t)


def test_build_sample_drops_when_any_input_missing():
    """A cycle missing the AC-port reading produces None — not a half-built sample."""
    assert build_derived_power_sample(
        now=NOW, ac_port=None, backup=_backup(NOW, 0.0),
        battery=_battery(0.0, 0.0), pv=_pv(NOW, 0.0),
    ) is None
    assert build_derived_power_sample(
        now=NOW, ac_port=_ac(NOW, 0.0), backup=None,
        battery=_battery(0.0, 0.0), pv=_pv(NOW, 0.0),
    ) is None
    assert build_derived_power_sample(
        now=NOW, ac_port=_ac(NOW, 0.0), backup=_backup(NOW, 0.0),
        battery=None, pv=_pv(NOW, 0.0),
    ) is None
    assert build_derived_power_sample(
        now=NOW, ac_port=_ac(NOW, 0.0), backup=_backup(NOW, 0.0),
        battery=_battery(0.0, 0.0), pv=None,
    ) is None


def test_build_sample_assembles_full_cycle():
    """All inputs present → sample with the cycle's `now` timestamp + fields."""
    s = build_derived_power_sample(
        now=NOW,
        ac_port=_ac(NOW - timedelta(seconds=1), 3.5),   # source ts ignored
        backup=_backup(NOW - timedelta(seconds=1), 0.1),
        battery=_battery(power=1.2, grid=-0.8),
        pv=_pv(NOW - timedelta(seconds=1), 5_000.0),     # 5 kW in watts
    )
    assert s is not None
    assert s.timestamp == NOW
    assert s.ac_port_kw_signed == pytest.approx(3.5)
    assert s.backup_kw == pytest.approx(0.1)
    assert s.grid_net_kw_signed == pytest.approx(-0.8)
    assert s.solar_kw == pytest.approx(5.0)
    assert s.battery_kw_signed == pytest.approx(1.2)


def test_build_sample_clamps_negative_backup_to_zero():
    """A small negative backup reading (sensor bias near zero) is clamped to 0."""
    s = build_derived_power_sample(
        now=NOW,
        ac_port=_ac(NOW, 0.0),
        backup=_backup(NOW, -0.05),
        battery=_battery(0.0, 0.0),
        pv=_pv(NOW, 0.0),
    )
    assert s is not None
    assert s.backup_kw == pytest.approx(0.0)


def test_build_sample_clamps_negative_pv_to_zero():
    """Likewise a small negative PV reading clamps to 0."""
    s = build_derived_power_sample(
        now=NOW,
        ac_port=_ac(NOW, 0.0),
        backup=_backup(NOW, 0.0),
        battery=_battery(0.0, 0.0),
        pv=_pv(NOW, -10.0),
    )
    assert s is not None
    assert s.solar_kw == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Engine integration — slot building with the two sides
# ---------------------------------------------------------------------------


def test_engine_returns_both_sides_with_independent_extracts():
    """A single sample stream yields different values per side."""
    engine = build_derived_engine(local_tz=timezone.utc)
    # Two samples in hour 10: solar 4, charging 1, AC out 2.8, no grid, no backup.
    # consumption = 0 + 2.8 + 0 = 2.8
    # losses      = 4 − 1 − 2.8 − 0 = 0.2
    history = DerivedPowerHistory(samples=(
        _sample(NOW.replace(hour=10, minute=15),
                sol=4.0, bat=1.0, ac=2.8, gn=0.0, bu=0.0),
        _sample(NOW.replace(hour=10, minute=45),
                sol=4.0, bat=1.0, ac=2.8, gn=0.0, bu=0.0),
    ))
    slots = engine.build_slots_for_window(
        samples_by_side={
            CONSUMPTION_SIDE_ID: history.samples,
            LOSSES_SIDE_ID: history.samples,
        },
        price_slots=_hourly_72h_price_series().slots,
        window_start=NOW,
        window_end=NOW + timedelta(hours=12),
    )
    hour10_consumption = next(
        s for s in slots[CONSUMPTION_SIDE_ID] if s.start == NOW.replace(hour=10)
    )
    hour10_losses = next(
        s for s in slots[LOSSES_SIDE_ID] if s.start == NOW.replace(hour=10)
    )
    assert hour10_consumption.kwh == pytest.approx(2.8)
    assert hour10_losses.kwh == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Series builders — end-to-end
# ---------------------------------------------------------------------------


def test_empty_history_yields_empty_consumption_series():
    s = build_observed_consumption_series(
        DerivedPowerHistory(samples=()),
        _hourly_72h_price_series().slots,
        now=NOW,
    )
    assert s.slots == ()
    assert s.total_yesterday_kwh == 0.0
    assert s.total_today_so_far_kwh == 0.0


def test_empty_history_yields_empty_losses_series():
    s = build_observed_losses_series(
        DerivedPowerHistory(samples=()),
        _hourly_72h_price_series().slots,
        now=NOW,
    )
    assert s.slots == ()


def test_consumption_series_aggregates_to_declared_total():
    """Build a steady 3 kW consumption sample stream → 3 kWh per hour slot."""
    now = NOW.replace(hour=3)
    samples = tuple(
        _sample(NOW.replace(hour=h, minute=30), ac=3.0)
        for h in range(now.hour)
    )
    series = build_observed_consumption_series(
        DerivedPowerHistory(samples=samples),
        _hourly_72h_price_series().slots,
        now=now,
    )
    today_slots = [
        s for s in series.slots
        if NOW <= s.start < now
    ]
    assert len(today_slots) == now.hour
    for s in today_slots:
        assert s.consumed_kwh == pytest.approx(3.0)
    assert series.total_today_so_far_kwh == pytest.approx(3.0 * now.hour, abs=1e-3)


def test_losses_series_aggregates_to_declared_total():
    """Steady 0.2 kW losses → 0.2 kWh per hour-slot, total today matches."""
    now = NOW.replace(hour=4)
    samples = tuple(
        _sample(NOW.replace(hour=h, minute=30),
                sol=4.0, bat=1.0, ac=2.8)
        for h in range(now.hour)
    )
    series = build_observed_losses_series(
        DerivedPowerHistory(samples=samples),
        _hourly_72h_price_series().slots,
        now=now,
    )
    today_slots = [s for s in series.slots if NOW <= s.start < now]
    assert len(today_slots) == now.hour
    for s in today_slots:
        assert s.losses_kwh == pytest.approx(0.2, abs=1e-3)
    assert series.total_today_so_far_kwh == pytest.approx(0.2 * now.hour, abs=1e-3)


def test_slots_include_yesterday_window():
    """Yesterday samples appear before today's slots in the assembled series."""
    yest_noon = (NOW - timedelta(days=1)).replace(hour=12, minute=30)
    today_noon = NOW.replace(hour=12, minute=30)
    samples = (
        _sample(yest_noon, ac=2.0),
        _sample(today_noon, ac=1.5),
    )
    series = build_observed_consumption_series(
        DerivedPowerHistory(samples=samples),
        _hourly_72h_price_series().slots,
        now=NOW.replace(hour=13),
    )
    yest_slot = next(
        s for s in series.slots if s.start == (NOW - timedelta(days=1)).replace(hour=12)
    )
    today_slot = next(s for s in series.slots if s.start == NOW.replace(hour=12))
    assert yest_slot.consumed_kwh == pytest.approx(2.0)
    assert today_slot.consumed_kwh == pytest.approx(1.5)
    assert series.total_yesterday_kwh == pytest.approx(2.0, abs=1e-3)
