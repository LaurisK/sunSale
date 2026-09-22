"""Behaviour of the online shape engine and its backfill.

The engine is a running average with corrections, so the tests pin the two
things that make it trustworthy: that a bucket's first day *is* its average,
and that everything after that moves toward the truth rather than away from it.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.sun_sale.contract.models import (
    OnlineShapeState,
    PriceCurveHistory,
    WeatherDaily,
)
from custom_components.sun_sale.inbound import price_backfill
from custom_components.sun_sale.orchestration import store_codecs
from custom_components.sun_sale.pipeline import online_shape

_MONDAY = date(2026, 6, 1)
_SATURDAY = date(2026, 6, 6)
_SUNDAY = date(2026, 6, 7)


def _curve(level: float = 0.10, peak: float = 0.05) -> list[float]:
    """Return a 24-hour curve with an evening peak."""
    return [level + (peak if 18 <= hour <= 20 else 0.0) for hour in range(24)]


def _inputs(day: date, wind: float = 0.0, temperature: float = 10.0) -> online_shape.DayInputs:
    """Return day inputs with a flat solar profile and no holidays."""
    return online_shape.DayInputs(
        day=day,
        bucket=online_shape.bucket_of(day),
        wind_index=wind,
        solar_profile=tuple(0.0 for _ in range(24)),
        temperature_c=temperature,
    )


def test_untrained_state_predicts_nothing():
    """A forecast is withheld rather than guessed before the first settled day."""
    assert online_shape.predict_curve(online_shape.initial_state(), _inputs(_MONDAY)) is None


def test_first_day_of_a_bucket_becomes_its_average():
    """The cold start is the working principle: day one is the average day."""
    curve = _curve()
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), curve)
    predicted = online_shape.predict_curve(state, _inputs(_MONDAY + timedelta(days=1)))
    assert predicted == pytest.approx(curve)


def test_level_moves_toward_a_repriced_market():
    """A step change in the level is tracked, and not in one jump."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve(0.10))
    before = state.levels[0]
    for offset in range(1, 6):
        state = online_shape.absorb(
            state, _inputs(_MONDAY + timedelta(days=offset)), _curve(0.20),
        )
    assert before < state.levels[0] < 0.20


def test_weekends_keep_their_own_average():
    """A Saturday must not drag the workday level with it."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve(0.20))
    workday_level = state.levels[0]
    state = online_shape.absorb(state, _inputs(_SATURDAY), _curve(0.05))
    state = online_shape.absorb(state, _inputs(_SUNDAY), _curve(0.06))
    assert state.levels[0] == pytest.approx(workday_level)
    # A bucket's level is its day's *mean*, which the evening peak lifts.
    assert state.levels[1] == pytest.approx(sum(_curve(0.05)) / 24)
    assert state.levels[2] == pytest.approx(sum(_curve(0.06)) / 24)
    assert state.bucket_days == (1, 1, 1)


def test_an_unseen_bucket_borrows_the_workday_average():
    """A fresh install's first Saturday is answered, not skipped."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve(0.20))
    predicted = online_shape.predict_curve(state, _inputs(_SATURDAY))
    assert predicted == pytest.approx(_curve(0.20))


def test_an_effector_learns_the_direction_of_its_error():
    """Wind that coincides with cheap days pushes later windy days down."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve(0.20))
    windy = _inputs(_MONDAY + timedelta(days=1), wind=1.0)
    for offset in range(1, 8):
        state = online_shape.absorb(
            state, _inputs(_MONDAY + timedelta(days=offset), wind=1.0), _curve(0.05),
        )
    calm = online_shape.predict_curve(state, _inputs(_MONDAY + timedelta(days=9)))
    gusty = online_shape.predict_curve(state, windy)
    assert sum(gusty) < sum(calm)


def test_weights_cannot_run_away():
    """One pathological day cannot throw the state out of shape."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve(0.10))
    for offset in range(1, 40):
        state = online_shape.absorb(
            state, _inputs(_MONDAY + timedelta(days=offset), wind=1.0),
            [-5.0] * 24,
        )
    flat = [abs(v) for effector in state.weights for knot in effector for v in knot]
    assert max(flat) <= online_shape.WEIGHT_CLIP_EUR_KWH + 1e-9


def test_state_shape_is_checked_before_it_is_trusted():
    """A state written against a different effector table is refused."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve())
    assert online_shape.state_matches(state)
    assert not online_shape.state_matches(OnlineShapeState())
    assert not online_shape.state_matches(
        OnlineShapeState(
            levels=state.levels, silhouettes=state.silhouettes,
            bucket_days=state.bucket_days, weights=state.weights[:-1],
            days_seen=state.days_seen,
        )
    )


def test_state_survives_the_store_round_trip():
    """What is persisted is what comes back."""
    state = online_shape.absorb(online_shape.initial_state(), _inputs(_MONDAY), _curve())
    history = PriceCurveHistory(records=(), vintages=(), shape_state=state)
    restored = store_codecs._deserialize_price_curve_history(
        store_codecs._serialize_price_curve_history(history)
    )
    assert restored.shape_state is not None
    assert restored.shape_state.levels == pytest.approx(state.levels)
    assert restored.shape_state.days_seen == state.days_seen


class _Slot:
    """Minimal price slot for the hourly-curve extraction."""

    def __init__(self, start: datetime, spot: float) -> None:
        """Store the slot's start and price."""
        self.start = start
        self.spot_eur_kwh = spot


def test_quarter_hours_average_into_an_hourly_curve():
    """The market's 15-minute unit collapses to the hourly definition."""
    slots = [
        _Slot(datetime(2026, 6, 1, hour, minute, tzinfo=UTC), 0.10 + hour / 100)
        for hour in range(24) for minute in (0, 15, 30, 45)
    ]
    curve = online_shape.curve_from_slots(slots, _MONDAY, UTC)
    assert curve is not None
    assert curve[5] == pytest.approx(0.15)


def test_a_day_with_a_missing_hour_has_no_curve():
    """A hole is reported rather than interpolated."""
    slots = [
        _Slot(datetime(2026, 6, 1, hour, tzinfo=UTC), 0.10)
        for hour in range(24) if hour != 7
    ]
    assert online_shape.curve_from_slots(slots, _MONDAY, UTC) is None


def test_solar_profile_follows_the_sun_and_the_cloud():
    """Night is zero, midday is the peak, and overcast scales the whole day."""
    clear = online_shape.solar_profile(_MONDAY, 54.9, 23.9, UTC, 0.0)
    cloudy = online_shape.solar_profile(_MONDAY, 54.9, 23.9, UTC, 100.0)
    assert clear[0] == 0.0
    assert max(clear) == pytest.approx(max(clear[8:16]))
    assert 0.0 < sum(cloudy) < sum(clear)


@pytest.mark.parametrize(("kmh", "expected"), [
    (None, 0.0), (0.0, 0.0), (7.2, 0.0), (50.0, 1.0), (100.0, 0.0),
])
def test_wind_index_saturates_and_cuts_out(kmh, expected):
    """Below cut-in and above cut-out a turbine makes nothing."""
    assert online_shape.wind_power_index(kmh) == pytest.approx(expected)


def test_wind_index_rises_between_cut_in_and_rated():
    """Inside the working range more wind is more power."""
    assert (online_shape.wind_power_index(20.0)
            < online_shape.wind_power_index(30.0)
            < online_shape.wind_power_index(40.0))


def test_backfill_maps_only_unambiguous_markets():
    """A country with several bidding zones is left to cold-start."""
    assert price_backfill.zone_for("LT") == ("elering", "lt")
    assert price_backfill.zone_for("PL") == ("energy_charts", "PL")
    assert price_backfill.zone_for("SE") is None      # four zones
    assert price_backfill.zone_for(None) is None


def test_backfill_builds_records_and_a_trained_state():
    """Fetched days become a history the engine has already learned from."""
    curves = {
        _MONDAY + timedelta(days=offset): _curve(0.10 + offset / 100)
        for offset in range(40)
    }
    history = price_backfill.build_history(
        curves, {}, UTC, 54.9, 23.9, negative_threshold_eur_kwh=0.02,
    )
    assert len(history.records) == 40
    assert all(record.curve is not None for record in history.records)
    assert history.shape_state is not None
    assert history.shape_state.days_seen == 40
    assert online_shape.predict_curve(history.shape_state, _inputs(_MONDAY)) is not None


def test_the_forecast_uses_the_engine_once_it_has_history():
    """A trained state supplies every modelled day, with consistent bands."""
    from custom_components.sun_sale.contract.models import (
        PRICE_STAT_SOURCE_MODEL,
        WeatherForecastData,
    )
    from custom_components.sun_sale.pipeline import price_forecast as pf
    from tests.test_price_forecast import _generation, _price_series

    curves = {
        _MONDAY + timedelta(days=offset): _curve(0.10 + offset / 200)
        for offset in range(60)
    }
    history = price_backfill.build_history(
        curves, {}, UTC, 54.9, 23.9, negative_threshold_eur_kwh=0.02,
    )
    now = datetime(2026, 8, 1, 9, tzinfo=UTC)
    forecast = pf.compute_price_forecast(
        price_series=_price_series(),
        history=history,
        weather=WeatherForecastData(days=()),
        generation=_generation(),
        negative_threshold_eur_kwh=0.02,
        now=now,
        local_tz=UTC,
        shape_state=history.shape_state,
        latitude=54.9,
        longitude=23.9,
    )
    modelled = [d for d in forecast.days if d.source == PRICE_STAT_SOURCE_MODEL]
    assert modelled, "the engine should supply the days the auction does not"
    assert forecast.shape_days == 60
    for day in modelled:
        assert day.trough_4h_eur_kwh <= day.peak_4h_eur_kwh
        assert day.trough_1h_eur_kwh <= day.trough_4h_eur_kwh
        assert day.peak_1h_eur_kwh >= day.peak_4h_eur_kwh


def test_a_flat_forecast_still_appears_without_weather():
    """No weather entity is not a reason to publish nothing."""
    from custom_components.sun_sale.pipeline import price_forecast as pf
    from tests.test_price_forecast import _generation, _price_series

    curves = {_MONDAY + timedelta(days=o): _curve(0.12) for o in range(20)}
    history = price_backfill.build_history(
        curves, {}, UTC, 54.9, 23.9, negative_threshold_eur_kwh=0.02,
    )
    forecast = pf.compute_price_forecast(
        price_series=_price_series(),
        history=history,
        weather=None,
        generation=_generation(),
        negative_threshold_eur_kwh=0.02,
        now=datetime(2026, 8, 1, 9, tzinfo=UTC),
        local_tz=UTC,
        shape_state=history.shape_state,
        latitude=54.9,
        longitude=23.9,
    )
    assert len(forecast.days) == 7
