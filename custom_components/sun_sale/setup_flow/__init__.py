"""The setup menu tree shared by the config flow and the options flow.

``config_flow.py`` binds :class:`SetupHub` to Home Assistant; everything the
user sees while adding or reconfiguring the integration lives here:

- ``fields`` — shared selectors and field helpers (pre-fill, ``← Back`` toggle).
- ``prices`` / ``tariffs`` / ``inverter`` / ``forecast`` — each section's form
  schemas, validators and defaults (pure, no flow state).
- ``sections`` — the section and page ids of the menu tree and their labels.
- ``core`` — :class:`HubCore`: flow state, status lines and menu navigation.
- ``price_steps`` / ``inverter_steps`` — each section's step handlers, as
  mixins over :class:`HubCore`.
- ``hub`` — :class:`SetupHub`: the mixins composed, plus the root rows and Finish.
"""
from __future__ import annotations

from .hub import SetupHub
from .inverter import IMPLEMENTED_INVERTER_PLATFORMS, INVERTER_PLATFORMS

__all__ = ["IMPLEMENTED_INVERTER_PLATFORMS", "INVERTER_PLATFORMS", "SetupHub"]
