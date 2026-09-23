"""Tests for sensor.py — native_value properties for all sensor entities."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.sun_sale.contract.models import (
    BillSlot,
    MonthlyBillResult,
    MonthlyBillState,
    Schedule,
    ScheduleSlot,
    StorageMode,
    TariffResult,
)
from custom_components.sun_sale.sensor import (
    CurrentActionSensor,
    CurrentBuyPriceSensor,
    CurrentSellPriceSensor,
    DailyCostSensor,
    DegradationCostSensor,
    EstimatedCapacitySensor,
    ExpectedProfitSensor,
    NextActionSensor,
    YesterdayCostSensor,
)

BASE = datetime(2024, 1, 15, 0, 0, tzinfo=UTC)


def make_slot(hour: int, mode: StorageMode = StorageMode.SelfUse, profit: float = 0.0) -> ScheduleSlot:
    start = BASE.replace(hour=hour)
    return ScheduleSlot(
        start=start,
        end=start + timedelta(hours=1),
        mode=mode,
        power_kw=3.0,
        expected_soc_after=0.5,
        expected_profit_eur=profit,
        reason="test",
    )


def make_schedule(slots: list[ScheduleSlot]) -> Schedule:
    return Schedule(
        slots=slots,
        total_expected_profit_eur=sum(s.expected_profit_eur for s in slots),
        degradation_cost_per_kwh=0.02,
        computed_at=BASE,
    )


def make_tariff(hour: int, buy: float = 0.12, sell: float = 0.06) -> TariffResult:
    return TariffResult(hour=BASE.replace(hour=hour), spot_price=0.08, buy_price=buy, sell_price=sell)


def _make_price_series_for_hour(hour: int, buy_eur_kwh: float = 0.12, sell_eur_kwh: float = 0.06):
    """Create a minimal PriceSeries with one slot matching the given buy/sell prices."""
    from datetime import timedelta

    from custom_components.sun_sale.contract.models import PriceSeries, PriceSlot
    start = BASE.replace(hour=hour)
    slot = PriceSlot(
        start=start, end=start + timedelta(hours=1),
        buy_eur_kwh=buy_eur_kwh, sell_eur_kwh=sell_eur_kwh, spot_eur_kwh=0.08,
        sources=("nordpool", "tariff"),
    )
    return PriceSeries(slots=(slot,), resolution=timedelta(hours=1), computed_at=BASE)


def make_coord(data: dict | None = None) -> MagicMock:
    coord = MagicMock()
    coord.data = data
    return coord


def make_entry(entry_id: str = "entry1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


# ---------------------------------------------------------------------------
# CurrentActionSensor
# ---------------------------------------------------------------------------

def test_current_action_idle_when_no_data():
    sensor = CurrentActionSensor(make_coord(None), make_entry())
    assert sensor.native_value == StorageMode.SelfUse.value


def test_current_action_idle_when_empty_schedule():
    schedule = make_schedule([])
    sensor = CurrentActionSensor(make_coord({"schedule": schedule}), make_entry())
    assert sensor.native_value == StorageMode.SelfUse.value


def test_current_action_returns_first_slot_mode_as_fallback():
    slots = [make_slot(0, StorageMode.GridCharge), make_slot(1, StorageMode.Discharge)]
    sensor = CurrentActionSensor(make_coord({"schedule": make_schedule(slots)}), make_entry())
    assert sensor.native_value == StorageMode.GridCharge.value


# ---------------------------------------------------------------------------
# NextActionSensor
# ---------------------------------------------------------------------------

def test_next_action_idle_when_no_data():
    sensor = NextActionSensor(make_coord(None), make_entry())
    assert sensor.native_value == StorageMode.SelfUse.value


def test_next_action_returns_current_when_no_change():
    slots = [make_slot(0, StorageMode.SelfUse), make_slot(1, StorageMode.SelfUse)]
    sensor = NextActionSensor(make_coord({"schedule": make_schedule(slots)}), make_entry())
    assert sensor.native_value == StorageMode.SelfUse.value


# ---------------------------------------------------------------------------
# ExpectedProfitSensor
# ---------------------------------------------------------------------------

def test_expected_profit_zero_when_no_data():
    sensor = ExpectedProfitSensor(make_coord(None), make_entry())
    assert sensor.native_value == 0.0


def test_expected_profit_sums_today_slots():
    today = datetime.now(UTC).date()
    start1 = datetime(today.year, today.month, today.day, 10, tzinfo=UTC)
    start2 = datetime(today.year, today.month, today.day, 11, tzinfo=UTC)
    slot1 = ScheduleSlot(start=start1, end=start1 + timedelta(hours=1),
                         mode=StorageMode.SelfUse, power_kw=0, expected_soc_after=0.5,
                         expected_profit_eur=0.10, reason="")
    slot2 = ScheduleSlot(start=start2, end=start2 + timedelta(hours=1),
                         mode=StorageMode.SelfUse, power_kw=0, expected_soc_after=0.5,
                         expected_profit_eur=0.20, reason="")
    schedule = make_schedule([slot1, slot2])
    sensor = ExpectedProfitSensor(make_coord({"schedule": schedule}), make_entry())
    assert abs(sensor.native_value - 0.30) < 1e-6


# ---------------------------------------------------------------------------
# DegradationCostSensor
# ---------------------------------------------------------------------------

def test_degradation_cost_none_when_no_data():
    sensor = DegradationCostSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


def test_degradation_cost_returned():
    sensor = DegradationCostSensor(make_coord({"degradation_cost": 0.041667}), make_entry())
    assert abs(sensor.native_value - 0.041667) < 1e-5


# ---------------------------------------------------------------------------
# EstimatedCapacitySensor
# ---------------------------------------------------------------------------

def test_estimated_capacity_none_when_no_data():
    sensor = EstimatedCapacitySensor(make_coord(None), make_entry())
    assert sensor.native_value is None


def test_estimated_capacity_returned():
    sensor = EstimatedCapacitySensor(make_coord({"estimated_capacity": 9.75}), make_entry())
    assert abs(sensor.native_value - 9.75) < 1e-6


def test_dashboard_battery_uses_estimated_capacity_and_provides_set():
    """Dashboard total/remaining use the learned estimate; nameplate exposed as 'set'."""
    from custom_components.sun_sale.contract.models import BatteryStatus
    from custom_components.sun_sale.sensor import DashboardSensor
    from tests.conftest import default_battery_config

    bc = default_battery_config()
    nominal = bc.nominal_capacity_kwh
    status = BatteryStatus(
        total_capacity_kwh=nominal,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        soc=0.5,
        remaining_capacity_kwh=0.5 * nominal,
    )
    coord = make_coord({
        "battery_status": status,
        "estimated_capacity": 26.0,
        "battery_power_kw": 0.0,
    })
    coord.battery_config = bc
    coord.automation_enabled = True
    entry = make_entry()
    entry.data = {}
    entry.options = {}

    attrs = DashboardSensor(coord, entry).extra_state_attributes
    assert attrs["battery_capacity_kwh"] == 26.0                 # estimated used as total
    assert attrs["battery_set_capacity_kwh"] == round(nominal, 2)  # nameplate provided
    assert attrs["battery_remaining_kwh"] == 13.0               # 0.5 × estimated (not nominal)


def test_dashboard_battery_falls_back_to_nominal_without_estimate():
    """With no learned estimate yet, total/remaining fall back to the nameplate."""
    from custom_components.sun_sale.contract.models import BatteryStatus
    from custom_components.sun_sale.sensor import DashboardSensor
    from tests.conftest import default_battery_config

    bc = default_battery_config()
    nominal = bc.nominal_capacity_kwh
    status = BatteryStatus(
        total_capacity_kwh=nominal, max_charge_power_kw=5.0, max_discharge_power_kw=5.0,
        soc=0.5, remaining_capacity_kwh=0.5 * nominal,
    )
    coord = make_coord({"battery_status": status, "battery_power_kw": 0.0})
    coord.battery_config = bc
    coord.automation_enabled = True
    entry = make_entry()
    entry.data = {}
    entry.options = {}

    attrs = DashboardSensor(coord, entry).extra_state_attributes
    assert attrs["battery_capacity_kwh"] == round(nominal, 2)
    assert attrs["battery_set_capacity_kwh"] == round(nominal, 2)
    assert attrs["battery_remaining_kwh"] == round(0.5 * nominal, 2)


def _dashboard_entry() -> MagicMock:
    """Return a config entry with empty data/options for the dashboard sensor."""
    entry = make_entry()
    entry.data = {}
    entry.options = {}
    return entry


def test_dashboard_live_flows_from_latest_derived_sample():
    """live_flows mirrors the newest derived sample and applies the home/loss formulas."""
    from custom_components.sun_sale.contract.models import (
        BatteryStatus,
        DerivedPowerHistory,
        DerivedPowerSample,
    )
    from custom_components.sun_sale.sensor import DashboardSensor
    from tests.conftest import default_battery_config

    old = DerivedPowerSample(
        timestamp=BASE, ac_port_kw_signed=0.0, backup_kw=0.0,
        grid_net_kw_signed=0.0, solar_kw=0.0, battery_kw_signed=0.0,
    )
    latest = DerivedPowerSample(
        timestamp=BASE + timedelta(minutes=5),
        ac_port_kw_signed=1.1,   # inverter → grid
        backup_kw=0.0,
        grid_net_kw_signed=-0.3,  # exporting
        solar_kw=2.4,
        battery_kw_signed=1.0,    # charging
    )
    status = BatteryStatus(
        total_capacity_kwh=26.0, max_charge_power_kw=5.0, max_discharge_power_kw=5.0,
        soc=0.85, remaining_capacity_kwh=22.1,
    )
    coord = make_coord({
        "battery_status": status,
        "battery_power_kw": 1.0,
        "derived_power_history": DerivedPowerHistory(samples=(old, latest)),
    })
    coord.battery_config = default_battery_config()
    coord.automation_enabled = True

    flows = DashboardSensor(coord, _dashboard_entry()).extra_state_attributes["live_flows"]
    assert flows["ts"] == int(latest.timestamp.timestamp() * 1000)  # newest sample wins
    assert flows["solar_kw"] == 2.4
    assert flows["battery_kw"] == 1.0      # sign preserved (+ = charging)
    assert flows["grid_kw"] == -0.3        # sign preserved (− = export)
    assert flows["home_kw"] == 0.8         # max(0, 0.0 + 1.1 − 0.3)
    assert flows["losses_kw"] == 0.3       # max(0, 2.4 − 1.0 − 1.1 − 0.0)
    assert flows["battery_soc_pct"] == 85.0


def test_dashboard_live_flows_none_without_samples():
    """No derived history (or an empty one) → live_flows is None, so the panel hides it."""
    from custom_components.sun_sale.contract.models import DerivedPowerHistory
    from custom_components.sun_sale.sensor import DashboardSensor
    from tests.conftest import default_battery_config

    for hist in (None, DerivedPowerHistory(samples=())):
        coord = make_coord({"battery_power_kw": 0.0, "derived_power_history": hist})
        coord.battery_config = default_battery_config()
        coord.automation_enabled = True
        attrs = DashboardSensor(coord, _dashboard_entry()).extra_state_attributes
        assert attrs["live_flows"] is None


# ---------------------------------------------------------------------------
# CurrentBuyPriceSensor / CurrentSellPriceSensor
# ---------------------------------------------------------------------------

def test_buy_price_none_when_no_data():
    sensor = CurrentBuyPriceSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


def test_buy_price_uses_first_slot_as_fallback():
    ps = _make_price_series_for_hour(0, buy_eur_kwh=0.15)
    sensor = CurrentBuyPriceSensor(make_coord({"pricing": ps}), make_entry())
    assert abs(sensor.native_value - 0.15) < 1e-6


def test_sell_price_uses_first_slot_as_fallback():
    ps = _make_price_series_for_hour(0, sell_eur_kwh=0.07)
    sensor = CurrentSellPriceSensor(make_coord({"pricing": ps}), make_entry())
    assert abs(sensor.native_value - 0.07) < 1e-6


def test_sell_price_none_when_no_data():
    sensor = CurrentSellPriceSensor(make_coord(None), make_entry())
    assert sensor.native_value is None


# ---------------------------------------------------------------------------
# Observed-series sensors (live / slot-sum / baked-yesterday)
# ---------------------------------------------------------------------------

from custom_components.sun_sale.contract.const import (  # noqa: E402  (section-local)
    SOURCE_KIND_DEDICATED_SENSOR,
    SOURCE_KIND_SNAPSHOT,
)
from custom_components.sun_sale.contract.models import (  # noqa: E402
    BakedDayRecord,
    BakedObservedHistory,
    ObservedGenerationSeries,
    ObservedGridSeries,
    SlotKwh,
)
from custom_components.sun_sale.sensor import (  # noqa: E402
    TodayExportedLiveSensor,
    TodayExportedSlotSumSensor,
    TodayGenerationLiveSensor,
    TodayGenerationSlotSumSensor,
    TodayImportedLiveSensor,
    TodayImportedSlotSumSensor,
    YesterdayExportedBakedSensor,
    YesterdayGenerationBakedSensor,
    YesterdayImportedBakedSensor,
)


def _coord_with_local_tz(data: dict | None = None) -> MagicMock:
    """Build a coordinator stub with both .data and the local_tz config field."""
    coord = MagicMock()
    coord.data = data
    coord._sun_sale_config.local_tz = UTC
    return coord


# --- live-counter sensors ---


def test_today_generation_live_returns_counter_kwh():
    """Reads ``today_generation_live_kwh`` straight from coordinator.data."""
    sensor = TodayGenerationLiveSensor(
        _coord_with_local_tz({"today_generation_live_kwh": 12.3456}),
        make_entry(),
    )
    assert sensor.native_value == 12.346


def test_today_generation_live_none_when_unavailable():
    """A missing live key (entity unavailable) returns ``None``."""
    sensor = TodayGenerationLiveSensor(
        _coord_with_local_tz({"today_generation_live_kwh": None}),
        make_entry(),
    )
    assert sensor.native_value is None


def test_today_imported_and_exported_live_use_separate_keys():
    """Each live sensor reads its own key independently."""
    data = {
        "today_imported_live_kwh": 4.5,
        "today_exported_live_kwh": 7.0,
    }
    imp = TodayImportedLiveSensor(_coord_with_local_tz(data), make_entry())
    exp = TodayExportedLiveSensor(_coord_with_local_tz(data), make_entry())
    assert imp.native_value == 4.5
    assert exp.native_value == 7.0


# --- slot-sum (diagnostic) sensors ---


def test_today_generation_slot_sum_reads_observed_total():
    """Slot-sum sensor reflects ``ObservedGenerationSeries.total_today_so_far_kwh``."""
    observed = ObservedGenerationSeries(
        slots=(), computed_at=BASE,
        total_yesterday_kwh=8.0, total_today_so_far_kwh=3.789,
    )
    sensor = TodayGenerationSlotSumSensor(
        _coord_with_local_tz({"observed_generation": observed}), make_entry(),
    )
    assert sensor.native_value == 3.789


def test_today_imported_and_exported_slot_sum_read_grid_totals():
    """Grid slot-sum sensors read import/export totals independently."""
    observed = ObservedGridSeries(
        slots=(), computed_at=BASE,
        total_today_imported_kwh=2.222, total_today_exported_kwh=1.111,
    )
    imp = TodayImportedSlotSumSensor(
        _coord_with_local_tz({"observed_grid": observed}), make_entry(),
    )
    exp = TodayExportedSlotSumSensor(
        _coord_with_local_tz({"observed_grid": observed}), make_entry(),
    )
    assert imp.native_value == 2.222
    assert exp.native_value == 1.111


def test_slot_sum_sensor_none_when_observed_missing():
    """Slot-sum sensors return ``None`` when their observed series is absent."""
    sensor = TodayGenerationSlotSumSensor(
        _coord_with_local_tz({}), make_entry(),
    )
    assert sensor.native_value is None


# --- yesterday baked sensors ---


def _baked_record(side_id: str, date_str: str, baked_sum: float, source_kind: str) -> BakedDayRecord:
    """Construct a BakedDayRecord with minimal slot content."""
    return BakedDayRecord(
        date_str=date_str,
        side_id=side_id,
        counter_total_used=baked_sum,
        source_kind=source_kind,
        baked_slots=(SlotKwh(BASE, BASE + timedelta(hours=1), baked_sum),),
        baked_sum=baked_sum,
        baked_at=BASE,
    )


def test_yesterday_generation_baked_returns_baked_sum():
    """Sensor exposes baked_sum for yesterday's local date."""
    yesterday_str = (
        datetime.now(UTC).astimezone(UTC).date()
        - timedelta(days=1)
    ).isoformat()
    history = BakedObservedHistory(records=(
        _baked_record("generation", yesterday_str, 9.876, SOURCE_KIND_DEDICATED_SENSOR),
    ))
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({"baked_observed_history": history}), make_entry(),
    )
    assert sensor.native_value == 9.876


def test_yesterday_baked_exposes_source_kind_attribute():
    """The ``source_kind`` provenance is surfaced as a sensor attribute."""
    yesterday_str = (
        datetime.now(UTC).astimezone(UTC).date()
        - timedelta(days=1)
    ).isoformat()
    history = BakedObservedHistory(records=(
        _baked_record("generation", yesterday_str, 5.0, SOURCE_KIND_SNAPSHOT),
    ))
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({"baked_observed_history": history}), make_entry(),
    )
    attrs = sensor.extra_state_attributes
    assert attrs["source_kind"] == SOURCE_KIND_SNAPSHOT
    assert attrs["counter_total_used"] == 5.0
    assert attrs["date"] == yesterday_str


def test_yesterday_baked_none_when_no_record_for_yesterday():
    """No matching record → ``None`` value and empty attributes."""
    history = BakedObservedHistory(records=(
        _baked_record("generation", "1999-12-31", 5.0, SOURCE_KIND_DEDICATED_SENSOR),
    ))
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({"baked_observed_history": history}), make_entry(),
    )
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_yesterday_baked_picks_correct_side():
    """A sensor for grid_import never picks up generation's record on the same date."""
    yesterday_str = (
        datetime.now(UTC).astimezone(UTC).date()
        - timedelta(days=1)
    ).isoformat()
    history = BakedObservedHistory(records=(
        _baked_record("generation",  yesterday_str, 10.0, SOURCE_KIND_DEDICATED_SENSOR),
        _baked_record("grid_import", yesterday_str, 2.0,  SOURCE_KIND_SNAPSHOT),
        _baked_record("grid_export", yesterday_str, 3.0,  SOURCE_KIND_SNAPSHOT),
    ))
    coord = _coord_with_local_tz({"baked_observed_history": history})
    assert YesterdayGenerationBakedSensor(coord, make_entry()).native_value == 10.0
    assert YesterdayImportedBakedSensor(coord, make_entry()).native_value == 2.0
    assert YesterdayExportedBakedSensor(coord, make_entry()).native_value == 3.0


def test_yesterday_baked_none_when_no_history():
    """Missing ``baked_observed_history`` key → ``None``."""
    sensor = YesterdayGenerationBakedSensor(
        _coord_with_local_tz({}), make_entry(),
    )
    assert sensor.native_value is None




# ---------------------------------------------------------------------------
# Daily-cost sensors (DailyCostSensor / YesterdayCostSensor)
# ---------------------------------------------------------------------------


def _bill_result(today_cost: float, yesterday_cost: float, slots=()) -> MonthlyBillResult:
    """Build a MonthlyBillResult carrying the given daily cost breakdowns."""
    return MonthlyBillResult(
        slots=tuple(slots),
        carry_eur=0.0,
        yday_to_now_eur=today_cost + yesterday_cost,
        total_month_eur=0.0,
        month_str="2024-01",
        previous_month_str="",
        previous_month_eur=0.0,
        updated_state=MonthlyBillState(finalized_days=()),
        computed_at=BASE,
        today_cost_eur=today_cost,
        yesterday_cost_eur=yesterday_cost,
    )


def test_daily_cost_native_value_reads_today_cost():
    """DailyCostSensor surfaces today_cost_eur; last_reset is a local midnight."""
    coord = _coord_with_local_tz({"monthly_bill": _bill_result(1.2345, 9.0)})
    sensor = DailyCostSensor(coord, make_entry())
    assert sensor.native_value == 1.2345
    reset = sensor.last_reset
    assert reset.hour == 0 and reset.minute == 0 and reset.second == 0


def test_yesterday_cost_native_value_reads_yesterday_cost():
    """YesterdayCostSensor surfaces yesterday_cost_eur with a date attribute."""
    coord = _coord_with_local_tz({"monthly_bill": _bill_result(1.0, 2.5)})
    sensor = YesterdayCostSensor(coord, make_entry())
    assert sensor.native_value == 2.5
    assert "date" in sensor.extra_state_attributes


def test_daily_cost_sensors_none_without_data():
    """Both daily-cost sensors return None when no monthly_bill is present."""
    coord = _coord_with_local_tz({})
    assert DailyCostSensor(coord, make_entry()).native_value is None
    assert YesterdayCostSensor(coord, make_entry()).native_value is None


def test_daily_cost_attributes_sum_today_flows():
    """DailyCostSensor breakdown sums import/export of slots at/after today midnight."""
    coord = _coord_with_local_tz({})
    # today_start with local_tz=UTC is today's UTC midnight.
    today_start = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    slot = BillSlot(
        start=today_start + timedelta(hours=10),
        end=today_start + timedelta(hours=10, minutes=15),
        imported_kwh=1.5, exported_kwh=0.4,
        buy_eur_kwh=0.20, sell_eur_kwh=0.10, net_cost_eur=0.26,
    )
    coord.data = {"monthly_bill": _bill_result(0.26, 0.0, slots=(slot,))}
    attrs = DailyCostSensor(coord, make_entry()).extra_state_attributes
    assert attrs["imported_kwh"] == 1.5
    assert attrs["exported_kwh"] == 0.4


def test_pricing_sensor_publishes_only_priced_slots():
    """Tomorrow's zero-spot filler must not reach the panel as a price line.

    The panel starts the dashed forecast where the solid priced line ends, so a
    filler slot there pushed the forecast off the chart entirely (live,
    2026-09-23) and would also skew the min/max stats.
    """
    from dataclasses import replace

    from custom_components.sun_sale.contract.models import PriceSeries, PriceSlot
    from custom_components.sun_sale.sensor import PricingPipelineSensor

    step = timedelta(minutes=15)
    slots = tuple(
        PriceSlot(
            start=BASE + step * i, end=BASE + step * (i + 1),
            buy_eur_kwh=0.30, sell_eur_kwh=0.10, spot_eur_kwh=0.15, sources=("test",),
        )
        for i in range(8)
    )
    filler = tuple(
        replace(s, start=s.start + step * 8, end=s.end + step * 8,
                buy_eur_kwh=0.12, sell_eur_kwh=-0.05, spot_eur_kwh=0.0, priced=False)
        for s in slots
    )
    series = PriceSeries(slots=slots + filler, resolution=step, computed_at=BASE)
    attrs = PricingPipelineSensor(make_coord({"pricing": series}), make_entry()).extra_state_attributes

    assert len(attrs["slots"]) == 8
    assert attrs["min_buy"] == 0.30
    assert attrs["negative_sell_count"] == 0
