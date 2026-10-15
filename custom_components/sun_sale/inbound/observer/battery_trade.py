"""Realized trade prices: what the battery actually paid and earned in a day.

The running buy / sell averages (``pipeline/discharge_gate.py``,
``pipeline/precharge.py``) learn from the slots in which the battery really
traded with the grid, not from every price of the day. Two ``Side`` tracks on
the shared observer engine turn the persisted ``DerivedPowerSample`` stream into
per-slot kWh:

  * **bought** — battery charging while the grid imports:
    ``max(0, min(battery_signed, grid_net_signed))``
  * **sold** — battery discharging while the grid exports:
    ``max(0, min(−battery_signed, −grid_net_signed))``

The per-sample ``min`` is an upper bound on grid→battery / battery→grid energy:
house load drawn from the grid during a charge counts as bought. That is rare
beside real grid charging, which is what these averages are about.

Prices are the slot's ``buy_eur_kwh`` / ``sell_eur_kwh`` — the same
formula-applied figures the chart draws, never the raw market price.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from datetime import tzinfo as TzInfo
from typing import NamedTuple

from ...contract.models import (
    DerivedPowerHistory,
    DerivedPowerSample,
    PriceSeries,
    SlotKwh,
)
from .engine import ObservedSeriesEngine, Side

BOUGHT_SIDE_ID = "battery_bought"
SOLD_SIDE_ID = "battery_sold"

# A slot counts as traded only above this energy (0.2 kW averaged over a
# 15-minute slot), so sensor noise around zero never contributes a price.
MIN_SLOT_KWH = 0.05


class RealizedPrices(NamedTuple):
    """The kWh-weighted mean price of one day's grid trading, per direction.

    Attributes:
        buy: Mean ``buy_eur_kwh`` over the slots the battery charged from the
            grid, or ``None`` when it did not.
        sell: Mean ``sell_eur_kwh`` over the slots the battery discharged to the
            grid, or ``None`` when it did not.
    """

    buy: float | None
    sell: float | None


def _bought_kw(s: DerivedPowerSample) -> float:
    """Return the kW the battery charged from the grid in one sample (≥ 0)."""
    return max(0.0, min(s.battery_kw_signed, s.grid_net_kw_signed))


def _sold_kw(s: DerivedPowerSample) -> float:
    """Return the kW the battery discharged to the grid in one sample (≥ 0)."""
    return max(0.0, min(-s.battery_kw_signed, -s.grid_net_kw_signed))


def _weighted_mean(slots: list[SlotKwh], prices: list[float]) -> float | None:
    """Return the kWh-weighted mean price over the slots that really traded.

    Args:
        slots: Per-slot traded energy, in the same order as ``prices``.
        prices: The slot price for each entry of ``slots``.

    Returns:
        ``Σ kWh × price / Σ kWh`` over slots of at least ``MIN_SLOT_KWH``, or
        ``None`` when no slot traded that much.
    """
    traded = [(s.kwh, p) for s, p in zip(slots, prices, strict=True) if s.kwh >= MIN_SLOT_KWH]
    total = sum(kwh for kwh, _ in traded)
    if total <= 0.0:
        return None
    return sum(kwh * p for kwh, p in traded) / total


def realized_trade_prices(
    history: DerivedPowerHistory,
    price_series: PriceSeries,
    day: date,
    local_tz: TzInfo,
) -> RealizedPrices:
    """Return the prices the battery really bought and sold at on local ``day``.

    Args:
        history: Persisted cross-stream power samples.
        price_series: Prices for the planning window; only ``day``'s priced
            slots are read, so a forecast-filled placeholder never counts.
        day: The local date to average.
        local_tz: Timezone that decides where ``day`` starts and ends.

    Returns:
        The weighted buy and sell means, each ``None`` when the battery did not
        trade in that direction on ``day`` (or no samples cover it).
    """
    day_start = datetime.combine(day, time.min, tzinfo=local_tz).astimezone(UTC)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_tz).astimezone(UTC)
    samples = [s for s in history.samples if day_start <= s.timestamp < day_end]
    slots = [s for s in price_series.priced_slots if day_start <= s.start < day_end]
    if not samples or not slots:
        return RealizedPrices(None, None)

    engine = ObservedSeriesEngine(
        [Side(BOUGHT_SIDE_ID, _bought_kw), Side(SOLD_SIDE_ID, _sold_kw)], local_tz,
    )
    kwh = engine.build_slots_for_window(
        {BOUGHT_SIDE_ID: samples, SOLD_SIDE_ID: samples}, slots, day_start, day_end,
    )
    return RealizedPrices(
        buy=_weighted_mean(kwh[BOUGHT_SIDE_ID], [s.buy_eur_kwh for s in slots]),
        sell=_weighted_mean(kwh[SOLD_SIDE_ID], [s.sell_eur_kwh for s in slots]),
    )
