"""Calculation stage: derive per-slot decision flags from prices and generation.

Pure Python — no Home Assistant imports.
Consumption is out of scope for v1; GenerationSeries stays solar-only.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..contract.models import (
    BatteryState,
    CalculationResult,
    GenerationSeries,
    PriceSeries,
    PriceSlot,
    SlotDecision,
)


def calculate(
    prices: PriceSeries,
    generation: GenerationSeries,
    battery_state: BatteryState,
    now: datetime,
) -> CalculationResult:
    """Derive per-slot decision flags and feed-in lockout windows from price/generation data.

    v1 responsibility: feed-in lockout reporting.
      - Contiguous slots with non-positive sell prices are coalesced into
        ``feed_in_lockout_windows`` (a locked-out slot is one where exporting
        would cost rather than earn).
      - Expected solar production during locked-out slots is reported in
        ``expected_solar_negative_sale_kwh`` (no decision taken — the schedule
        module decides what to do).

    Args:
        prices: Full PriceSeries covering the scheduling horizon.
        generation: Expected solar generation aligned to the price grid.
        battery_state: Current SoC and estimated capacity (for headroom check).
        now: Cycle timestamp.

    Returns:
        CalculationResult with per-slot SlotDecisions and coalesced lockout windows.
    """
    slots: list[SlotDecision] = []

    for price_slot in prices.slots:
        locked_out = price_slot.sell_eur_kwh <= 0.0
        expected_kwh = generation.energy_between(price_slot.start, price_slot.end)
        notes: list[str] = []

        negative_kwh = expected_kwh if locked_out else 0.0

        if locked_out and expected_kwh > 0:
            headroom_kwh = battery_state.estimated_capacity_kwh * (1.0 - battery_state.soc)
            if expected_kwh > headroom_kwh:
                notes.append("battery_full_during_lockout")

        if price_slot.buy_eur_kwh < 0:
            notes.append("paid_to_charge")

        slots.append(SlotDecision(
            start=price_slot.start,
            end=price_slot.end,
            expected_solar_kwh=expected_kwh,
            expected_solar_negative_sale_kwh=negative_kwh,
            notes=tuple(notes),
        ))

    lockout_windows = _coalesce_lockout_windows(prices.slots)
    total_negative_kwh = sum(s.expected_solar_negative_sale_kwh for s in slots)

    return CalculationResult(
        slots=tuple(slots),
        feed_in_lockout_windows=lockout_windows,
        total_negative_sale_kwh=total_negative_kwh,
        computed_at=now,
    )


def _coalesce_lockout_windows(
    price_slots: Iterable[PriceSlot],
) -> tuple[tuple[datetime, datetime], ...]:
    """Merge contiguous non-positive-sell-price slots into (start, end) windows.

    Args:
        price_slots: PriceSlot iterable in chronological order.

    Returns:
        Tuple of (window_start, window_end) pairs for each contiguous locked-out run.
    """
    windows: list[tuple[datetime, datetime]] = []
    win_start: datetime | None = None
    win_end: datetime | None = None
    for slot in price_slots:
        if slot.sell_eur_kwh <= 0.0:
            if win_start is None:
                win_start = slot.start
            win_end = slot.end
        else:
            if win_start is not None:
                windows.append((win_start, win_end))  # type: ignore[arg-type]
                win_start = None
                win_end = None
    if win_start is not None:
        windows.append((win_start, win_end))  # type: ignore[arg-type]
    return tuple(windows)
