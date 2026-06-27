"""Shared engine for observed solar / grid series.

A `Side` is one non-negative kWh track with its own per-sample power extractor.
Generation registers a single side; grid registers two (import + export). The
engine handles slot iteration, per-side power averaging, and the once-per-day
proportional bake-in. Translators (HA-edge readers) and concrete output
dataclass packing stay per-module — this engine is HA-agnostic and stateless
w.r.t. samples.

Bake-in math: each slot scaled by ``counter_total / slot_sum``. Mathematically
equivalent to ``slot × (1 + error_pct)`` so zero-valued slots stay zero and
the per-slot shape is preserved. Skipped when the factor lands outside
``[BAKE_IN_FACTOR_MIN, BAKE_IN_FACTOR_MAX]`` (sensor-fault guard), when no
source counter total is available, or when the slot sum is zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import tzinfo as TzInfo
from typing import Any, Callable, Sequence

from ...contract.models import SlotKwh


# Bake-in factor guard. A correction factor outside this range indicates a
# sensor fault (counter ≫ slot_sum or counter ≪ slot_sum); bake-in is skipped
# and the raw slots are returned unchanged with the skip reason surfaced.
BAKE_IN_FACTOR_MIN = 0.5
BAKE_IN_FACTOR_MAX = 2.0


# Bake-in per-side status discriminator returned alongside baked slots.
BAKE_STATUS_OK = "ok"
BAKE_STATUS_SKIPPED_NO_SOURCE = "skipped_no_source"
BAKE_STATUS_SKIPPED_ZERO_SUM = "skipped_zero_sum"
BAKE_STATUS_SKIPPED_OUT_OF_RANGE = "skipped_out_of_range"


@dataclass(frozen=True)
class Side:
    """One non-negative kWh track in the observed-series engine.

    Attributes:
        id: Unique side identifier (e.g. "generation", "grid_import",
            "grid_export"). Used as the key in engine output dicts and as
            the `side_id` on persisted records.
        extract: Function converting one sample to a non-negative *power*
            value. The engine multiplies the per-slot mean of this value
            by the slot duration in hours to obtain kWh — so the function
            must return power in **kW**.
    """
    id: str
    extract: Callable[[Any], float]


class ObservedSeriesEngine:
    """Builds per-side per-slot kWh and applies the once-per-day bake-in.

    Stateless: callers pass sample histories and price slots on each call.
    Multi-side support lets a single engine handle both the 1-side
    (generation) and 2-side (grid: import + export) cases without
    duplication.
    """

    def __init__(self, sides: Sequence[Side], local_tz: TzInfo) -> None:
        """Initialise with the registered side specs + local timezone.

        Args:
            sides: One Side per kWh track. Generation = 1; grid = 2 (import
                and export). Order is preserved in iteration but each side
                is keyed independently in output dicts.
            local_tz: Local timezone. Currently retained for future use
                (window-by-local-date convenience helpers); the public API
                below already takes explicit UTC windows.
        """
        self._sides = tuple(sides)
        self._local_tz = local_tz

    @property
    def sides(self) -> tuple[Side, ...]:
        """Return the registered side specs in registration order."""
        return self._sides

    @property
    def local_tz(self) -> TzInfo:
        """Return the local timezone supplied at construction time."""
        return self._local_tz

    def build_slots_for_window(
        self,
        samples_by_side: dict[str, Sequence[Any]],
        price_slots: Sequence[Any],
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, list[SlotKwh]]:
        """Average per-sample extracted power within each slot in the window.

        Each side averages **its own** sample stream. For a slot in
        ``[window_start, window_end)``:

          * the slot is clamped to ``window_end`` if it extends past it,
          * for each registered side, samples with
            ``window_start <= s.timestamp < clamped_end`` are collected
            from ``samples_by_side[side.id]``,
          * the side's ``extract`` is applied to every relevant sample; the
            mean over the slot is multiplied by the slot duration in hours
            to give kWh; the result is clamped to ≥ 0 and rounded to 6
            decimal places.

        Slots with no relevant samples for a given side are emitted with
        ``kwh = 0`` for that side, so each side's list has the same length
        and slot positions.

        Args:
            samples_by_side: Mapping ``side_id → samples sequence``. Each
                sample must expose ``timestamp``. A side with no entry (or
                an empty sequence) yields all-zero slots.
            price_slots: Price-grid slots; each must expose ``start`` and
                ``end`` (UTC datetimes).
            window_start: Inclusive UTC window start.
            window_end: Exclusive UTC window end; slots are clamped here.

        Returns:
            Mapping ``side_id → list[SlotKwh]``. Order matches the iteration
            order of ``price_slots``.
        """
        result: dict[str, list[SlotKwh]] = {s.id: [] for s in self._sides}
        for ps in price_slots:
            if ps.start < window_start or ps.start >= window_end:
                continue
            end_t = ps.end if ps.end < window_end else window_end
            if end_t <= ps.start:
                continue
            duration_h = (end_t - ps.start).total_seconds() / 3600.0
            for side in self._sides:
                samples = samples_by_side.get(side.id, ())
                relevant = [s for s in samples if ps.start <= s.timestamp < end_t]
                if not relevant:
                    result[side.id].append(SlotKwh(ps.start, ps.end, 0.0))
                    continue
                avg_kw = sum(side.extract(s) for s in relevant) / len(relevant)
                kwh = max(0.0, round(avg_kw * duration_h, 6))
                result[side.id].append(SlotKwh(ps.start, ps.end, kwh))
        return result

    def apply_proportional_bake_in(
        self,
        raw_slots_per_side: dict[str, list[SlotKwh]],
        counter_totals_per_side: dict[str, float | None],
    ) -> dict[str, tuple[list[SlotKwh], str, float | None]]:
        """Scale per-side slots so each side's sum matches the counter total.

        Per side, the proportional factor is ``counter_total / slot_sum`` and
        every slot value is multiplied by it. Zero slots stay zero (shape is
        preserved; dawn/dusk slots never gain fabricated energy).

        A side is skipped (slots returned unchanged) when:

          * ``counter_total`` is ``None`` (no source available),
          * ``slot_sum`` is ≤ 0 (cannot scale up from zero),
          * the resulting factor lies outside
            ``[BAKE_IN_FACTOR_MIN, BAKE_IN_FACTOR_MAX]`` (sensor fault guard).

        Args:
            raw_slots_per_side: Per-side raw averaged slots — typically the
                output of ``build_slots_for_window`` over yesterday's window.
            counter_totals_per_side: Per-side authoritative daily total.
                ``None`` indicates the resolver could not produce a value for
                that side; bake-in is skipped for that side only.

        Returns:
            Mapping ``side_id → (baked_slots, status, factor)`` where status is
            one of ``BAKE_STATUS_*``. When ``status != BAKE_STATUS_OK``,
            ``baked_slots`` is the input list and ``factor`` is ``None`` (or
            the out-of-range factor for ``BAKE_STATUS_SKIPPED_OUT_OF_RANGE``
            so callers can log it).
        """
        out: dict[str, tuple[list[SlotKwh], str, float | None]] = {}
        for side in self._sides:
            raw = raw_slots_per_side.get(side.id, [])
            total = counter_totals_per_side.get(side.id)
            if total is None:
                out[side.id] = (raw, BAKE_STATUS_SKIPPED_NO_SOURCE, None)
                continue
            slot_sum = sum(s.kwh for s in raw)
            if slot_sum <= 0:
                out[side.id] = (raw, BAKE_STATUS_SKIPPED_ZERO_SUM, None)
                continue
            factor = total / slot_sum
            if not BAKE_IN_FACTOR_MIN <= factor <= BAKE_IN_FACTOR_MAX:
                out[side.id] = (raw, BAKE_STATUS_SKIPPED_OUT_OF_RANGE, factor)
                continue
            baked = [
                SlotKwh(s.start, s.end, round(s.kwh * factor, 6)) for s in raw
            ]
            out[side.id] = (baked, BAKE_STATUS_OK, factor)
        return out
