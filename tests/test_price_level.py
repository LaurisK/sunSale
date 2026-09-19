"""Tests for pipeline/price_level.py — the cheap / normal / expensive classification."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.sun_sale.contract.const import (
    CONF_PRICE_LEVEL_CHEAP_BELOW,
    CONF_PRICE_LEVEL_CHEAP_SHARE,
    CONF_PRICE_LEVEL_EXPENSIVE_ABOVE,
    CONF_PRICE_LEVEL_EXPENSIVE_SHARE,
)
from custom_components.sun_sale.contract.models import PriceLevel, PriceLevelConfig, PriceSeries, PriceSlot
from custom_components.sun_sale.pipeline.price_level import (
    classify_price_levels,
    config_from_entry,
    next_start_of,
    price_level_status,
)

_T0 = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
_CHEAP, _NORMAL, _EXPENSIVE = PriceLevel.CHEAP, PriceLevel.NORMAL, PriceLevel.EXPENSIVE


def _series(buys: list[float], start: datetime = _T0, minutes: int = 60) -> PriceSeries:
    """Return a price series of consecutive slots with these buy prices."""
    step = timedelta(minutes=minutes)
    slots = tuple(
        PriceSlot(start=start + step * i, end=start + step * (i + 1), buy_eur_kwh=b,
                  sell_eur_kwh=0.0, spot_eur_kwh=b, sources=("test",))
        for i, b in enumerate(buys)
    )
    return PriceSeries(slots=slots, resolution=step, computed_at=start)


def _classify(buys: list[float], config: PriceLevelConfig = PriceLevelConfig(), tz=UTC):
    """Classify a series built from ``buys``."""
    return classify_price_levels(_series(buys), config, tz, _T0)


def test_default_shares_mark_the_cheapest_and_dearest_quarter_of_the_day():
    result = _classify([float(p) for p in range(1, 25)])
    levels = [s.level for s in result.slots]
    assert levels == [_CHEAP] * 6 + [_NORMAL] * 12 + [_EXPENSIVE] * 6
    day = result.days[0]
    assert (day.slot_count, day.cheap_threshold, day.expensive_threshold) == (24, 6.0, 19.0)


def test_a_flat_priced_day_is_all_normal():
    """Every slot is in both shares — no spread, nothing to shift towards."""
    assert {s.level for s in _classify([0.2] * 24).slots} == {_NORMAL}


def test_a_tou_shoulder_band_is_not_labelled_expensive():
    """Regression (live): 07–17 tied at the 25 % boundary with only 2 of 10 h inside it.

    Hourly TOU day: 00–07 at 0.08, 07–17 at 0.20, 17–21 at 0.35, 21–24 at 0.15.
    The dearest quarter (6 h) covers the 4 h peak plus 2 h of the shoulder — the
    shoulder must stay normal. The cheap night band (7 h, 6 of them inside the
    cheapest quarter) is mostly inside, so all of it is cheap.
    """
    day = [0.08] * 7 + [0.20] * 10 + [0.35] * 4 + [0.15] * 3
    levels = [s.level for s in _classify(day).slots]
    assert levels == [_CHEAP] * 7 + [_NORMAL] * 10 + [_EXPENSIVE] * 4 + [_NORMAL] * 3


def test_a_boundary_tie_group_mostly_outside_the_share_is_left_out():
    """Night band of 10 h at the cheapest price: only 6 of 10 inside → still cheap (≥ half)."""
    levels = [s.level for s in _classify([0.1] * 10 + [float(p) for p in range(1, 15)]).slots]
    assert levels[:10] == [_CHEAP] * 10
    # 20 h tied: 6 inside the cheapest quarter is under half → none of them cheap.
    levels = [s.level for s in _classify([0.1] * 20 + [1.0, 2.0, 3.0, 4.0]).slots]
    assert _CHEAP not in levels


def test_absolute_limits_override_the_rank():
    config = PriceLevelConfig(cheap_below=10.0, expensive_above=12.0)
    levels = [s.level for s in _classify([float(p) for p in range(1, 25)], config).slots]
    assert levels[9] is _CHEAP          # 10.0 — ranked normal, cheap by limit
    assert levels[10] is _NORMAL        # 11.0 — between the limits, ranked normal
    assert levels[11] is _EXPENSIVE     # 12.0 — ranked normal, expensive by limit


def test_a_zero_share_disables_that_rank_class():
    result = _classify([float(p) for p in range(1, 25)], PriceLevelConfig(cheap_share=0.0))
    assert _CHEAP not in {s.level for s in result.slots}
    assert result.days[0].cheap_threshold is None


def test_days_are_local_days_not_utc_days():
    """48 hourly slots from local midnight in Vilnius (UTC+3) → two full local days."""
    tz = ZoneInfo("Europe/Vilnius")
    start = datetime(2026, 9, 14, tzinfo=tz).astimezone(UTC)
    result = classify_price_levels(_series([float(i % 24) for i in range(48)], start), PriceLevelConfig(), tz, start)
    assert [(d.date.isoformat(), d.slot_count) for d in result.days] == [("2026-09-14", 24), ("2026-09-15", 24)]
    assert result.slots[0].level is _CHEAP  # local midnight is each local day's cheapest hour


def test_rank_runs_from_cheapest_to_dearest():
    ranks = [s.rank for s in _classify([3.0, 1.0, 2.0]).slots]
    assert ranks == [1.0, 0.0, 0.5]


def test_status_reports_level_thresholds_and_next_change():
    result = _classify([float(p) for p in range(1, 25)])
    status = price_level_status(result, _T0 + timedelta(minutes=30))
    assert status["level"] == "cheap"
    assert status["rank_pct"] == 0
    assert (status["cheap_threshold"], status["expensive_threshold"]) == (6.0, 19.0)
    assert status["next_change"] == (_T0 + timedelta(hours=6)).isoformat()
    assert status["next_level"] == "normal"
    assert next_start_of(result, _T0, _EXPENSIVE) == _T0 + timedelta(hours=18)
    assert next_start_of(result, _T0, _CHEAP) is None
    assert price_level_status(result, _T0 - timedelta(hours=1)) is None


def test_config_from_entry_defaults_and_percentages():
    assert config_from_entry({}) == PriceLevelConfig(0.25, 0.25, None, None)
    cfg = config_from_entry({
        CONF_PRICE_LEVEL_CHEAP_SHARE: 30, CONF_PRICE_LEVEL_EXPENSIVE_SHARE: 10,
        CONF_PRICE_LEVEL_CHEAP_BELOW: 0.05, CONF_PRICE_LEVEL_EXPENSIVE_ABOVE: None,
    })
    assert cfg == PriceLevelConfig(0.30, 0.10, 0.05, None)
