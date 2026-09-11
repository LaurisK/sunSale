"""Battery-export guard — monitor-only detection of unrequested battery export.

The control loop verifies what sunSale *wrote*; it cannot see an inverter that
accepts every write and then behaves differently. On 2026-09-09 the reference
Solis entered an undocumented state (register 33122 = 4096) right after a Remote
Dispatch release and exported at the BMS discharge limit for 91 minutes — past
sunSale's ``min_soc`` and the inverter's own over-discharge SoC — while every
register row read back ``match``. See ``docs/battery_export_guard.md``.

Two platform-neutral watchers live here, both pure state machines fed by the
coordinator:

  * :class:`ExportGuard` — trips when more power leaves to the grid than the
    panels produce (``export − PV``) while a passive mode is commanded. No
    passive mode ever asks the battery to feed the grid. Keyed on grid and PV
    rather than battery power because the battery registers can see only part of
    a multi-pack bank.
  * :class:`SocFloorWatch` — flags a battery still discharging at or below
    ``min_soc`` under a passive mode: the planner's floor is not a hardware
    limit, so this is where a breach becomes visible.

Phase 1 is observation only: neither class writes anything. The coordinator
logs, notifies and exposes their state.

Pure Python: no Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..contract.models import StorageMode

# Modes under which the battery must never feed the grid: sunSale commands no
# export target, so any battery→grid flow is the inverter acting on its own.
PASSIVE_MODES: frozenset[StorageMode] = frozenset({
    StorageMode.SelfUse,
    StorageMode.NoExport,
    StorageMode.StandBy,
    StorageMode.FeedIn,
})

# ``export − PV`` (kW) at or above which a sample counts as battery export.
# Normal passive operation on the reference install never exceeded 0.02 kW over
# 10 days of recorder history; the incident ran at 2.5–10 kW.
EXPORT_GUARD_THRESHOLD_KW = 1.0

# How long the condition must hold before tripping. Every dispatch→passive
# transition produces a single ~10 s sample above threshold while the inverter
# winds the forced mode down; 120 s filters all of them out.
EXPORT_GUARD_PERSIST = timedelta(seconds=120)

# How long the condition must stay false before a trip is considered over.
EXPORT_GUARD_CLEAR_AFTER = timedelta(seconds=60)

# Longest gap between two samples that is still integrated into the episode's
# energy. A longer silence (sensor outage, HA restart) is not assumed to have
# exported at the last seen rate.
_MAX_INTEGRATION_GAP = timedelta(seconds=60)

# Battery discharge (kW) above which the SoC-floor watch treats the battery as
# actively draining rather than idling on measurement noise.
SOC_FLOOR_DISCHARGE_KW = 0.2

STATE_IDLE = "idle"
STATE_SUSPECT = "suspect"
STATE_TRIPPED = "tripped"

TRANSITION_TRIPPED = "tripped"
TRANSITION_CLEARED = "cleared"


def _iso(value: datetime | None) -> str | None:
    """Return ``value`` as an ISO string, or ``None``."""
    return value.isoformat() if value is not None else None


@dataclass
class ExportGuardEpisode:
    """One stretch of battery export under a passive mode.

    Attributes:
        onset: When the condition first held.
        last_seen: Most recent sample at which the condition held.
        commanded_mode: The passive mode sunSale was commanding at onset.
        soc_at_onset: Battery SoC (0..1) at onset, when known.
        tripped_at: When the episode outlasted the persistence window, or
            ``None`` while it is still only suspect.
        cleared_at: When the condition stopped holding for good.
        peak_kw: Largest ``export − PV`` seen.
        export_kwh: ``export − PV`` integrated over the episode.
        snapshot: Raw platform state captured at the trip (driver-defined).
    """

    onset: datetime
    last_seen: datetime
    commanded_mode: str | None
    soc_at_onset: float | None
    tripped_at: datetime | None = None
    cleared_at: datetime | None = None
    peak_kw: float = 0.0
    export_kwh: float = 0.0
    snapshot: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the episode as a JSON-friendly dict."""
        return {
            "onset": _iso(self.onset),
            "tripped_at": _iso(self.tripped_at),
            "cleared_at": _iso(self.cleared_at),
            "last_seen": _iso(self.last_seen),
            "commanded_mode": self.commanded_mode,
            "soc_at_onset": self.soc_at_onset,
            "peak_kw": round(self.peak_kw, 3),
            "export_kwh": round(self.export_kwh, 3),
            "snapshot": dict(self.snapshot),
        }


class ExportGuard:
    """Detects battery energy leaving to the grid while a passive mode is commanded.

    States: ``idle`` → ``suspect`` (condition holds) → ``tripped`` (held for
    ``persist``). A tripped episode clears after the condition has been false
    for ``clear_after`` and is kept as ``last_trip``.
    """

    def __init__(
        self,
        threshold_kw: float = EXPORT_GUARD_THRESHOLD_KW,
        persist: timedelta = EXPORT_GUARD_PERSIST,
        clear_after: timedelta = EXPORT_GUARD_CLEAR_AFTER,
    ) -> None:
        """Configure the detector thresholds.

        Args:
            threshold_kw: ``export − PV`` in kW that counts as battery export.
            persist: How long the condition must hold before tripping.
            clear_after: How long it must stay false before a trip ends.
        """
        self._threshold_kw = threshold_kw
        self._persist = persist
        self._clear_after = clear_after
        self._state = STATE_IDLE
        self._episode: ExportGuardEpisode | None = None
        self._last_trip: ExportGuardEpisode | None = None
        self._trip_count = 0
        self._last_update: datetime | None = None
        self._clear_since: datetime | None = None
        self._last_excess_kw: float | None = None

    @property
    def state(self) -> str:
        """Return ``idle``, ``suspect`` or ``tripped``."""
        return self._state

    @property
    def episode(self) -> ExportGuardEpisode | None:
        """Return the episode in progress (suspect or tripped), if any."""
        return self._episode

    @property
    def last_trip(self) -> ExportGuardEpisode | None:
        """Return the most recent episode that tripped and has since cleared."""
        return self._last_trip

    def attach_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Store a platform state capture on the current episode.

        Args:
            snapshot: Raw readbacks captured by the caller at the trip.
        """
        if self._episode is not None:
            self._episode.snapshot = dict(snapshot)

    def update(
        self,
        now: datetime,
        commanded: StorageMode | None,
        grid_kw: float | None,
        pv_kw: float | None,
        soc: float | None = None,
    ) -> str | None:
        """Feed one sample and return the transition it caused, if any.

        Args:
            now: Sample time.
            commanded: The mode sunSale last commanded, or ``None``.
            grid_kw: Net grid power in kW, positive = import.
            pv_kw: PV production in kW. ``None`` (sensor unavailable) makes the
                sample unevaluable: without PV, daytime export would read as
                battery export.
            soc: Battery SoC (0..1), recorded at onset.

        Returns:
            ``"tripped"`` when an episode crosses the persistence window,
            ``"cleared"`` when a tripped episode ends, otherwise ``None``.
        """
        excess = None if grid_kw is None or pv_kw is None else -grid_kw - pv_kw
        active = (
            commanded in PASSIVE_MODES
            and excess is not None
            and excess >= self._threshold_kw
        )
        self._integrate(now)
        self._last_update = now
        self._last_excess_kw = excess if active else None

        if active:
            assert excess is not None
            self._clear_since = None
            if self._episode is None:
                self._episode = ExportGuardEpisode(
                    onset=now,
                    last_seen=now,
                    commanded_mode=commanded.value if commanded is not None else None,
                    soc_at_onset=soc,
                )
                self._state = STATE_SUSPECT
            self._episode.last_seen = now
            self._episode.peak_kw = max(self._episode.peak_kw, excess)
            if (
                self._state == STATE_SUSPECT
                and now - self._episode.onset >= self._persist
            ):
                self._state = STATE_TRIPPED
                self._episode.tripped_at = now
                self._trip_count += 1
                return TRANSITION_TRIPPED
            return None

        if self._state == STATE_SUSPECT:
            self._state = STATE_IDLE
            self._episode = None
            return None
        if self._state == STATE_TRIPPED:
            if self._clear_since is None:
                self._clear_since = now
            if now - self._clear_since >= self._clear_after:
                assert self._episode is not None
                self._episode.cleared_at = now
                self._last_trip = self._episode
                self._episode = None
                self._state = STATE_IDLE
                self._clear_since = None
                return TRANSITION_CLEARED
        return None

    def _integrate(self, now: datetime) -> None:
        """Add the energy exported since the previous sample to the episode.

        Uses the previous sample's excess (the rate that held over the interval)
        and skips gaps longer than ``_MAX_INTEGRATION_GAP``.

        Args:
            now: Time of the sample being fed.
        """
        if (
            self._episode is None
            or self._last_update is None
            or self._last_excess_kw is None
        ):
            return
        gap = now - self._last_update
        if gap <= timedelta(0) or gap > _MAX_INTEGRATION_GAP:
            return
        self._episode.export_kwh += self._last_excess_kw * gap.total_seconds() / 3600.0

    def as_dict(self) -> dict[str, Any]:
        """Return the guard's state for the diagnostic sensor and debug view."""
        return {
            "state": self._state,
            "threshold_kw": self._threshold_kw,
            "persist_s": int(self._persist.total_seconds()),
            "trip_count": self._trip_count,
            "episode": self._episode.as_dict() if self._episode is not None else None,
            "last_trip": (
                self._last_trip.as_dict() if self._last_trip is not None else None
            ),
            "last_update": _iso(self._last_update),
        }


class SocFloorWatch:
    """Flags a battery still discharging at or below ``min_soc`` under a passive mode.

    ``min_soc`` is only a planning floor; in passive modes the inverter drains to
    its own over-discharge SoC. A breach is recorded (with the lowest SoC seen)
    until the condition clears.
    """

    def __init__(self, discharge_kw: float = SOC_FLOOR_DISCHARGE_KW) -> None:
        """Configure the discharge threshold.

        Args:
            discharge_kw: Battery discharge in kW that counts as draining.
        """
        self._discharge_kw = discharge_kw
        self._since: datetime | None = None
        self._min_soc_seen: float | None = None
        self._breach_count = 0
        self._last_breach: dict[str, Any] | None = None

    @property
    def in_breach(self) -> bool:
        """Return whether a breach is currently in progress."""
        return self._since is not None

    def update(
        self,
        now: datetime,
        commanded: StorageMode | None,
        soc: float | None,
        battery_kw: float | None,
        min_soc: float,
    ) -> str | None:
        """Feed one sample and return ``"breach"`` / ``"recovered"`` on a transition.

        Args:
            now: Sample time.
            commanded: The mode sunSale last commanded, or ``None``.
            soc: Battery SoC (0..1), or ``None`` when unknown.
            battery_kw: Battery power in kW, positive = charging.
            min_soc: The planner's SoC floor (0..1).

        Returns:
            ``"breach"`` when a breach starts, ``"recovered"`` when it ends,
            otherwise ``None``.
        """
        breaching = (
            commanded in PASSIVE_MODES
            and soc is not None
            and battery_kw is not None
            and soc <= min_soc + 1e-9
            and battery_kw <= -self._discharge_kw
        )
        if breaching:
            assert soc is not None
            self._min_soc_seen = (
                soc if self._min_soc_seen is None else min(self._min_soc_seen, soc)
            )
            if self._since is None:
                self._since = now
                self._breach_count += 1
                return "breach"
            return None
        if self._since is not None:
            self._last_breach = {
                "since": _iso(self._since),
                "until": _iso(now),
                "min_soc_seen": self._min_soc_seen,
            }
            self._since = None
            self._min_soc_seen = None
            return "recovered"
        return None

    def as_dict(self) -> dict[str, Any]:
        """Return the watch's state for the diagnostic sensor and debug view."""
        return {
            "in_breach": self.in_breach,
            "since": _iso(self._since),
            "min_soc_seen": self._min_soc_seen,
            "breach_count": self._breach_count,
            "last_breach": dict(self._last_breach) if self._last_breach else None,
        }
