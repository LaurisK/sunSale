"""What a sunSale installation has — the setup checklist's capability flags.

The config / options flow opens with a checklist (prices, inverter, …) whose
answers are stored under ``CONF_INSTALL_CAPABILITIES``. The hub reads them to
decide which setup sections exist; the coordinator reads them to decide which
optional seams (e.g. the price feed) to build.

Entries created before the checklist have no such key. They are inferred from
what the entry already holds, so every existing install keeps exactly its
current behaviour with no config-entry migration.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import CONF_INSTALL_CAPABILITIES

CAP_PRICES = "prices"
CAP_INVERTER = "inverter"
CAP_SOLAR_FORECAST = "solar_forecast"

INSTALL_CAPABILITY_KEYS = (CAP_PRICES, CAP_INVERTER, CAP_SOLAR_FORECAST)


def resolve_install_capabilities(config: Mapping[str, Any]) -> dict[str, bool]:
    """Return the capability flags for a config dict, inferring legacy entries.

    Args:
        config: Merged config-entry data + options (or a flow's accumulator).

    Returns:
        A dict with every key in ``INSTALL_CAPABILITY_KEYS``. A stored
        ``CONF_INSTALL_CAPABILITIES`` dict wins (missing keys read ``False``);
        without one every capability is on, which is what an entry written
        before the checklist already had.
    """
    stored = config.get(CONF_INSTALL_CAPABILITIES)
    if isinstance(stored, Mapping):
        return {key: bool(stored.get(key, False)) for key in INSTALL_CAPABILITY_KEYS}
    return dict.fromkeys(INSTALL_CAPABILITY_KEYS, True)
