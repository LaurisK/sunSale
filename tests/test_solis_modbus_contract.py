"""Contract guard against upstream ``solis_modbus`` changes.

sunSale's Remote Dispatch path calls another integration's services and reads
another integration's entities. Neither is a versioned API: a rename in
``solis_modbus`` would not fail to import, would not fail a unit test written
against a mock, and would not raise at runtime — the service call would simply
be rejected and swallowed by the "log, don't propagate" rule, leaving a battery
that quietly stops discharging. That failure mode is why this file exists.

It parses upstream's own source — ``services.yaml`` for the service surface and
``const.py`` for the dispatch mode names — and asserts every name and field
sunSale sends is still there. Pointed at a local checkout of the integration
(``SOLIS_MODBUS_PATH``, defaulting to a sibling clone); **skipped when that
checkout is absent**, so a fresh clone of sunSale alone still has a green suite.
In CI, check out ``Pho3niX90/solis_modbus`` at both ``master`` and the latest
release tag and run this file — an upstream rename then breaks the build rather
than the hardware.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# Only the dedicated CI job installs pyyaml; the main suite must still collect
# this module cleanly, so a missing parser skips rather than errors.
yaml = pytest.importorskip("yaml")

from custom_components.sun_sale.outbound.solis_dispatch_driver import (  # noqa: E402
    SERVICE_DISPATCH,
    SERVICE_DISPATCH_STOP,
)

# Every field name sunSale puts in a ``solis_dispatch`` payload. Kept explicit
# rather than derived from the driver so that dropping a field from the driver
# without dropping it here is itself a visible change.
_DISPATCH_FIELDS = frozenset({"mode", "power_watts", "failsafe_minutes"})

# The dispatch mode names sunSale asks for, from ``_DISPATCH_MODES``.
_DISPATCH_MODE_NAMES = frozenset({"grid_export", "grid_import"})

# Unique-id slugs of the readback sensors the verify loop compares against;
# these are matched by ``inbound/solis_entity_resolver.py``.
_SENSOR_SLUGS = frozenset({
    "solis_modbus_inverter_remote_dispatch_capability",
    "solis_modbus_inverter_dispatch_active",
    "solis_modbus_inverter_dispatch_control_mode",
    "solis_modbus_inverter_dispatch_power_target",
    "solis_modbus_inverter_dispatch_failsafe_interval",
})

_DEFAULT_PATH = Path.home() / "Projects" / "HomeAssistant" / "solis_modbus"


def _upstream() -> Path:
    """Return the solis_modbus checkout root, or skip when unavailable."""
    root = Path(os.environ.get("SOLIS_MODBUS_PATH", _DEFAULT_PATH))
    component = root / "custom_components" / "solis_modbus"
    if not component.is_dir():
        pytest.skip(
            f"solis_modbus checkout not found at {root} — set SOLIS_MODBUS_PATH "
            "to run the upstream contract guard",
        )
    return component


@pytest.fixture(scope="module")
def services() -> dict:
    """Return upstream's parsed ``services.yaml``."""
    return yaml.safe_load((_upstream() / "services.yaml").read_text())


@pytest.fixture(scope="module")
def const_source() -> str:
    """Return upstream's ``const.py`` and ``__init__.py`` concatenated.

    The dispatch constants have moved between the two across releases, so both
    are searched — the guard is about the *names* still existing, not about
    which module currently holds them.
    """
    root = _upstream()
    return (root / "const.py").read_text() + (root / "__init__.py").read_text()


def test_dispatch_services_still_exist(services: dict) -> None:
    for name in (SERVICE_DISPATCH, SERVICE_DISPATCH_STOP):
        assert name in services, (
            f"solis_modbus no longer declares '{name}'. sunSale's Remote "
            "Dispatch path calls it directly — either upstream renamed it or "
            "the checkout predates it."
        )


def test_dispatch_accepts_every_field_sunsale_sends(services: dict) -> None:
    declared = set(services[SERVICE_DISPATCH].get("fields") or {})
    missing = _DISPATCH_FIELDS - declared
    assert not missing, (
        f"solis_dispatch no longer accepts {sorted(missing)}. A rejected field "
        "does not raise through sunSale's write path — it is logged and "
        "swallowed — so this would silently stop the forced modes."
    )


def test_failsafe_range_still_covers_the_window_sunsale_asks_for(
    services: dict,
) -> None:
    """The failsafe sunSale writes must be inside upstream's accepted range."""
    from custom_components.sun_sale.outbound.solis_dispatch_driver import (
        _FAILSAFE_MINUTES,
    )

    field = (services[SERVICE_DISPATCH].get("fields") or {}).get(
        "failsafe_minutes", {},
    )
    selector = (field.get("selector") or {}).get("number") or {}
    low = selector.get("min")
    high = selector.get("max")
    if low is not None:
        assert _FAILSAFE_MINUTES >= low
    if high is not None:
        assert _FAILSAFE_MINUTES <= high, (
            f"sunSale asks for a {_FAILSAFE_MINUTES}-minute failsafe but "
            f"solis_dispatch now caps it at {high}."
        )


def test_dispatch_mode_names_still_exist(const_source: str) -> None:
    """The mode strings sunSale passes must still be keys of ``DISPATCH_MODES``."""
    block = re.search(
        r"DISPATCH_MODES\s*=\s*\{(.*?)\}", const_source, re.DOTALL,
    )
    assert block, "DISPATCH_MODES no longer found in solis_modbus"
    declared = set(re.findall(r'"([a-z_]+)"\s*:', block.group(1)))
    missing = _DISPATCH_MODE_NAMES - declared
    assert not missing, (
        f"solis_modbus's DISPATCH_MODES no longer defines {sorted(missing)}. "
        "sunSale passes these by name; an unknown mode raises inside the "
        "service and is swallowed by the write path."
    )


def test_dispatch_capability_gate_is_unchanged(const_source: str) -> None:
    """sunSale gates on register 34502 reading 0xAA55; upstream must agree."""
    from custom_components.sun_sale.outbound.solis_dispatch_driver import (
        DISPATCH_CAPABLE_MAGIC,
    )

    assert "DISPATCH_CAPABILITY_REG = 34502" in const_source
    assert f"DISPATCH_CAPABLE_MAGIC = {DISPATCH_CAPABLE_MAGIC:#x}" in const_source.lower() \
        or "DISPATCH_CAPABLE_MAGIC = 0xAA55" in const_source


def test_readback_sensor_slugs_still_exist() -> None:
    """The verify loop's readback sensors must still be declared upstream.

    Resolution is by unique-id slug (``inbound/solis_entity_resolver.py``), so a
    renamed slug silently yields an unmapped role — the dispatch rows would go
    permanently ``unknown`` and the verify loop could never confirm a forced
    mode.
    """
    source = (_upstream() / "sensor_data" / "hybrid_sensors.py").read_text()
    missing = [slug for slug in sorted(_SENSOR_SLUGS) if slug not in source]
    assert not missing, (
        f"solis_modbus no longer declares the readback sensors {missing}; "
        "sunSale resolves them by unique-id slug."
    )


def test_number_write_still_short_circuits_on_equal_values() -> None:
    """The premise behind ``EntityActuator.set_number(renew=True)``.

    sunSale nudges a neighbouring value before re-writing an unchanged one
    because upstream drops the no-op write. If upstream ever stops doing that,
    the nudge becomes dead weight worth removing — so pin the premise rather
    than let it rot silently.
    """
    source = (_upstream() / "sensors" / "solis_number_sensor.py").read_text()
    body = source[source.index("def set_native_value"):]
    assert "if self._attr_native_value == value:" in body, (
        "solis_modbus's number write no longer short-circuits on an unchanged "
        "value — EntityActuator's `renew` nudge may now be unnecessary."
    )
