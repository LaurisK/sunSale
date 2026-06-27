"""Per-coordinator Snapshot model and the collect() entry point."""
from __future__ import annotations

from dataclasses import dataclass, field

from .client import HAClient


@dataclass
class Snapshot:
    """One coordinator's complete observable state, including raw HA sources."""

    entry_id: str
    debug: dict
    raw_entities: dict[str, dict | None] = field(default_factory=dict)

    @property
    def config(self) -> dict:
        """Return the ``config`` block from the debug payload (or an empty dict)."""
        return self.debug.get("config", {}) or {}

    @property
    def inputs(self) -> dict:
        """Return the ``inputs`` block from the debug payload (or an empty dict)."""
        return self.debug.get("inputs", {}) or {}

    @property
    def pipeline(self) -> dict:
        """Return the ``pipeline`` block from the debug payload (or an empty dict)."""
        return self.debug.get("pipeline", {}) or {}

    @property
    def outputs(self) -> dict:
        """Return the ``outputs`` block from the debug payload (or an empty dict)."""
        return self.debug.get("outputs", {}) or {}


def _tomorrow_eid(entity_id: str) -> str:
    """Derive the ``_tomorrow`` sibling entity ID from a ``_today`` one.

    Args:
        entity_id: Entity ID containing ``_today_`` or ``_today``.

    Returns:
        Modified entity ID, or empty string when no substitution pattern fits.
    """
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "tomorrow"), 1)
    return ""


def _day_eid(entity_id: str, n: int) -> str:
    """Derive the ``_d<n>`` sibling entity ID for day-ahead horizons 2..6.

    Args:
        entity_id: Entity ID containing ``_today_`` or ``_today``.
        n: Day offset (2–6).

    Returns:
        Modified entity ID, or empty string when no substitution pattern fits.
    """
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", f"d{n}"), 1)
    return ""


def _remaining_eid(entity_id: str) -> str:
    """Derive the today-remaining forecast entity ID by substituting 'today' → 'today_remaining'.

    Args:
        entity_id: Entity ID containing '_today_' or '_today' substring.

    Returns:
        Modified entity ID, or empty string if no substitution pattern is found.
    """
    for pattern in ("_today_", "_today"):
        if pattern in entity_id:
            return entity_id.replace(pattern, pattern.replace("today", "today_remaining"), 1)
    return ""


def collect(client: HAClient) -> list[Snapshot]:
    """Build snapshots for every coordinator the integration has registered."""
    snapshots: list[Snapshot] = []
    for entry in client.debug():
        snap = Snapshot(entry_id=entry.get("entry_id", "?"), debug=entry)

        nordpool_eid = snap.config.get("nordpool_entity")
        if nordpool_eid:
            snap.raw_entities[nordpool_eid] = client.state(nordpool_eid)

        # Prefer the runtime-resolved base entities (covers device-based
        # selection); fall back to the legacy entity keys for older payloads.
        base_eids = list(snap.config.get("solar_forecast_base_entities") or [])
        if not base_eids:
            base_eids = [
                e
                for e in (
                    snap.config.get("solar_forecast_entity"),
                    snap.config.get("solar_forecast_entity_2"),
                )
                if e
            ]
        for eid in base_eids:
            snap.raw_entities[eid] = client.state(eid)
            t_eid = _tomorrow_eid(eid)
            if t_eid:
                snap.raw_entities[t_eid] = client.state(t_eid)
            r_eid = _remaining_eid(eid)
            if r_eid:
                snap.raw_entities[r_eid] = client.state(r_eid)
            for n in range(2, 7):
                d_eid = _day_eid(eid, n)
                if d_eid:
                    snap.raw_entities[d_eid] = client.state(d_eid)

        snapshots.append(snap)
    return snapshots
