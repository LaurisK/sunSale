"""Driver-owned liveness timer for platforms with a volatile hold.

Kept out of :mod:`..outbound.driver` deliberately: that module is the neutral
*contract* and stays free of Home Assistant imports, while a heartbeat is
concrete machinery only some drivers instantiate. Nothing in the driver protocol
references this class — see the "liveness is a driver concern" note there.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)


class Heartbeat:
    """A driver-owned repeating timer for platforms with a volatile hold.

    Some inverters forget a commanded mode unless it is re-asserted before a
    hardware deadman expires (Solis: the RC function; Huawei: a timed forcible
    charge). **How often, and whether at all, is a property of the platform** —
    so the timer belongs to the driver, not to the control module, whose job
    ends at "this mode is still the target". This class is the shared plumbing
    so each driver that needs one is a field and two calls rather than its own
    copy of the ``async_track_time_interval`` dance.

    Not part of :class:`InverterControlDriver` — the neutral protocol has no
    keep-alive vocabulary at all. A driver whose hold is persistent (a written
    setting, an inverter-side failsafe long enough to be refreshed by the
    ordinary cycle) simply never constructs one.
    """

    def __init__(
        self,
        hass: HomeAssistant | None,
        interval: timedelta,
        action: Callable[[], Coroutine[Any, Any, None]],
        name: str,
    ) -> None:
        """Wire the timer without starting it.

        Args:
            hass: Home Assistant instance. ``None`` (unit-test wiring with no
                event loop) makes :meth:`start` a no-op, so the driver degrades
                to whatever re-assertion its ``hold`` does per cycle.
            interval: How often ``action`` runs while started.
            action: Zero-arg coroutine invoked on each beat.
            name: Short label for log lines.
        """
        self._hass = hass
        self._interval = interval
        self._action = action
        self._name = name
        self._cancel: Callable[[], None] | None = None

    def start(self) -> None:
        """(Re)start the timer, anchoring the next beat one interval from now.

        Idempotent in effect: an already-running timer is cancelled first, so
        calling this after a fresh full write keeps the beat phased to that
        write rather than to whenever the timer first started.
        """
        self.stop()
        if self._hass is None:
            return
        self._cancel = async_track_time_interval(
            self._hass, self._on_beat, self._interval,
        )

    def stop(self) -> None:
        """Cancel the timer if running. Idempotent; never raises."""
        if self._cancel is None:
            return
        try:
            self._cancel()
        except Exception:  # noqa: BLE001 — cancel must never raise
            _LOGGER.debug(
                "%s heartbeat: cancel raised — ignoring", self._name, exc_info=True,
            )
        self._cancel = None

    async def _on_beat(self, _now: datetime) -> None:
        """Run the driver's action, swallowing failures so the timer survives."""
        try:
            await self._action()
        except Exception:  # noqa: BLE001 — a failed beat must not kill the timer
            _LOGGER.warning(
                "%s heartbeat: re-assert failed — will retry next beat",
                self._name, exc_info=True,
            )
