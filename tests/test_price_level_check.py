"""Tests for the price-level integration check (``tools/checks/price_level.py``).

The payload is built by the integration's own debug-view serialiser, so a clean
run also proves the check and the debug view agree; each test then corrupts one
exposed claim and asserts the check notices.
"""
from __future__ import annotations

import copy
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.sun_sale.contract.models import PriceLevelConfig, PriceSeries, PriceSlot
from custom_components.sun_sale.orchestration.debug_view import _price_level_block
from custom_components.sun_sale.pipeline.price_level import classify_price_levels
from tools.checks import check_price_level
from tools.checks.snapshot import Snapshot

_TZ = ZoneInfo("Europe/Vilnius")


def _payload() -> dict:
    """Return a debug payload with today + tomorrow's 15-min prices and their levels."""
    start = datetime.combine(datetime.now(_TZ).date(), datetime.min.time(), _TZ).astimezone(UTC)
    step = timedelta(minutes=15)
    slots = tuple(
        PriceSlot(start=start + step * i, end=start + step * (i + 1),
                  buy_eur_kwh=round(0.2 + 0.1 * math.sin(i / 10), 4), sell_eur_kwh=0.0,
                  spot_eur_kwh=0.1, sources=("test",))
        for i in range(192)
    )
    series = PriceSeries(slots=slots, resolution=step, computed_at=start)
    levels = classify_price_levels(series, PriceLevelConfig(cheap_below=0.12), _TZ, start)
    return {
        "config": {"time_zone": "Europe/Vilnius"},
        "pipeline": {
            "pricing": {
                "resolution_s": 900,
                "slots": [{"start": s.start.isoformat(), "buy": s.buy_eur_kwh} for s in slots],
            },
            "price_level": _price_level_block(levels),
        },
    }


def _check(payload: dict):
    """Run the check on a payload."""
    return check_price_level(Snapshot(entry_id="e1", debug=payload))


def test_a_consistent_payload_passes():
    result = _check(_payload())
    assert result.overall_ok, result.mismatches
    assert result.slot_count == 192
    assert result.day_count == 2
    assert sum(result.counts.values()) == 192


def test_skips_without_price_level():
    payload = _payload()
    payload["pipeline"]["price_level"] = None
    assert _check(payload).skipped


def test_flags_a_wrong_slot_level():
    payload = _payload()
    slot = payload["pipeline"]["price_level"]["slots"][40]
    slot["level"] = "expensive" if slot["level"] != "expensive" else "cheap"
    assert not _check(payload).overall_ok


def test_flags_a_wrong_day_threshold():
    payload = _payload()
    payload["pipeline"]["price_level"]["days"][0]["cheap_threshold"] += 0.01
    assert not _check(payload).overall_ok


def test_flags_counts_that_disagree_with_the_slots():
    payload = _payload()
    payload["pipeline"]["price_level"]["counts"]["normal"] += 1
    assert not _check(payload).overall_ok


def test_flags_a_buy_price_that_drifted_from_pricing():
    payload = _payload()
    payload["pipeline"]["pricing"]["slots"][5]["buy"] += 0.05
    assert not _check(payload).overall_ok


def test_flags_a_slot_set_that_differs_from_pricing():
    payload = copy.deepcopy(_payload())
    payload["pipeline"]["price_level"]["slots"].pop()
    assert not _check(payload).overall_ok


def test_flags_a_current_status_that_is_not_the_slot_at_now():
    payload = _payload()
    current = payload["pipeline"]["price_level"]["current"]
    current["level"] = {"cheap": "normal", "normal": "expensive", "expensive": "cheap"}[current["level"]]
    assert not _check(payload).overall_ok
