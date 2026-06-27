"""Inverter clock-skew tracker — measures and exposes HA↔inverter time drift.

The daily-resetting counters roll over at **inverter-local midnight**, not
HA-local midnight. When the two clocks disagree (skew > ~1 minute), an
HA-timed pre-rollover snapshot may fire after the counter has already reset,
reading a near-zero value instead of the day's accumulated total. This module:

1. Reads the inverter clock entity each cycle (via
   ``InverterTimeTranslator``).
2. Maintains a rolling buffer of paired (HA UTC, inverter UTC) samples in
   ``InverterTimeHistory``.
3. Once enough samples are present, exposes the **median** skew through
   ``current_skew_seconds`` so the snapshot module can shift its window
   relative to the inverter's idea of "now".

Median (not mean) is used so a single bogus reading — e.g., a brief
"unavailable" interlude that returns a stale cached value — cannot pull the
estimate. Confidence is gated by ``INVERTER_TIME_MIN_SAMPLES``: until that
threshold is met, the snapshot module falls back to HA-local timing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import tzinfo as TzInfo
from statistics import median
from typing import Any

from ..contract.const import (
    INVERTER_TIME_MAX_SAMPLES,
    INVERTER_TIME_MIN_SAMPLES,
)
from ..contract.models import InverterTimeReading, SunSaleConfig


@dataclass(frozen=True)
class InverterTimeHistory:
    """Rolling buffer of inverter-clock observations.

    Coordinator owns one instance per entry. Each cycle, a fresh reading is
    appended (when available) and the oldest sample is trimmed once the
    buffer reaches ``INVERTER_TIME_MAX_SAMPLES``.
    """
    samples: tuple[InverterTimeReading, ...]


def empty_history() -> InverterTimeHistory:
    """Return a fresh, empty history (useful as a startup default).

    Returns:
        ``InverterTimeHistory`` with no samples.
    """
    return InverterTimeHistory(samples=())


def update_history(
    history: InverterTimeHistory,
    reading: InverterTimeReading | None,
    max_samples: int = INVERTER_TIME_MAX_SAMPLES,
) -> InverterTimeHistory:
    """Append ``reading`` to the history and trim to ``max_samples``.

    Args:
        history: Current rolling history.
        reading: New reading; ``None`` is a no-op (this cycle's read failed).
        max_samples: Retention cap on the returned history.

    Returns:
        Updated history. Returns the input instance unchanged when
        ``reading`` is ``None`` and no trimming is needed.
    """
    if reading is None:
        if len(history.samples) <= max_samples:
            return history
        return InverterTimeHistory(samples=history.samples[-max_samples:])
    new_samples = (history.samples + (reading,))[-max_samples:]
    return InverterTimeHistory(samples=new_samples)


def current_skew_seconds(
    history: InverterTimeHistory,
    min_samples: int = INVERTER_TIME_MIN_SAMPLES,
) -> float | None:
    """Return the median ``inverter_now - ha_now`` in seconds, or ``None``.

    Positive values mean the inverter clock is ahead of HA; negative values
    mean the inverter is behind. ``None`` is returned until the rolling
    history holds at least ``min_samples`` samples (confidence gate).

    Args:
        history: Rolling samples of inverter-clock observations.
        min_samples: Confidence threshold; below it, ``None`` is returned.

    Returns:
        Median skew in seconds, or ``None`` when insufficient samples.
    """
    if len(history.samples) < min_samples:
        return None
    skews = [
        (s.inverter_now - s.ha_now).total_seconds() for s in history.samples
    ]
    return median(skews)


class InverterTimeTranslator:
    """Reads the inverter clock HA entity; produces ``InverterTimeReading``.

    The inverter typically exposes its clock as a local-time datetime entity
    with no timezone info. The translator attaches the HA installation's
    local timezone and converts to UTC so all downstream maths stays in UTC.
    """

    output_type = InverterTimeReading

    def __init__(self, entity_id: str, local_tz: TzInfo) -> None:
        """Initialise with the HA entity ID and local timezone.

        Args:
            entity_id: Entity ID of the inverter time / datetime entity.
                Empty string disables this translator (returns ``None``).
            local_tz: HA installation timezone, used to interpret naive
                datetimes read from the entity.
        """
        self._entity_id = entity_id
        self._local_tz = local_tz

    def parse(self, hass: Any, now: datetime | None = None) -> InverterTimeReading | None:
        """Read the inverter clock entity and return a paired reading.

        Accepts ISO-format strings (with or without timezone info) as well as
        datetime objects (which HA may surface for ``datetime`` platform
        entities). A naive datetime is assumed to be in the configured local
        timezone.

        Args:
            hass: Home Assistant instance.
            now: HA-side reference timestamp; defaults to UTC now.

        Returns:
            ``InverterTimeReading`` or ``None`` when the entity is missing,
            unavailable, or unparseable.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if not self._entity_id:
            return None
        state = hass.states.get(self._entity_id)
        if state is None:
            return None

        raw = state.state
        if raw in ("unavailable", "unknown", "", None):
            return None

        inverter_dt: datetime | None = None
        if isinstance(raw, datetime):
            inverter_dt = raw
        else:
            try:
                inverter_dt = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                return None

        if inverter_dt.tzinfo is None:
            inverter_dt = inverter_dt.replace(tzinfo=self._local_tz)

        return InverterTimeReading(
            ha_now=now,
            inverter_now=inverter_dt.astimezone(timezone.utc),
        )

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime,
    ) -> InverterTimeReading | None:
        """DAG translator entry-point; delegates to ``parse``.

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused — timezone is captured
                at construction time).
            raw_config: Raw config-entry dict (unused).
            now: Cycle timestamp.

        Returns:
            ``InverterTimeReading`` or ``None`` when unavailable.
        """
        return self.parse(hass, now)
