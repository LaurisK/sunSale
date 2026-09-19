"""Tests for the pricing integration check (``tools/checks/pricing.py``).

The slots are priced by the integration's own ``build_price_series``, so a clean
run proves the check mirrors the tariff formula — seasons, day types and public
holidays included; each test then corrupts one claim and asserts the check
notices.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.sun_sale.contract.models import (
    DaySchedule,
    PriceEntry,
    PriceFormula,
    TariffConfig,
    TariffSwitch,
)
from custom_components.sun_sale.inbound.holiday_calendar import holiday_predicate
from custom_components.sun_sale.inbound.pricing import build_price_series
from tools.checks import check_pricing
from tools.checks.snapshot import Snapshot

pytest.importorskip("holidays")

_TZ = ZoneInfo("Europe/Vilnius")
# Mon 2024-12-23 → Thu 2024-12-26, local; the 25th is a Lithuanian public holiday.
_START = datetime(2024, 12, 23, tzinfo=_TZ).astimezone(UTC)
_SPOT = 0.05

_BUY = PriceFormula(
    markup=0.01, vat=0.21, grid_fees=(0.06, 0.03, 0.02, 0.01), weekends=True, holidays=True, seasons=True,
    schedules=(
        DaySchedule("winter", "workday", (TariffSwitch(0, 3), TariffSwitch(7 * 60, 1), TariffSwitch(23 * 60, 3))),
        DaySchedule("winter", "weekend", (TariffSwitch(0, 4),)),
        DaySchedule("winter", "holiday", (TariffSwitch(0, 2),)),
    ),
)
_FIXED_SELL = PriceFormula(energy_mode="fixed", fixed_price=0.08, markup=0.005, grid_fees=(0.01,))


def _payload(sell: PriceFormula = _FIXED_SELL, export: float | None = None) -> dict:
    """Return a debug payload of 96 hourly slots priced by the pipeline."""
    tariff = TariffConfig(buy=_BUY, sell=sell)
    step = timedelta(hours=1)
    entries = [
        PriceEntry(start=_START + step * i, end=_START + step * (i + 1), price_eur_kwh=_SPOT,
                   export_price_eur_kwh=export)
        for i in range(96)
    ]
    series = build_price_series(entries, tariff, now=_START, local_tz=_TZ, is_holiday=holiday_predicate("LT"))
    slots = [
        {"start": s.start.isoformat(), "spot": s.spot_eur_kwh, "buy": s.buy_eur_kwh,
         "sell": s.sell_eur_kwh, "export": s.export_eur_kwh}
        for s in series.slots
    ]
    return {
        "config": {"time_zone": "Europe/Vilnius", "holiday_country": "LT"},
        # The debug view serialises with dataclasses.asdict and ships JSON.
        "inputs": {"tariff_config": json.loads(json.dumps(dataclasses.asdict(tariff)))},
        "pipeline": {"pricing": {"slot_count": len(slots), "resolution_s": 3600, "slots": slots}},
    }


def _check(payload: dict):
    """Run the check on a payload."""
    return check_pricing(Snapshot(entry_id="e1", debug=payload))


def test_a_consistent_payload_passes():
    result = _check(_payload())
    assert not result.skipped, result.skip_reason
    assert result.overall_ok, result.mismatches
    assert len(result.slot_rows) == 96


def test_the_payload_exercises_the_workday_and_holiday_tariffs():
    slots = _check(_payload()).slot_rows
    # Mon 12:00 is the workday day tariff T1; Wed 25 Dec 12:00 the holiday tariff T2.
    assert slots[12]["act_buy"] == pytest.approx((_SPOT + 0.01 + 0.06) * 1.21)
    assert slots[60]["act_buy"] == pytest.approx((_SPOT + 0.01 + 0.03) * 1.21)


def test_flags_a_buy_price_off_the_formula():
    payload = _payload()
    payload["pipeline"]["pricing"]["slots"][12]["buy"] += 0.01
    result = _check(payload)
    assert not result.overall_ok
    assert result.mismatches == [payload["pipeline"]["pricing"]["slots"][12]["start"]]


def test_flags_a_sell_price_that_ignores_the_fixed_price():
    payload = _payload()
    payload["pipeline"]["pricing"]["slots"][3]["sell"] = _SPOT
    assert not _check(payload).overall_ok


def test_a_dynamic_sell_price_follows_the_export_feed():
    dynamic = PriceFormula(markup=0.005, grid_fees=(0.01,))
    payload = _payload(sell=dynamic, export=0.09)
    assert _check(payload).overall_ok
    payload["pipeline"]["pricing"]["slots"][5]["export"] = None
    assert not _check(payload).overall_ok


def test_skips_a_holiday_schedule_it_has_no_calendar_for(monkeypatch):
    monkeypatch.setattr("tools.checks.pricing._holiday_calendar", lambda country: None)
    result = _check(_payload())
    assert result.skipped
    assert "holiday" in result.skip_reason
