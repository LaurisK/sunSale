"""Tests for tariff.py — pure Python, no HA required."""
from datetime import UTC, date, datetime

import pytest

from custom_components.sun_sale.contract.const import (
    CONF_PRICE_BUY,
    CONF_PRICE_SOURCE,
    CONF_PRICE_TOU_BANDS,
    CONF_TARIFF_DISTRIBUTION_FEE,
    CONF_TARIFF_FIXED_SELL_PRICE,
    CONF_TARIFF_MARKUP,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE,
    CONF_TARIFF_SELL_MARKUP,
    CONF_TARIFF_SELL_MODE,
    CONF_TARIFF_SELL_TAX_RATE,
    CONF_TARIFF_TAX_RATE,
    CONF_TARIFF_WEEKDAY_BANDS,
    CONF_TARIFF_WEEKEND_BANDS,
)
from custom_components.sun_sale.contract.models import DaySchedule, PriceFormula, TariffConfig, TariffSwitch
from custom_components.sun_sale.pipeline.tariff import (
    active_tariff,
    buy_price,
    formula_from_config,
    legacy_price_config,
    needs_market_price,
    price_config,
    schedule_keys,
    season_of,
    sell_price,
    tariff_from_config,
)
from tests.conftest import default_tariff_config, flat_tariff

# 2024-01-15 is a Monday, 2024-01-20 a Saturday, 2024-07-15 a Monday in summer.
MON = datetime(2024, 1, 15, tzinfo=UTC)
SAT = datetime(2024, 1, 20, tzinfo=UTC)
SUMMER_MON = datetime(2024, 7, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------

def test_buy_price_basic():
    # (0.05 + 0.01 markup + 0.03 grid fee) * 1.21
    assert buy_price(0.05, default_tariff_config()) == pytest.approx(0.1089)


def test_sell_price_basic():
    # (0.15 - 0.005 deduction - 0.02 grid fee) * (1 - 0)
    assert sell_price(0.15, default_tariff_config()) == pytest.approx(0.125)


def test_buy_price_zero_and_negative_spot():
    config = default_tariff_config()
    assert buy_price(0.0, config) == pytest.approx(0.0484)
    assert buy_price(-0.05, config) == pytest.approx(-0.0121)


def test_sell_price_negative_spot():
    assert sell_price(-0.10, default_tariff_config()) < 0


def test_buy_always_greater_than_sell_for_same_spot():
    config = default_tariff_config()
    for spot in [-0.05, 0.0, 0.05, 0.10, 0.20, 0.50]:
        assert buy_price(spot, config) > sell_price(spot, config), f"buy should exceed sell at spot={spot}"


def test_sell_tax_is_deducted():
    config = flat_tariff(sell_fee=0.02, sell_tax=0.05, sell_markup=0.005)
    assert sell_price(0.10, config) == pytest.approx((0.10 - 0.02 - 0.005) * 0.95)


def test_fixed_energy_ignores_the_market_price():
    config = TariffConfig(
        buy=PriceFormula(energy_mode="fixed", fixed_price=0.20, markup=0.01, vat=0.21, grid_fees=(0.05,)),
        sell=PriceFormula(energy_mode="fixed", fixed_price=0.08),
    )
    for spot in (-0.5, 0.0, 0.9):
        assert buy_price(spot, config) == pytest.approx((0.20 + 0.01 + 0.05) * 1.21)
        assert sell_price(spot, config) == pytest.approx(0.08)


def test_dynamic_sell_prefers_the_separate_export_price():
    config = flat_tariff(sell_fee=0.01, sell_markup=0.0)
    assert sell_price(0.99, config, export=0.07) == pytest.approx(0.06)
    assert sell_price(0.99, config, export=None) == pytest.approx(0.98)


def test_fixed_sell_ignores_the_export_price():
    config = TariffConfig(sell=PriceFormula(energy_mode="fixed", fixed_price=0.08))
    assert sell_price(0.99, config, export=0.07) == 0.08


def test_needs_market_price_when_either_side_is_dynamic():
    fixed = PriceFormula(energy_mode="fixed")
    assert needs_market_price(default_tariff_config())
    assert not needs_market_price(TariffConfig(buy=fixed, sell=fixed))
    assert needs_market_price(TariffConfig(buy=fixed, sell=PriceFormula()))


# ---------------------------------------------------------------------------
# Grid-fee tariffs and their schedules
# ---------------------------------------------------------------------------

_DAY_NIGHT = DaySchedule("all", "workday", (TariffSwitch(7 * 60, 1), TariffSwitch(23 * 60, 2)))


def _two_tariffs(schedules=(_DAY_NIGHT,), **fields) -> PriceFormula:
    """Return T1 0.06 / T2 0.02 with a 07:00 day, 23:00 night workday schedule."""
    return PriceFormula(grid_fees=(0.06, 0.02), schedules=schedules, **fields)


def test_active_tariff_follows_the_switch_points():
    formula = _two_tariffs()
    assert active_tariff(formula, MON.replace(hour=10)) == 1
    assert active_tariff(formula, MON.replace(hour=23, minute=30)) == 2


def test_active_tariff_wraps_past_midnight():
    # 02:00 is before the 07:00 switch → the previous day's 23:00 tariff carries over.
    assert active_tariff(_two_tariffs(), MON.replace(hour=2)) == 2


def test_without_a_local_time_or_with_one_tariff_t1_applies():
    assert active_tariff(_two_tariffs(), None) == 1
    assert active_tariff(PriceFormula(grid_fees=(0.05,)), MON.replace(hour=2)) == 1


def test_weekends_follow_the_workday_schedule_unless_separated():
    assert active_tariff(_two_tariffs(), SAT.replace(hour=12)) == 1
    separate = _two_tariffs(
        weekends=True, schedules=(_DAY_NIGHT, DaySchedule("all", "weekend", (TariffSwitch(0, 2),))),
    )
    assert active_tariff(separate, SAT.replace(hour=12)) == 2
    assert active_tariff(separate, MON.replace(hour=12)) == 1


def test_a_day_type_without_a_schedule_gets_t1():
    assert active_tariff(_two_tariffs(weekends=True), SAT.replace(hour=2)) == 1


def test_holiday_schedule_wins_over_the_weekend_and_needs_a_calendar():
    formula = PriceFormula(grid_fees=(0.4, 0.3, 0.2, 0.1), weekends=True, holidays=True, schedules=(
        DaySchedule("all", "workday", (TariffSwitch(0, 1),)),
        DaySchedule("all", "weekend", (TariffSwitch(0, 2),)),
        DaySchedule("all", "holiday", (TariffSwitch(0, 3),)),
    ))
    holiday = {date(2024, 1, 15), date(2024, 1, 20)}.__contains__
    assert active_tariff(formula, MON, holiday) == 3
    assert active_tariff(formula, SAT, holiday) == 3
    assert active_tariff(formula, SAT, None) == 2
    assert active_tariff(formula, MON, None) == 1


def test_holidays_follow_their_weekday_unless_separated():
    assert active_tariff(_two_tariffs(), MON.replace(hour=10), lambda day: True) == 1


def test_seasons_pick_the_summer_or_winter_schedule():
    # The day zone starts an hour later in summer (a DST-shifted zone, as in Lithuania).
    formula = PriceFormula(grid_fees=(0.06, 0.02), seasons=True, schedules=(
        DaySchedule("summer", "workday", (TariffSwitch(0, 2), TariffSwitch(8 * 60, 1))),
        DaySchedule("winter", "workday", (TariffSwitch(0, 2), TariffSwitch(7 * 60, 1))),
    ))
    assert active_tariff(formula, MON.replace(hour=7, minute=30)) == 1
    assert active_tariff(formula, SUMMER_MON.replace(hour=7, minute=30)) == 2


def test_season_starts_are_inclusive():
    formula = PriceFormula(seasons=True, summer_start=(4, 1), winter_start=(11, 1))
    assert season_of(formula, date(2024, 3, 31)) == "winter"
    assert season_of(formula, date(2024, 4, 1)) == "summer"
    assert season_of(formula, date(2024, 10, 31)) == "summer"
    assert season_of(formula, date(2024, 11, 1)) == "winter"


def test_a_summer_across_the_new_year_wraps():
    formula = PriceFormula(seasons=True, summer_start=(10, 1), winter_start=(4, 1))
    assert season_of(formula, date(2024, 1, 15)) == "summer"
    assert season_of(formula, date(2024, 6, 15)) == "winter"


def test_without_seasons_every_day_is_all():
    assert season_of(PriceFormula(), date(2024, 7, 1)) == "all"


def test_buy_and_sell_use_their_own_active_fee():
    config = TariffConfig(buy=_two_tariffs(markup=0.01, vat=0.21), sell=_two_tariffs(markup=0.005))
    night = MON.replace(hour=23, minute=30)
    assert buy_price(0.05, config, night) == pytest.approx((0.05 + 0.02 + 0.01) * 1.21)
    assert sell_price(0.15, config, night) == pytest.approx(0.15 - 0.02 - 0.005)


# ---------------------------------------------------------------------------
# Stored formula dicts
# ---------------------------------------------------------------------------

def test_schedule_keys_follow_the_structure():
    assert schedule_keys({"tariffs": 1, "weekends": True}) == []
    assert schedule_keys({"tariffs": 2}) == ["all_workday"]
    assert schedule_keys({"tariffs": 4, "weekends": True, "holidays": True}) == [
        "all_workday", "all_weekend", "all_holiday",
    ]
    assert schedule_keys({"tariffs": 2, "seasons": True, "holidays": True}) == [
        "summer_workday", "summer_holiday", "winter_workday", "winter_holiday",
    ]


def test_formula_from_config_parses_the_stored_dict():
    formula = formula_from_config({
        "energy": "fixed", "fixed_price": 0.2, "markup": 0.01, "vat": 21, "tariffs": 2, "grid_fees": [0.06],
        "schedules": {
            "all_workday": [
                {"start": "23:00", "tariff": 2},
                {"start": "7:00", "tariff": 1},
                {"start": "bad", "tariff": 1},
                {"start": "12:00", "tariff": 3},  # no T3 with two tariffs
            ],
            "all_weekend": [{"start": "00:00", "tariff": 2}],  # weekends are not separated
        },
    })
    assert (formula.energy_mode, formula.fixed_price, formula.markup) == ("fixed", 0.2, 0.01)
    assert formula.vat == pytest.approx(0.21)
    assert formula.grid_fees == (0.06, 0.0)
    assert formula.schedules == (_DAY_NIGHT,)


def test_formula_from_config_defaults():
    assert formula_from_config(None) == PriceFormula()


# ---------------------------------------------------------------------------
# Legacy entries (flat fees, fee bands, sell modes, TOU source)
# ---------------------------------------------------------------------------

_LEGACY = {
    CONF_TARIFF_DISTRIBUTION_FEE: 0.03,
    CONF_TARIFF_TAX_RATE: 21.0,
    CONF_TARIFF_MARKUP: 0.01,
    CONF_TARIFF_SELL_DISTRIBUTION_FEE: 0.02,
    CONF_TARIFF_SELL_TAX_RATE: 5.0,
    CONF_TARIFF_SELL_MARKUP: 0.005,
}


def test_legacy_flat_tariff_keeps_its_prices():
    config = tariff_from_config(_LEGACY)
    assert buy_price(0.10, config, MON) == pytest.approx((0.10 + 0.03 + 0.01) * 1.21)
    assert sell_price(0.10, config, MON) == pytest.approx((0.10 - 0.02 - 0.005) * 0.95)
    assert config.buy.grid_fees == (0.03,)


def test_legacy_fee_bands_become_tariffs_and_keep_their_prices():
    data = {
        **_LEGACY,
        CONF_TARIFF_WEEKDAY_BANDS: [
            {"start": "07:00", "buy_fee": 0.06, "sell_fee": 0.01},
            {"start": "23:00", "buy_fee": 0.02, "sell_fee": 0.0},
        ],
        CONF_TARIFF_WEEKEND_BANDS: [],  # weekends used the flat fees
    }
    config = tariff_from_config(data)
    for when, buy_fee, sell_fee in (
        (MON.replace(hour=10), 0.06, 0.01), (MON.replace(hour=2), 0.02, 0.0), (SAT.replace(hour=10), 0.03, 0.02),
    ):
        assert buy_price(0.10, config, when) == pytest.approx((0.10 + buy_fee + 0.01) * 1.21)
        assert sell_price(0.10, config, when) == pytest.approx((0.10 - sell_fee - 0.005) * 0.95)
    assert config.buy.weekends is True


def test_legacy_bands_equal_on_all_days_need_no_weekend_schedule():
    bands = [{"start": "07:00", "buy_fee": 0.06, "sell_fee": 0.0}, {"start": "23:00", "buy_fee": 0.02, "sell_fee": 0.0}]
    buy, _ = legacy_price_config({**_LEGACY, CONF_TARIFF_WEEKDAY_BANDS: bands, CONF_TARIFF_WEEKEND_BANDS: bands})
    assert (buy["tariffs"], buy["grid_fees"], buy["weekends"]) == (2, [0.06, 0.02], False)
    assert buy["schedules"] == {"all_workday": [{"start": "07:00", "tariff": 1}, {"start": "23:00", "tariff": 2}]}


def test_legacy_with_more_than_four_fees_maps_the_rest_to_the_nearest():
    bands = [
        {"start": f"{hour:02d}:00", "buy_fee": fee, "sell_fee": 0.0}
        for hour, fee in ((0, 0.01), (4, 0.02), (8, 0.03), (12, 0.04), (16, 0.041))
    ]
    buy, _ = legacy_price_config({**_LEGACY, CONF_TARIFF_WEEKDAY_BANDS: bands, CONF_TARIFF_WEEKEND_BANDS: bands})
    assert buy["grid_fees"] == [0.01, 0.02, 0.03, 0.04]
    assert buy["schedules"]["all_workday"][-1] == {"start": "16:00", "tariff": 4}


def test_legacy_sell_modes_keep_their_prices():
    fixed = tariff_from_config({**_LEGACY, CONF_TARIFF_SELL_MODE: "fixed", CONF_TARIFF_FIXED_SELL_PRICE: 0.08})
    assert sell_price(0.99, fixed, MON) == pytest.approx(0.08)
    feed = tariff_from_config({**_LEGACY, CONF_TARIFF_SELL_MODE: "feed"})
    assert sell_price(0.99, feed, MON, export=0.07) == pytest.approx(0.07)
    schedule = tariff_from_config({
        **_LEGACY,
        CONF_TARIFF_SELL_MODE: "schedule",
        CONF_TARIFF_FIXED_SELL_PRICE: 0.01,
        CONF_TARIFF_WEEKDAY_BANDS: [
            {"start": "00:00", "buy_fee": 0.03, "sell_fee": 0.05},
            {"start": "08:00", "buy_fee": 0.03, "sell_fee": 0.30},
        ],
    })
    # A band's sell fee was the absolute export price; days without bands got the fixed price.
    assert sell_price(0.99, schedule, MON.replace(hour=12)) == pytest.approx(0.30)
    assert sell_price(0.99, schedule, MON.replace(hour=3)) == pytest.approx(0.05)
    assert sell_price(0.99, schedule, SAT.replace(hour=12)) == pytest.approx(0.01)


def test_legacy_tou_source_becomes_fixed_prices_with_tariffs():
    config = tariff_from_config({
        **_LEGACY,
        CONF_PRICE_SOURCE: "tou",
        CONF_PRICE_TOU_BANDS: [{"start": "00:00", "price": 0.10}, {"start": "08:00", "price": 0.40}],
    })
    assert not needs_market_price(config)
    # The TOU price used to stand in for spot in both formulas.
    for when, tou in ((MON.replace(hour=3), 0.10), (SAT.replace(hour=9), 0.40)):
        assert buy_price(0.0, config, when) == pytest.approx((tou + 0.03 + 0.01) * 1.21)
        assert sell_price(0.0, config, when) == pytest.approx((tou - 0.02 - 0.005) * 0.95)


def test_an_empty_entry_converts_to_the_defaults():
    buy, sell = legacy_price_config({})
    assert formula_from_config(buy) == PriceFormula()
    assert formula_from_config(sell) == PriceFormula()


def test_stored_formulas_win_over_legacy_keys():
    stored = {"energy": "fixed", "fixed_price": 0.2}
    buy, sell = price_config({**_LEGACY, CONF_PRICE_BUY: stored})
    assert buy == stored
    assert sell["markup"] == 0.005  # the sell side is still converted from the legacy keys
