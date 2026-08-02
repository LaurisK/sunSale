"""Shared HA-edge state-reading and unit-normalisation helpers.

Every translator and the inverter controller must turn a Home Assistant
entity state into a usable number while guarding the same set of
"not-a-reading" cases — the entity is unmapped, missing from the state
machine, or carrying ``unavailable`` / ``unknown`` / a blank string — and
then rescale the raw value from whatever ``unit_of_measurement`` the sensor
publishes into the canonical internal unit (kW for power, kWh for energy).
These helpers centralise that boilerplate so each reader expresses only its
intent instead of re-implementing the same eight-line guard.

The module deliberately imports nothing from the sunSale package (and types
the ``hass`` handle as ``Any``) so it can be used from every layer —
``inbound``, ``outbound`` and ``orchestration`` — without creating an import
cycle.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import State

# Unit → kW / kWh multipliers. An empty or unrecognised unit maps to the
# canonical internal unit (kW / kWh), so configs that already publish the
# internal unit need no mapping.
_POWER_UNIT_SCALERS: dict[str, float] = {
    "W": 1.0 / 1000.0,
    "mW": 1.0 / 1_000_000.0,
    "kW": 1.0,
    "MW": 1000.0,
}

_ENERGY_UNIT_SCALERS: dict[str, float] = {
    "Wh": 1.0 / 1000.0,
    "kWh": 1.0,
    "MWh": 1000.0,
}

# State strings that mean "no usable reading this cycle".
_UNUSABLE_STATES = ("unavailable", "unknown", "")


def normalize_power_to_kw(value: float, unit: str) -> float:
    """Rescale a power value to kW based on its ``unit_of_measurement``.

    Treats an empty or unrecognised unit as kW (the canonical internal
    unit), so configs that already return kW remain unaffected.

    Args:
        value: Raw sensor value as published by HA.
        unit: HA ``unit_of_measurement`` attribute (e.g. "W", "kW").

    Returns:
        Value expressed in kW.
    """
    scaler = _POWER_UNIT_SCALERS.get(unit.strip())
    if scaler is None:
        return value
    return value * scaler


def power_unit_scale(unit: str) -> float:
    """Return the multiplier that converts a power ``unit`` to kW.

    Mirrors ``normalize_power_to_kw``'s unit handling but exposes just the
    scalar, so a caller that reads a raw sensor value elsewhere (e.g. the
    panel, via a server-published spec) can apply ``value * scale`` itself.
    An empty or unrecognised unit maps to ``1.0`` (treated as already-kW),
    matching ``normalize_power_to_kw``.

    Args:
        unit: HA ``unit_of_measurement`` attribute (e.g. "W", "kW").

    Returns:
        kW-per-unit multiplier (``1.0`` for unknown units).
    """
    return _POWER_UNIT_SCALERS.get(unit.strip(), 1.0)


def normalize_energy_to_kwh(value: float, unit: str) -> float:
    """Rescale a cumulative-energy value to kWh based on its unit.

    An empty or unrecognised unit is treated as kWh (the canonical internal
    unit); a ``Wh`` / ``MWh`` sensor is rescaled.

    Args:
        value: Raw counter value as published by HA.
        unit: HA ``unit_of_measurement`` attribute (e.g. "Wh", "kWh").

    Returns:
        Value expressed in kWh.
    """
    return value * _ENERGY_UNIT_SCALERS.get(unit.strip(), 1.0)


def available_state(hass: Any, entity_id: str) -> State | None:
    """Return the entity's HA ``State`` when it carries a usable value.

    Guards the cases every HA-edge reader must reject before trusting a
    state: an unmapped entity (empty ``entity_id``), an entity absent from
    the state machine, and the ``unavailable`` / ``unknown`` / blank
    sentinel states. Callers that also need the entity's attributes (unit,
    ``last_updated``) use this and parse the value themselves; callers that
    only need a number use ``read_float_state`` / ``read_power_kw``.

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID to read; an empty string yields ``None``.

    Returns:
        The entity ``State`` object, or ``None`` when no usable state exists.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNUSABLE_STATES:
        return None
    return state


def read_float_state(hass: Any, entity_id: str) -> float | None:
    """Return an entity's numeric state, or ``None`` when not a usable number.

    Combines ``available_state`` with a tolerant ``float`` parse, collapsing
    the get-state / guard / ``try float`` idiom into one call.

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID to read.

    Returns:
        The state parsed as ``float``, or ``None`` when the entity is
        unmapped, unavailable, or non-numeric.
    """
    state = available_state(hass, entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def read_power_kw(
    hass: Any,
    entity_id: str,
    *,
    now: datetime | None = None,
    max_age_s: float = float("inf"),
) -> float | None:
    """Read a power sensor and normalise it to kW, honouring a freshness window.

    Reads the sensor's ``unit_of_measurement`` and rescales to kW (W → /1000,
    MW → ×1000, kW / missing → as-is), since vendors are inconsistent about
    the published unit. When ``max_age_s`` is finite, a reading whose
    ``last_updated`` is older than the window is rejected as stale; a missing
    or naive ``last_updated`` skips the staleness check (the reading is kept).

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID of the power sensor.
        now: Reference instant for the freshness check; defaults to UTC now.
            Ignored when ``max_age_s`` is infinite.
        max_age_s: Maximum age (seconds) of ``state.last_updated`` for the
            reading to be considered fresh. ``float('inf')`` disables the
            check.

    Returns:
        Power in kW, or ``None`` when the entity is unmapped, unavailable,
        non-numeric, or staler than ``max_age_s``.
    """
    state = available_state(hass, entity_id)
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if max_age_s != float("inf"):
        last_updated = getattr(state, "last_updated", None)
        if last_updated is not None and last_updated.tzinfo is not None:
            ref = now or datetime.now(UTC)
            if (ref - last_updated).total_seconds() > max_age_s:
                return None
    unit = str((state.attributes or {}).get("unit_of_measurement") or "").strip()
    return normalize_power_to_kw(value, unit)


def read_soc_fraction(hass: Any, entity_id: str) -> float | None:
    """Read a state-of-charge sensor and normalise to a 0.0–1.0 fraction.

    A sensor carrying a ``%`` unit is divided by 100 unconditionally; a
    unit-less sensor falls back to the magnitude heuristic (divide only when
    the value exceeds 1.0). This mirrors the SoC normalisation
    ``InverterController`` applies, so a BMS SoC and an inverter SoC are read
    on the same convention.

    Args:
        hass: Home Assistant instance.
        entity_id: Entity ID of the SoC sensor; empty yields ``None``.

    Returns:
        SoC as a 0.0–1.0 fraction, or ``None`` when the entity is unmapped,
        unavailable, or non-numeric.
    """
    state = available_state(hass, entity_id)
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    # A "%" sensor is always 0–100, so divide unconditionally: this is the only
    # way to read an exact 1 % correctly — the magnitude heuristic would mistake
    # it for the fraction 1.0 = 100 %. Unit-less SoC sensors keep the heuristic:
    # a value above 1.0 must be a percentage, anything ≤ 1.0 is taken as an
    # already-normalised fraction (genuinely ambiguous at exactly 1.0).
    unit = str((state.attributes or {}).get("unit_of_measurement") or "").strip()
    if unit == "%" or value > 1.0:
        return value / 100.0
    return value
