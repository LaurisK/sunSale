"""Tests for inbound.inverter_discovery — the vendor-pluggable discovery registry."""
from __future__ import annotations

from custom_components.sun_sale.inbound.inverter_discovery import (
    GenericEntityDiscovery,
    InverterEntityDiscovery,
    discovery_for,
    register_discovery,
)
from custom_components.sun_sale.inbound.platform_profiles import ProfileEntityDiscovery
from custom_components.sun_sale.inbound.solis_entity_resolver import SolisEntityDiscovery
from custom_components.sun_sale.outbound.inverter import InverterPlatform


def test_builtin_strategies_satisfy_protocol():
    """Both built-in strategies structurally satisfy the discovery protocol."""
    assert isinstance(GenericEntityDiscovery(), InverterEntityDiscovery)
    assert isinstance(SolisEntityDiscovery(), InverterEntityDiscovery)


def test_discovery_for_selects_registered_and_defaults_to_generic():
    """SOLIS → Solis; entity-driven platforms → profile strategy; rest → generic."""
    assert isinstance(discovery_for(InverterPlatform.SOLIS), SolisEntityDiscovery)
    assert isinstance(discovery_for(InverterPlatform.GENERIC), GenericEntityDiscovery)
    # The five entity-driven platforms each resolve to a profile-backed strategy.
    assert isinstance(discovery_for(InverterPlatform.HUAWEI_SOLAR), ProfileEntityDiscovery)
    # A platform with no registered strategy still falls back to generic.
    assert isinstance(discovery_for(InverterPlatform.SOLAREDGE), GenericEntityDiscovery)


def test_register_discovery_overrides_and_is_used_by_lookup():
    """A newly registered strategy is returned by discovery_for (the extension hook)."""
    class _StubDiscovery:
        def resolve_role_ids(self, hass, data):
            return {"battery_soc": "sensor.stub"}

    original = discovery_for(InverterPlatform.GOODWE)
    try:
        stub = _StubDiscovery()
        register_discovery(InverterPlatform.GOODWE, stub)
        assert discovery_for(InverterPlatform.GOODWE) is stub
        assert isinstance(stub, InverterEntityDiscovery)
    finally:
        # Restore so the module-level registry isn't polluted for other tests.
        register_discovery(InverterPlatform.GOODWE, original)
