"""Tests for pipeline/monthly_bill.py — pure Python."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:    # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from custom_components.sun_sale.contract.models import (
    GridExportPowerHistory,
    GridImportPowerHistory,
    GridImportPowerReading,
    MonthlyBillState,
    ObservedGridSeries,
    ObservedGridSlot,
    PriceSeries,
    PriceSlot,
)
from custom_components.sun_sale.inbound.observer.grid import build_observed_grid_series
from custom_components.sun_sale.pipeline.monthly_bill import (
    build_monthly_bill_result,
    compute_bill_slots,
)

LOCAL_TZ = ZoneInfo("Europe/Vilnius")


def _slot(start_utc: datetime, end_utc: datetime, buy: float = 0.20, sell: float = 0.15) -> PriceSlot:
    return PriceSlot(
        start=start_utc,
        end=end_utc,
        buy_eur_kwh=buy,
        sell_eur_kwh=sell,
        spot_eur_kwh=buy,
        sources=("nordpool",),
    )


def _price_series(slots: list[PriceSlot]) -> PriceSeries:
    return PriceSeries(
        slots=tuple(slots),
        resolution=timedelta(minutes=15),
        computed_at=slots[0].start if slots else datetime(2026, 5, 30, tzinfo=UTC),
    )


def _samples_every_minute(
    start: datetime, count: int, power_kw: float,
) -> list[GridImportPowerReading]:
    """Build per-minute import-power readings. Positive power → import side."""
    return [
        GridImportPowerReading(
            power_kw=max(0.0, power_kw),
            timestamp=start + timedelta(minutes=i),
        )
        for i in range(count)
    ]


def _grid_series_from_samples(
    samples: list[GridImportPowerReading], price_series: PriceSeries, now: datetime,
) -> ObservedGridSeries:
    """Wrap raw import-power samples into an ObservedGridSeries via the real builder.

    All current monthly_bill tests use positive (import) flow only, so the
    export history is always empty here. Tests for mixed flows would
    populate both histories.
    """
    return build_observed_grid_series(
        GridImportPowerHistory(samples=tuple(samples)),
        GridExportPowerHistory(samples=()),
        price_series.slots,
        now=now,
        local_tz=LOCAL_TZ,
    )


def _empty_grid_series(now: datetime) -> ObservedGridSeries:
    """Return an empty ObservedGridSeries usable for no-samples tests."""
    return ObservedGridSeries(slots=(), computed_at=now)


# ---------------------------------------------------------------------------
# compute_bill_slots
# ---------------------------------------------------------------------------


def test_compute_bill_slots_emits_dense_zero_slots_when_no_samples():
    """Every overlapping price slot must appear, even with no samples."""
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=UTC)
    slots = [_slot(t0 + timedelta(minutes=15 * i), t0 + timedelta(minutes=15 * (i + 1))) for i in range(4)]
    ps = _price_series(slots)
    grid = _empty_grid_series(t0)

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(hours=1))

    assert len(out) == 4
    assert all(s.imported_kwh == 0.0 and s.exported_kwh == 0.0 for s in out)
    assert all(s.net_cost_eur == 0.0 for s in out)


def test_compute_bill_slots_imports_priced_with_buy():
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=UTC)
    slot = _slot(t0, t0 + timedelta(minutes=15), buy=0.25, sell=0.10)
    ps = _price_series([slot])
    grid = ObservedGridSeries(
        slots=(ObservedGridSlot(
            start=t0, end=t0 + timedelta(minutes=15),
            imported_kwh=1.0, exported_kwh=0.0, source="inverter",
        ),),
        computed_at=t0 + timedelta(minutes=15),
    )

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(minutes=15))

    assert len(out) == 1
    assert out[0].imported_kwh == 1.0    # 4 kW * 0.25h
    assert out[0].exported_kwh == 0.0
    assert out[0].net_cost_eur == 0.25


def test_compute_bill_slots_exports_priced_with_sell_even_when_negative():
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=UTC)
    slot = _slot(t0, t0 + timedelta(minutes=15), buy=0.25, sell=-0.05)
    ps = _price_series([slot])
    grid = ObservedGridSeries(
        slots=(ObservedGridSlot(
            start=t0, end=t0 + timedelta(minutes=15),
            imported_kwh=0.0, exported_kwh=1.0, source="inverter",
        ),),
        computed_at=t0 + timedelta(minutes=15),
    )

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(minutes=15))

    assert out[0].imported_kwh == 0.0
    assert out[0].exported_kwh == 1.0
    assert out[0].net_cost_eur == 0.05    # exporting on a negative sell costs us


# ---------------------------------------------------------------------------
# build_monthly_bill_result
# ---------------------------------------------------------------------------


def _build_pricing_for_window(
    start_utc: datetime, end_utc: datetime, buy: float = 0.20, sell: float = 0.15,
) -> PriceSeries:
    slots = []
    cursor = start_utc
    while cursor < end_utc:
        slots.append(_slot(cursor, cursor + timedelta(minutes=15), buy=buy, sell=sell))
        cursor += timedelta(minutes=15)
    return _price_series(slots)


def _realistic_pricing(now: datetime, buy: float = 0.20, sell: float = 0.15) -> PriceSeries:
    """Build a price series spanning the realistic window [yesterday 00:00 → now+1d).

    Mirrors production: Nordpool only publishes from yesterday-local-midnight
    onward, so completed days older than yesterday are *not* priceable. Tests
    that relied on a 3-day-deep price series were masking the carry bug.
    """
    local_now = now.astimezone(LOCAL_TZ)
    yday_local = local_now.date() - timedelta(days=1)
    start = datetime(yday_local.year, yday_local.month, yday_local.day, tzinfo=LOCAL_TZ).astimezone(UTC)
    end = now + timedelta(days=1)
    return _build_pricing_for_window(start, end, buy=buy, sell=sell)


def _local_midnight_utc(d, tz=LOCAL_TZ) -> datetime:
    """Return the UTC instant of local midnight on date ``d``."""
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)


def test_first_run_with_empty_state_zeroes_carry_and_previous_month():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    ps = _realistic_pricing(now)

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now),
        price_series=ps,
        stored_state=None,
        local_tz=LOCAL_TZ,
        now=now,
    )

    assert result.carry_eur == 0.0
    assert result.previous_month_eur == 0.0
    assert result.previous_month_str == ""
    assert result.month_str == "2026-05"
    assert result.total_month_eur == 0.0
    # Slots cover the live window from yday-start LOCAL to now.
    assert len(result.slots) > 0


def test_carry_accumulates_from_priceable_ledger_days():
    """Carry sums prior-day ledger entries; yesterday is re-priced this cycle.

    Stored ledger holds 2026-05-28 (a completed day before yesterday). Yesterday
    (2026-05-29) is still inside the price window, so it is re-priced from the
    grid samples — but it belongs to the *live* window, so it lands in
    yday_to_now, not carry.
    """
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)  # local 15:00, today=05-30
    stored = MonthlyBillState(finalized_days=(("2026-05-28", 3.0),))
    ps = _realistic_pricing(now, buy=0.10, sell=0.05)
    # Constant 1 kW import across yesterday (05-29) + today-so-far.
    yday_start = _local_midnight_utc(date(2026, 5, 29))
    samples = _samples_every_minute(yday_start, count=39 * 60, power_kw=1.0)
    grid = _grid_series_from_samples(samples, ps, now)

    result = build_monthly_bill_result(
        grid_series=grid, price_series=ps, stored_state=stored,
        local_tz=LOCAL_TZ, now=now,
    )

    # Carry = the frozen 05-28 entry only (05-29 is in the live window).
    assert result.carry_eur == 3.0
    # Yesterday (24 kWh @ 0.10 = 2.40) + today-so-far (15 kWh @ 0.10 = 1.50).
    assert abs(result.yday_to_now_eur - 3.90) < 1e-6
    assert abs(result.total_month_eur - (3.0 + 3.90)) < 1e-6
    # Yesterday was (re)written into the ledger this cycle at its full-day cost.
    ledger = dict(result.updated_state.finalized_days)
    assert abs(ledger["2026-05-29"] - 2.40) < 1e-6
    assert ledger["2026-05-28"] == 3.0


def test_unpriceable_older_ledger_days_are_frozen_not_zeroed():
    """A stored entry for a day outside the price window keeps its value.

    This is the core of the fix: the old design re-priced the day-before-
    yesterday against a price series that no longer covered it, getting 0.
    Now that day is never recomputed — its ledger value is preserved.
    """
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    # 05-28 is the day-before-yesterday — outside the realistic price window.
    stored = MonthlyBillState(finalized_days=(("2026-05-28", -1.25),))
    ps = _realistic_pricing(now)

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now), price_series=ps,
        stored_state=stored, local_tz=LOCAL_TZ, now=now,
    )

    ledger = dict(result.updated_state.finalized_days)
    assert ledger["2026-05-28"] == -1.25          # preserved, not zeroed
    assert result.carry_eur == -1.25              # and it counts toward carry


def test_previous_month_total_summed_from_ledger():
    """On the 1st, previous_month_eur sums the prior month's ledger entries."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # local 15:00 on the 1st
    stored = MonthlyBillState(finalized_days=(
        ("2026-05-29", 2.0),
        ("2026-05-30", 3.0),
    ))
    ps = _realistic_pricing(now, buy=0.20, sell=0.10)
    # Yesterday is 2026-05-31 (prev month's last day) — re-priced this cycle.
    yday_start = _local_midnight_utc(date(2026, 5, 31))
    samples = _samples_every_minute(yday_start, count=24 * 60, power_kw=0.5)
    grid = _grid_series_from_samples(samples, ps, now)

    result = build_monthly_bill_result(
        grid_series=grid, price_series=ps, stored_state=stored,
        local_tz=LOCAL_TZ, now=now,
    )

    assert result.month_str == "2026-06"
    assert result.previous_month_str == "2026-05"
    # 2.0 + 3.0 + (24h*0.5kW = 12 kWh @ 0.20 = 2.40) for 05-31.
    assert abs(result.previous_month_eur - (2.0 + 3.0 + 2.40)) < 1e-6
    # Carry holds no current-month (June) completed days yet on the 1st.
    assert result.carry_eur == 0.0


def test_ledger_prunes_entries_older_than_previous_month():
    """Entries older than the previous calendar month are dropped from state."""
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    stored = MonthlyBillState(finalized_days=(
        ("2026-04-30", 9.0),    # two months back — should be pruned
        ("2026-05-31", 4.0),    # previous month — kept
        ("2026-06-10", 1.0),    # current month — kept
    ))
    ps = _realistic_pricing(now)

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now), price_series=ps,
        stored_state=stored, local_tz=LOCAL_TZ, now=now,
    )

    months = {ds[:7] for ds, _ in result.updated_state.finalized_days}
    assert "2026-04" not in months
    assert result.previous_month_eur == 4.0


def test_live_slots_never_cross_into_previous_month():
    """On day 1 of a new month, live slots cover only the new month."""
    now = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
    ps = _realistic_pricing(now)
    stored = MonthlyBillState(finalized_days=())

    result = build_monthly_bill_result(
        grid_series=_empty_grid_series(now),
        price_series=ps,
        stored_state=stored,
        local_tz=LOCAL_TZ,
        now=now,
    )

    month_start_utc = datetime(2026, 5, 31, 21, 0, tzinfo=UTC)
    assert all(s.start >= month_start_utc for s in result.slots)


# ---------------------------------------------------------------------------
# Daily breakdowns (today_cost_eur / yesterday_cost_eur)
# ---------------------------------------------------------------------------


def test_daily_costs_split_today_and_yesterday():
    """today_cost_eur / yesterday_cost_eur partition the live window on a normal day."""
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)  # local 15:00 (UTC+3)
    # Local midnights: yesterday(05-29)=05-28 21:00Z, today(05-30)=05-29 21:00Z.
    yday_start = datetime(2026, 5, 28, 21, 0, tzinfo=UTC)
    ps = _build_pricing_for_window(yday_start, now, buy=0.10, sell=0.05)
    # Constant 1 kW import across yesterday + today-so-far (39 h to `now`).
    samples = _samples_every_minute(yday_start, count=39 * 60, power_kw=1.0)
    grid = _grid_series_from_samples(samples, ps, now)
    stored = MonthlyBillState(finalized_days=())

    result = build_monthly_bill_result(
        grid_series=grid, price_series=ps, stored_state=stored,
        local_tz=LOCAL_TZ, now=now,
    )

    # Yesterday: 24 h * 1 kW = 24 kWh @ 0.10 = 2.40 EUR.
    assert abs(result.yesterday_cost_eur - 2.40) < 1e-6
    # Today so far: 15 h (00:00→15:00 local) * 1 kW = 15 kWh @ 0.10 = 1.50 EUR.
    assert abs(result.today_cost_eur - 1.50) < 1e-6
    # On a normal day the two legs reconstruct the live-window total.
    assert abs((result.today_cost_eur + result.yesterday_cost_eur)
               - result.yday_to_now_eur) < 1e-6


def test_yesterday_cost_survives_month_boundary():
    """On the 1st, yesterday_cost_eur still reports the previous month's last day.

    The live window covers only the new month (today), so yesterday's cost is
    outside it — yet the independent [yday_start, today_start) computation must
    still surface it.
    """
    now = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # local 09:00 on the 1st
    # yesterday(05-31) = 05-30 21:00Z → 05-31 21:00Z; today(06-01) starts 05-31 21:00Z.
    yday_start = datetime(2026, 5, 30, 21, 0, tzinfo=UTC)
    today_start = datetime(2026, 5, 31, 21, 0, tzinfo=UTC)
    ps = _build_pricing_for_window(yday_start, now, buy=0.10, sell=0.05)
    # Import only during yesterday (the previous month's last day).
    samples = _samples_every_minute(yday_start, count=24 * 60, power_kw=1.0)
    grid = _grid_series_from_samples(samples, ps, now)
    stored = MonthlyBillState(finalized_days=())

    result = build_monthly_bill_result(
        grid_series=grid, price_series=ps, stored_state=stored,
        local_tz=LOCAL_TZ, now=now,
    )

    assert abs(result.yesterday_cost_eur - 2.40) < 1e-6
    assert abs(result.today_cost_eur - 0.0) < 1e-6
    # Live slots stay within the new month (today only), never touching yesterday.
    assert all(s.start >= today_start for s in result.slots)


# ---------------------------------------------------------------------------
# Unpriced slots — a slot with no known market price must not be billed
# ---------------------------------------------------------------------------

def test_compute_bill_slots_skips_unpriced_slots():
    """A placeholder slot's filler zero must not be charged as a real price."""
    t0 = datetime(2026, 5, 29, 0, 0, tzinfo=UTC)
    priced = _slot(t0, t0 + timedelta(minutes=15), buy=0.25, sell=0.10)
    placeholder = PriceSlot(
        start=t0 + timedelta(minutes=15), end=t0 + timedelta(minutes=30),
        buy_eur_kwh=0.19, sell_eur_kwh=-0.02, spot_eur_kwh=0.0,
        sources=("nordpool", "tariff", "unpriced"), priced=False,
    )
    ps = _price_series([priced, placeholder])
    grid = ObservedGridSeries(
        slots=(
            ObservedGridSlot(
                start=t0, end=t0 + timedelta(minutes=15),
                imported_kwh=1.0, exported_kwh=0.0, source="inverter",
            ),
            ObservedGridSlot(
                start=t0 + timedelta(minutes=15), end=t0 + timedelta(minutes=30),
                imported_kwh=1.0, exported_kwh=0.0, source="inverter",
            ),
        ),
        computed_at=t0,
    )

    out = compute_bill_slots(grid, ps, t0, t0 + timedelta(minutes=30))

    assert len(out) == 1
    assert out[0].start == t0
    assert abs(out[0].net_cost_eur - 0.25) < 1e-9
