"""Discharge gate: sell above the running average, and shed tomorrow's overfill.

Pure Python — no Home Assistant imports.

Two small pieces consumed by :func:`pipeline.schedule.optimize_schedule`:

* **Running sell average.** One persisted figure that moves once per local day
  to ``0.9 × previous + 0.1 × today's mean sell price``. The first pass of the
  planner offers Discharge-to-grid only in slots selling at or above it, so
  stored energy waits for the better part of the price curve.

* **Overfill plan.** After that first pass, tomorrow's forecast is raised by the
  configured boost and its surplus over the house load is compared with the
  room the battery will have when the sun starts filling it. What does not fit
  is *overfill*: it would be sold cheaply or lost. The plan picks the
  highest-priced slots the first pass left unused, before that moment, until
  they can shed the overfill. A Discharge slot runs at the dispatcher's fixed
  discharge power, so the energy is spread by choosing **how many** slots, not
  by giving each a smaller share.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from ..contract.models import PriceSeries, PriceSlot, RunningSellAverage

# Weight of the previous average in the daily update; today's mean gets the rest.
RUNNING_AVERAGE_KEEP = 0.9

# Price comparisons tolerate float noise so a fixed sell price, whose running
# average equals it to within a rounding error, never gates its own slots out.
PRICE_EPS = 1e-9

# Below this much AC energy to shed, no slot is turned into a Discharge slot: a
# full-power forced discharge (and the inverter mode change around it) for a
# fraction of a kWh buys nothing, and a battery already at its floor — the usual
# state when the first pass has sold the evening — would just flap.
MIN_SHED_AC_KWH = 0.5


@dataclass(frozen=True)
class OverfillPlan:
    """Result of :func:`plan_overfill`.

    ``overfill_kwh`` is the storage-side energy tomorrow's boosted surplus has
    no room for; ``slot_indexes`` are the (ascending) future-slot indexes
    chosen to shed it — empty when there is no overfill or nothing to shed.
    """

    overfill_kwh: float = 0.0
    slot_indexes: tuple[int, ...] = ()


def fold_running_average(
    stored: RunningSellAverage | None,
    price_series: PriceSeries,
    today: date,
    local_tz: tzinfo,
) -> RunningSellAverage | None:
    """Return the running sell average as of local ``today``.

    The update happens once per local day: a stored figure already stamped
    ``today`` (or later) is returned as the same object, so the caller can tell
    "unchanged" by identity and persist only when the day moved on. With no
    stored figure, today's mean seeds it.

    Args:
        stored: The persisted average, or ``None`` on a cold start.
        price_series: Prices for the planning window; only today's priced slots
            are read, so a forecast-filled placeholder never moves the average.
        today: Local date the figure is wanted for.
        local_tz: Timezone that decides which slots belong to ``today``.

    Returns:
        The average for ``today``; the unchanged ``stored`` when it is current or
        when today has no priced slot yet (``None`` if there was nothing stored).
    """
    if stored is not None and stored.day >= today:
        return stored
    sells = [
        s.sell_eur_kwh for s in price_series.priced_slots
        if s.start.astimezone(local_tz).date() == today
    ]
    if not sells:
        return stored
    mean = sum(sells) / len(sells)
    if stored is None:
        return RunningSellAverage(value_eur_kwh=mean, day=today)
    return RunningSellAverage(
        value_eur_kwh=(
            RUNNING_AVERAGE_KEEP * stored.value_eur_kwh
            + (1.0 - RUNNING_AVERAGE_KEEP) * mean
        ),
        day=today,
    )


def plan_overfill(
    *,
    slots: Sequence[PriceSlot],
    solar_kwh: Sequence[float],
    baseload_kwh: Sequence[float],
    soc_before: Sequence[float],
    taken: Sequence[bool],
    eligible: Sequence[bool],
    drain_ac_kwh: Callable[[int], float],
    now: datetime,
    local_tz: tzinfo | None,
    boost: float,
    max_charge_kwh: float,
    cap_kwh: float,
    max_soc: float,
    floor_soc: float,
    eff: float,
) -> OverfillPlan:
    """Pick the slots that make room for tomorrow's overfill.

    Tomorrow's net solar (forecast × ``1 + boost`` minus the house load, per
    slot, limited by the charge rate) is what the battery is about to be asked
    to store. The room it has is read from the first-pass SoC trajectory at the
    first slot where net solar turns positive; the excess is the overfill. It is
    shed from what the battery holds above the floor at that moment, in the
    highest-priced slots before it that the first pass left unused.

    Args:
        slots: The future price slots, chronological.
        solar_kwh: Expected (unboosted) solar per slot.
        baseload_kwh: Expected household load per slot.
        soc_before: First-pass SoC at the start of each slot.
        taken: Slots the first pass already discharges in.
        eligible: Slots where Discharge may be offered (sell at or above the
            battery cycle cost).
        drain_ac_kwh: Battery AC energy a Discharge slot ``i`` delivers.
        now: Cycle timestamp — anchors which local date is "tomorrow".
        local_tz: Local timezone; ``None`` disables the plan (no "tomorrow").
        boost: Fraction added to the forecast (0.2 = +20 %).
        max_charge_kwh: Most the battery can take in one slot.
        cap_kwh: Estimated usable capacity.
        max_soc: Top of the SoC envelope.
        floor_soc: Planning floor the shed energy is measured from.
        eff: Round-trip efficiency; converts shed storage kWh to AC kWh.

    Returns:
        The overfill and the slots to turn into Discharge slots.
    """
    if local_tz is None or cap_kwh <= 0:
        return OverfillPlan()
    tomorrow = now.astimezone(local_tz).date() + timedelta(days=1)
    fill_start: int | None = None
    surplus = 0.0
    for i, slot in enumerate(slots):
        if slot.start.astimezone(local_tz).date() != tomorrow:
            continue
        net = solar_kwh[i] * (1.0 + boost) - baseload_kwh[i]
        if net <= 0:
            continue
        if fill_start is None:
            fill_start = i
        surplus += min(net, max_charge_kwh)
    if fill_start is None:
        return OverfillPlan()

    soc = soc_before[fill_start]
    overfill = max(0.0, surplus - max(0.0, max_soc - soc) * cap_kwh)
    if overfill <= PRICE_EPS:
        return OverfillPlan()

    to_shed_ac = min(overfill, max(0.0, soc - floor_soc) * cap_kwh) * eff
    if to_shed_ac < MIN_SHED_AC_KWH:
        return OverfillPlan(overfill_kwh=overfill)
    candidates = sorted(
        (i for i in range(fill_start) if eligible[i] and not taken[i]),
        key=lambda i: (-slots[i].sell_eur_kwh, i),
    )
    chosen: list[int] = []
    shed = 0.0
    for i in candidates:
        if shed >= to_shed_ac - PRICE_EPS:
            break
        drain = drain_ac_kwh(i)
        if drain <= PRICE_EPS:
            continue
        chosen.append(i)
        shed += drain
    return OverfillPlan(overfill_kwh=overfill, slot_indexes=tuple(sorted(chosen)))
