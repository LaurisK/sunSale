"""Monthly electricity bill computation — pure Python, no HA imports.

Builds a per-price-slot cost series for the live window [yday_start, now)
and adds a carry that covers the bill from month-start up to the start of
the live window. The two together give the running monthly bill.

Per-slot import/export kWh are sourced from the upstream `ObservedGridSeries`
(see ``inbound/grid.py``). Costs are priced through `PriceSeries`:

    slot_cost = imported_kWh * buy_eur_kwh − exported_kWh * sell_eur_kwh

No floor is applied to `sell_eur_kwh`; negative sell prices charge the
exporter as they would on the real market.

Per-day ledger (`MonthlyBillState.finalized_days`)
--------------------------------------------------
The carry is **derived from a per-day ledger**, not accumulated once at
rollover. The live `PriceSeries` only ever spans roughly
``[yesterday 00:00 local, tomorrow 23:45 local]`` — so a completed day can be
priced *only while it is still "yesterday"*. The previous design folded the
day-before-yesterday into a running ``carry_eur`` at each rollover; by then
that day had already dropped out of the price window, so `PriceSeries.window`
returned no slots and the bridge silently added 0 every single day — the carry
never grew. (That is the bug this module is the fix for.)

Instead, every cycle:

  * each completed local day whose 00:00 start is still at/after the earliest
    price slot is (re)priced from the current `ObservedGridSeries` +
    `PriceSeries` and written into the ledger — overwriting any prior value, so
    a day converges onto its bake-in-corrected total as the day's bake lands and
    transient empty cycles self-heal;
  * once the price window rolls past a day it is no longer recomputed and its
    ledger entry stays frozen;
  * the carry is the sum of current-month ledger entries that sit fully before
    the live window; the previous-month total is the sum of the previous
    calendar month's entries;
  * entries older than the previous calendar month are pruned.

This is self-healing across restarts (the carry is rebuilt from the ledger each
cycle) and across a missed cycle (a day is re-priced for as long as it stays in
the price window). Days that were never observed while priceable cannot be
recovered — their prices are gone — and simply contribute 0.

Live slots are always computed over `[max(yday_start, month_start), now)`, so on
the first day of a new month they cover only the current month (the previous
month's last day lives in `previous_month_eur`, not in `slots`).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from ..contract.models import (
    BillSlot,
    MonthlyBillResult,
    MonthlyBillState,
    ObservedGridSeries,
    PriceSeries,
)

if TYPE_CHECKING:
    from datetime import tzinfo


def compute_bill_slots(
    grid_series: ObservedGridSeries,
    price_series: PriceSeries,
    t_start: datetime,
    t_end: datetime,
) -> list[BillSlot]:
    """Return per-price-slot electricity cost for grid flow in [t_start, t_end).

    Iterates the price slots overlapping ``[t_start, t_end)`` and looks up each
    slot's imported/exported kWh in ``grid_series``. The grid-series resolution
    matches the price grid, so the match is by ``start`` time; the small fraction
    of slots that the series has not yet covered (e.g. windows older than the
    grid history's retention) contribute zero on both sides.

    Cost uses the slot's ``buy_eur_kwh`` on imports and ``sell_eur_kwh`` on
    exports (no floor — negative sell prices are honoured). When ``t_start`` or
    ``t_end`` falls partway through a price slot, the cost is pro-rated by the
    overlap fraction so partial-slot windows (e.g. month-rollover bridges)
    bill accurately.

    Args:
        grid_series: Observed per-slot gross import/export kWh from
            ``inbound/grid.py:build_observed_grid_series``.
        price_series: Tariff-adjusted price series covering at least [t_start, t_end).
        t_start: Start of the billing window (inclusive, tz-aware).
        t_end: End of the billing window (exclusive, tz-aware).

    Returns:
        List of BillSlot objects, one per overlapping price slot.
    """
    grid_by_start = {gs.start: gs for gs in grid_series.slots}

    result: list[BillSlot] = []
    for price_slot in price_series.window(t_start, t_end):
        slot_start = max(price_slot.start, t_start)
        slot_end = min(price_slot.end, t_end)

        full_secs = (price_slot.end - price_slot.start).total_seconds()
        win_secs = (slot_end - slot_start).total_seconds()
        overlap_fraction = (win_secs / full_secs) if full_secs > 0 else 0.0

        gs = grid_by_start.get(price_slot.start)
        if gs is not None and overlap_fraction > 0:
            imported_kwh = gs.imported_kwh * overlap_fraction
            exported_kwh = gs.exported_kwh * overlap_fraction
        else:
            imported_kwh = 0.0
            exported_kwh = 0.0

        net_cost_eur = (
            imported_kwh * price_slot.buy_eur_kwh
            - exported_kwh * price_slot.sell_eur_kwh
        )

        result.append(BillSlot(
            start=slot_start,
            end=slot_end,
            imported_kwh=round(imported_kwh, 4),
            exported_kwh=round(exported_kwh, 4),
            buy_eur_kwh=price_slot.buy_eur_kwh,
            sell_eur_kwh=price_slot.sell_eur_kwh,
            net_cost_eur=round(net_cost_eur, 6),
        ))
    return result


def build_monthly_bill_result(
    grid_series: ObservedGridSeries,
    price_series: PriceSeries,
    stored_state: MonthlyBillState | None,
    local_tz: tzinfo,
    now: datetime,
) -> MonthlyBillResult:
    """Compute the net monthly electricity bill from the per-day ledger.

    Each cycle: re-price every completed local day still inside the price
    window into the ledger (so it converges with its bake-in and self-heals),
    derive the carry and previous-month total from the ledger, prune entries
    older than the previous calendar month, then add the live window.

    Args:
        grid_series: ObservedGridSeries with per-slot gross import/export kWh.
        price_series: Full ~72-hour tariff-adjusted price series.
        stored_state: Previously persisted ledger, or None on first run.
        local_tz: HA installation timezone, used to locate local midnights.
        now: Current cycle UTC timestamp.

    Returns:
        MonthlyBillResult with per-slot data, carry, previous-month total,
        and the updated ledger for persistence.
    """
    local_now = now.astimezone(local_tz)
    today = local_now.date()
    current_month_str = today.strftime("%Y-%m")
    prev_month_str = _previous_month_str(today)
    month_start = _local_midnight_utc(date(today.year, today.month, 1), local_tz)
    today_start = _local_midnight_utc(today, local_tz)
    yday_date = today - timedelta(days=1)
    yday_start = _local_midnight_utc(yday_date, local_tz)
    live_start = max(yday_start, month_start)

    ledger: dict[str, float] = dict(stored_state.finalized_days) if stored_state else {}

    # Re-price every completed day whose 00:00 start is still at/after the
    # earliest price slot. In steady state that is only "yesterday"; iterating
    # back to the price-window start also re-prices a day skipped by a missed
    # cycle, as long as it has not yet aged out of the window. Re-pricing a
    # day already in the ledger overwrites it, letting the value track the
    # late-arriving bake-in correction until the window rolls past it.
    price_start = min((s.start for s in price_series.slots), default=None)
    if price_start is not None:
        d = yday_date
        while True:
            d_start = _local_midnight_utc(d, local_tz)
            if d_start < price_start:
                break
            d_end = _local_midnight_utc(d + timedelta(days=1), local_tz)
            day_slots = compute_bill_slots(grid_series, price_series, d_start, d_end)
            ledger[d.isoformat()] = round(sum(s.net_cost_eur for s in day_slots), 6)
            d -= timedelta(days=1)

    # Keep only the current and previous calendar month; older days are gone
    # from any displayable total and would only grow the persisted state.
    ledger = {
        ds: cost for ds, cost in ledger.items()
        if ds[:7] in (current_month_str, prev_month_str)
    }

    # Carry = current-month ledger entries that sit fully before the live
    # window (live covers [live_start, now); a completed day before it ends
    # at/by live_start). On the 1st of a month there are none → carry is 0.
    carry_eur = 0.0
    for ds, cost in ledger.items():
        if ds[:7] != current_month_str:
            continue
        d = date.fromisoformat(ds)
        if _local_midnight_utc(d + timedelta(days=1), local_tz) <= live_start:
            carry_eur += cost
    carry_eur = round(carry_eur, 6)

    previous_month_eur = round(
        sum(cost for ds, cost in ledger.items() if ds[:7] == prev_month_str), 6,
    )
    has_prev = any(ds[:7] == prev_month_str for ds in ledger)
    previous_month_str = prev_month_str if has_prev else ""

    slots = compute_bill_slots(grid_series, price_series, live_start, now)
    yday_to_now_eur = sum(s.net_cost_eur for s in slots)

    # Daily breakdowns are computed over their own exact local-day windows
    # rather than sliced out of the live slots, so they stay correct on the
    # first of a month (when the live window starts at month_start, not
    # yday_start). The grid series spans day-before-yesterday → now, so both
    # windows are covered.
    today_slots = compute_bill_slots(grid_series, price_series, today_start, now)
    today_cost_eur = sum(s.net_cost_eur for s in today_slots)
    yday_full_slots = compute_bill_slots(
        grid_series, price_series, yday_start, today_start,
    )
    yesterday_cost_eur = sum(s.net_cost_eur for s in yday_full_slots)

    return MonthlyBillResult(
        slots=tuple(slots),
        carry_eur=carry_eur,
        yday_to_now_eur=yday_to_now_eur,
        total_month_eur=carry_eur + yday_to_now_eur,
        month_str=current_month_str,
        previous_month_str=previous_month_str,
        previous_month_eur=previous_month_eur,
        updated_state=MonthlyBillState(
            finalized_days=tuple(sorted(ledger.items())),
        ),
        computed_at=now,
        today_cost_eur=today_cost_eur,
        yesterday_cost_eur=yesterday_cost_eur,
    )


def _previous_month_str(d: date) -> str:
    """Return the "YYYY-MM" of the calendar month before ``d``'s month.

    Args:
        d: Any local date.

    Returns:
        The previous calendar month formatted as "YYYY-MM".
    """
    first_of_month = date(d.year, d.month, 1)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


def _local_midnight_utc(d: date, local_tz: tzinfo) -> datetime:
    """Return UTC datetime for local midnight on the given date.

    Args:
        d: Local calendar date.
        local_tz: Local timezone.

    Returns:
        UTC-aware datetime corresponding to 00:00 local time on `d`.
    """
    return datetime(d.year, d.month, d.day, tzinfo=local_tz).astimezone(UTC)
