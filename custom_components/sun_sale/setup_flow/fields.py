"""Shared form widgets and field helpers for the setup pages.

Holds the Confirm dropdown every page form ends with, the entity / device
selectors, the source-picker helpers, and ``suggest`` / ``req`` / ``opt`` /
``missing_required``.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

# Every page form ends with this dropdown: HA gives a form one button only, so
# "what Confirm does" is chosen in a selector — save and go back (the default)
# or discard and go back to the window the form came from.
SAVE = "save"
GO_TO = "go_to"

_SAVE_LABEL = "\u2713 save and go back to {parent}"
_DISCARD_LABEL = "\u2715 discard and go back to {parent}"


def next_options(back_to: str, parent: str, save_label: str | None = None) -> list[dict[str, str]]:
    """Return a form's Confirm dropdown options: save, or discard to the parent.

    The discard option's value is the parent window's step id, so the step
    handler can route to it directly; SAVE is the reserved default value.

    Args:
        back_to: Step id of the parent window the form was opened from.
        parent: Its display label, shown in the option text.
        save_label: Text of the SAVE option when Confirm does something other
            than save and go back (e.g. a picker that opens the next form).

    Returns:
        Two ``{"value": ..., "label": ...}`` option dicts.
    """
    return [
        {"value": SAVE, "label": save_label or _SAVE_LABEL.format(parent=parent)},
        {"value": back_to, "label": _DISCARD_LABEL.format(parent=parent)},
    ]


def with_next(schema: vol.Schema, options: list[dict[str, str]]) -> vol.Schema:
    """Return the schema with the Confirm dropdown selector appended.

    Args:
        schema: The page's field schema.
        options: Options from :func:`next_options`.

    Returns:
        The schema with a default ``SAVE`` select field named ``GO_TO``.
    """
    config = SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    return schema.extend({vol.Optional(GO_TO, default=SAVE): SelectSelector(config)})


def list_selector(options: list[dict[str, str]], *, multiple: bool = False) -> SelectSelector:
    """Build a list-mode SelectSelector from a list of option dicts.

    Args:
        options: List of ``{"value": ..., "label": ...}`` dicts.
        multiple: Whether several options may be ticked.

    Returns:
        Configured SelectSelector widget.
    """
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST, multiple=multiple))


def suggest(value: Any) -> dict[str, Any]:
    """Return a field description that pre-fills ``value`` without making it a default.

    HA fills a missing optional field with its ``default``, and a field the user
    clears is submitted as missing — so a ``default`` can never be cleared.
    ``suggested_value`` only pre-fills the widget.
    """
    return {"suggested_value": value} if value not in (None, "", []) else {}


def missing_required(user_input: dict, keys: Any) -> dict[str, str]:
    """Return a ``required`` error for every listed field left blank.

    Args:
        user_input: Submitted form values.
        keys: The fields that must be filled in.

    Returns:
        Mapping of each blank field to ``"required"``; empty when all are set.
    """
    return {key: "required" for key in keys if user_input.get(key) in (None, "", [])}

SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor"))
SENSOR_POWER = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
SENSOR_SOC = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="battery"))
SENSOR_ENERGY = EntitySelector(EntitySelectorConfig(domain="sensor", device_class=["energy", "power"]))
SENSOR_ENERGY_ONLY = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="energy"))
# Any number of forecast sensors — a property can have many arrays.
SENSOR_ENERGY_MULTI = EntitySelector(
    EntitySelectorConfig(domain="sensor", device_class=["energy", "power"], multiple=True)
)
# A price feed: a sensor, or an event entity (Octopus publishes its day rates on those).
PRICE_ENTITY = EntitySelector(EntitySelectorConfig(domain=["sensor", "event"]))
WEATHER = EntitySelector(EntitySelectorConfig(domain="weather"))
SWITCH = EntitySelector(EntitySelectorConfig(domain="switch"))
NUMBER = EntitySelector(EntitySelectorConfig(domain="number"))
SELECT = EntitySelector(EntitySelectorConfig(domain="select"))
ANY_ENTITY = EntitySelector(EntitySelectorConfig())
DEVICE = DeviceSelector(DeviceSelectorConfig())
# A device that publishes a state of charge — a battery pack's BMS. The strict
# test (which of its sensors are usable) runs once a device is picked.
BATTERY_DEVICE = DeviceSelector(DeviceSelectorConfig(entity={"domain": "sensor", "device_class": "battery"}))

# Reserved rows of a detected-source list: OTHER opens the by-hand form, NONE
# means "not set". Entity ids always hold a dot, so neither value can collide
# with one — that is what lets a single field carry both a picked entity and a
# command about what to do next.
OTHER = "other"
NONE = "none"

__all__ = [
    "ANY_ENTITY",
    "BATTERY_DEVICE",
    "DEVICE",
    "GO_TO",
    "NONE",
    "NUMBER",
    "OTHER",
    "PRICE_ENTITY",
    "SAVE",
    "SELECT",
    "SENSOR",
    "SENSOR_ENERGY",
    "SENSOR_ENERGY_MULTI",
    "SENSOR_ENERGY_ONLY",
    "SENSOR_POWER",
    "SENSOR_SOC",
    "SWITCH",
    "WEATHER",
    "detected_options",
    "detected_selector",
    "list_selector",
    "missing_required",
    "next_options",
    "opt",
    "req",
    "suggest",
    "with_next",
]


def detected_options(
    options: list[dict[str, str]], *, other: str = "", none: str = "",
) -> list[dict[str, str]]:
    """Return a detected-source list's rows with the reserved rows appended.

    Args:
        options: The detected rows, best guess first.
        other: Label of the Other row, which opens the by-hand form; omitted
            when blank.
        none: Label of the None row, which clears the setting; omitted when blank.

    Returns:
        The rows in display order: what was detected, then Other, then None.
    """
    rows = list(options)
    if other:
        rows.append({"value": OTHER, "label": other})
    if none:
        rows.append({"value": NONE, "label": none})
    return rows


def detected_selector(
    options: list[dict[str, str]], *, other: str = "", none: str = "", dropdown: bool = False,
) -> SelectSelector:
    """Build the picker for a source sunSale detected, with its reserved rows.

    Args:
        options: The detected rows, best guess first.
        other: Label of the Other row (see :func:`detected_options`).
        none: Label of the None row.
        dropdown: Render as a dropdown rather than a radio list — what a page
            of many one-line sources needs to stay readable.

    Returns:
        A SelectSelector over the detected rows plus the reserved ones.
    """
    rows = detected_options(options, other=other, none=none)
    if dropdown:
        return SelectSelector(SelectSelectorConfig(options=rows, mode=SelectSelectorMode.DROPDOWN))
    return list_selector(rows)


def req(key: str, d: dict, fallback: Any = vol.UNDEFINED) -> vol.Marker:
    """Return a required field, pre-filled when a value is available.

    With a value it is ``vol.Required`` with that default. Without one it is
    ``vol.Optional``: HA's frontend refuses to submit a form while a required
    field is empty, which would also block "discard and go back" — so the step
    checks presence itself with :func:`missing_required`.
    """
    v = d.get(key, fallback)
    return vol.Required(key, default=v) if v is not vol.UNDEFINED else vol.Optional(key)


def opt(key: str, d: dict, fallback: Any = vol.UNDEFINED) -> vol.Optional:
    """Return vol.Optional with a default only when a value is available."""
    v = d.get(key, fallback)
    return vol.Optional(key, default=v) if v is not vol.UNDEFINED else vol.Optional(key)
