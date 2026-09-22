"""Week-ahead price forecasting that learns on the clock, one day at a time.

The shipped statistics model fits a ridge on daily aggregates and predicts four
*rank* statistics — the dearest hour, the cheapest four hours — each on its own.
This module predicts the **shape of the day in clock order** and reads those
statistics off the finished curve, so they cannot contradict each other and the
timing information rank statistics throw away is kept.

A day is assembled as::

    price(h) = level[bucket] + silhouette[bucket][h] + Σ effectors w[e][k][h]·φ[e][k](x)

* ``bucket`` is the day type. Workdays, Saturdays and Sundays carry **separate**
  running levels and silhouettes, because a weekend is not a workday with a
  discount: measured on Lithuanian history a weekend day sits at about half the
  workday level, Saturday's shape is the flattest of the three, and Sunday's
  evening peak is nearly a workday's while Saturday's is far below it.
* ``silhouette`` is each hour's running distance from its own day's mean — the
  duck curve, in clock order.
* every **effector** carries its own 24-hour weight profile, so wind may push
  the evening down and leave the night alone. That profile is what lets daily
  weather do hourly work, which is why this engine needs no hourly forecast.
* each effector's value passes through a piecewise-linear response built from
  tent basis functions, every knot of which owns a 24-hour profile. The bend is
  learned, not assumed.

**Learning is the same loop as running.** When a day settles, the engine
compares what it predicted for it a day ahead with what happened, and nudges
every weight in proportion to its contribution. There is no batch fit, no
training window and no solver: one settled day costs a few hundred multiply-adds
and the whole state is under 400 floats. A backfilled install and an install
that has been running for two years converge to the same state, because the
backfill is the same loop replayed over fetched history.

Only the **next-day** prediction trains the weights — the day-ahead auction is
the only ground truth that arrives at a known lead — and days further out reuse
those weights with their own forecast.

Measured on 910 days of Lithuanian day-ahead history against a trailing
day-class climatology, with the daily-resolution weather this integration can
actually read: **+26.5 % at D+1, +20.5 % at D+3, +13.4 % at D+7**. Supplying
hourly weather instead is worth 0.06 c/kWh, which is why none is asked for. See
``docs/price_forecast_online_shape.md``.

Pure Python — no Home Assistant imports, no third-party deps.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

from ..contract.models import OnlineShapeState, WeatherDaily

HOURS = 24

# --- Effector table ---------------------------------------------------------
# Knot counts come from a sweep over the same history: a straight-line response
# (2 knots) costs 0.18 c/kWh and a fourth knot buys nothing, because the
# relationships bend exactly once — more wind is cheaper, with diminishing
# returns. Bunching the knots toward the extreme of a range ("steeper") was
# measured worse in every configuration, so the knots are evenly spaced: the
# resolution is needed where the ordinary days are.


@dataclass(frozen=True)
class Effector:
    """One driver of the price shape.

    Attributes:
        name: Identifier, also the key used in diagnostics.
        knots: Knots in the learned response curve; 1 is a plain on/off flag.
        low: Value mapped to the bottom of the response.
        high: Value mapped to the top.
    """
    name: str
    knots: int
    low: float = 0.0
    high: float = 1.0

    def activations(self, value: float) -> list[float]:
        """Return the tent-basis activations of one value.

        Args:
            value: Raw effector value.

        Returns:
            One weight per knot, summing to 1 inside the range. With a single
            knot the (clamped) value passes straight through, which is what a
            binary flag wants.
        """
        span = self.high - self.low
        scaled = 0.0 if span <= 0 else (value - self.low) / span
        scaled = min(1.0, max(0.0, scaled))
        if self.knots == 1:
            return [scaled]
        step = 1.0 / (self.knots - 1)
        out = []
        for index in range(self.knots):
            distance = abs(scaled - index * step) / step
            out.append(max(0.0, 1.0 - distance))
        return out


EFFECTORS: tuple[Effector, ...] = (
    Effector("wind", knots=3),
    Effector("solar", knots=3),
    Effector("temperature", knots=4, low=-15.0, high=30.0),
    Effector("holiday", knots=1),
    Effector("neighbour_holiday", knots=2),
)

#: Day-type buckets, each with its own running level and silhouette.
BUCKETS = ("workday", "saturday", "sunday")

#: Step sizes. The weights move fastest because they have the least to carry;
#: the level moves fast enough to track a market repricing within a fortnight.
LR_WEIGHT = 0.05
LR_SILHOUETTE = 0.05
LR_LEVEL = 0.10

#: No single weight may move a price by more than this, which keeps one
#: pathological day (a negative-price hour against a high level) from throwing
#: the state out of shape.
WEIGHT_CLIP_EUR_KWH = 0.25

#: Days a bucket needs before its predictions are published rather than its
#: neighbour's. One day is enough to place the bucket; the effectors then earn
#: their weights from there.
MIN_BUCKET_DAYS = 1

#: Turbine cut-in, rated and cut-out speeds in m/s, for the power index that
#: replaces raw wind speed. The cube-then-saturate transform is what makes wind
#: comparable across sites and seasons.
_WIND_CUT_IN = 3.0
_WIND_RATED = 12.0
_WIND_CUT_OUT = 25.0

#: Irradiance that maps to the top of the solar effector's range, W/m². The
#: profile is scaled against this fixed reference rather than against the day's
#: own peak: normalising per day would make an overcast December noon and a
#: clear June noon identical, which is exactly the signal being modelled.
_SOLAR_REFERENCE_WM2 = 900.0

#: Neighbouring bidding zones whose public holidays move the local price,
#: keyed by the install's own holiday country. A country that is not listed
#: simply never sees the effector fire.
NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "LT": ("LV", "EE", "PL", "SE", "FI"),
    "LV": ("LT", "EE", "SE", "FI"),
    "EE": ("LV", "FI", "SE"),
    "FI": ("SE", "NO", "EE"),
    "SE": ("NO", "FI", "DK"),
    "NO": ("SE", "DK", "FI"),
    "DK": ("SE", "NO", "DE"),
    "PL": ("DE", "SE", "LT"),
    "DE": ("PL", "DK", "NL", "FR"),
    "NL": ("DE", "BE", "DK"),
    "BE": ("NL", "FR", "DE"),
    "FR": ("DE", "BE", "ES"),
    "ES": ("FR", "PT"),
    "PT": ("ES",),
}


def wind_power_index(speed_kmh: float | None) -> float:
    """Convert a wind speed into a 0-1 turbine power index.

    Raw speed is the wrong scale for price: output rises with the cube of speed
    up to rated, then flattens, then stops. The earlier multi-region study
    measured this transform sharpening every within-region correlation.

    Args:
        speed_kmh: Wind speed in km/h, or None.

    Returns:
        Index in 0-1; 0 when the speed is unknown, below cut-in or above
        cut-out.
    """
    if speed_kmh is None:
        return 0.0
    speed = speed_kmh / 3.6
    if speed <= _WIND_CUT_IN or speed >= _WIND_CUT_OUT:
        return 0.0
    if speed >= _WIND_RATED:
        return 1.0
    ratio = (speed - _WIND_CUT_IN) / (_WIND_RATED - _WIND_CUT_IN)
    return min(1.0, max(0.0, ratio ** 3))


@dataclass(frozen=True)
class DayInputs:
    """Everything the engine needs to place one local day on the clock.

    Attributes:
        day: Local date.
        bucket: Index into :data:`BUCKETS`.
        wind_index: Turbine power index for the day, 0-1.
        solar_profile: 24 relative irradiance values, 0-1, in local clock
            order. Modelled clear-sky shape scaled by forecast cloud cover, so
            no irradiance feed is needed.
        temperature_c: Daily mean temperature, or None when unknown.
        is_holiday: Whether the local calendar makes this a public holiday.
        neighbour_share: Fraction of neighbouring zones on holiday, 0-1.
    """
    day: date
    bucket: int
    wind_index: float = 0.0
    solar_profile: tuple[float, ...] = ()
    temperature_c: float | None = None
    is_holiday: bool = False
    neighbour_share: float = 0.0

    def values(self) -> list[list[float]]:
        """Return each effector's 24 hourly values, in ``EFFECTORS`` order."""
        solar = list(self.solar_profile) if len(self.solar_profile) == HOURS else [0.0] * HOURS
        # A missing temperature must not read as "freezing": the neutral value
        # is the middle of the response, where the curve contributes least.
        temperature = self.temperature_c if self.temperature_c is not None else 7.5
        return [
            [self.wind_index] * HOURS,
            solar,
            [temperature] * HOURS,
            [1.0 if self.is_holiday else 0.0] * HOURS,
            [self.neighbour_share] * HOURS,
        ]


def bucket_of(day: date, is_holiday: bool = False) -> int:
    """Return which running average a date belongs to.

    Holidays stay in the workday bucket and are corrected by their own
    effector: there are too few of them a year to support a fourth running
    average, and what they mostly do is look like a weak weekend.

    Args:
        day: Local date.
        is_holiday: Whether the day is a public holiday.

    Returns:
        Index into :data:`BUCKETS`.
    """
    weekday = day.weekday()
    if weekday == 5:
        return 1
    if weekday == 6:
        return 2
    return 0


def initial_state() -> OnlineShapeState:
    """Return the untrained state: no level, flat silhouette, neutral weights."""
    return OnlineShapeState(
        levels=(0.0,) * len(BUCKETS),
        silhouettes=tuple((0.0,) * HOURS for _ in BUCKETS),
        bucket_days=(0,) * len(BUCKETS),
        weights=tuple(
            tuple((0.0,) * HOURS for _ in range(effector.knots))
            for effector in EFFECTORS
        ),
        days_seen=0,
    )


def _activations(inputs: DayInputs) -> list[list[list[float]]]:
    """Return per-effector, per-knot, per-hour basis activations."""
    out: list[list[list[float]]] = []
    for effector, hourly in zip(EFFECTORS, inputs.values()):
        per_knot: list[list[float]] = [[0.0] * HOURS for _ in range(effector.knots)]
        for hour in range(HOURS):
            for knot, weight in enumerate(effector.activations(hourly[hour])):
                per_knot[knot][hour] = weight
        out.append(per_knot)
    return out


def is_ready(state: OnlineShapeState) -> bool:
    """Return whether the state can predict at all."""
    return state.days_seen > 0 and any(n >= MIN_BUCKET_DAYS for n in state.bucket_days)


def predict_curve(state: OnlineShapeState, inputs: DayInputs) -> list[float] | None:
    """Return the predicted 24-hour price curve for one local day.

    Args:
        state: Learned state.
        inputs: The day's calendar and weather.

    Returns:
        Twenty-four spot prices in EUR/kWh in local clock order, or None when
        the state has never seen a settled day.
    """
    if not is_ready(state):
        return None
    # A bucket with no day of its own — a fresh install's first Saturday —
    # borrows the workday average rather than predicting nothing.
    bucket = inputs.bucket if state.bucket_days[inputs.bucket] >= MIN_BUCKET_DAYS else 0
    level = state.levels[bucket]
    silhouette = state.silhouettes[bucket]
    curve = [level + silhouette[hour] for hour in range(HOURS)]
    for weights, per_knot in zip(state.weights, _activations(inputs)):
        for knot_weights, knot_activation in zip(weights, per_knot):
            for hour in range(HOURS):
                activation = knot_activation[hour]
                if activation:
                    curve[hour] += knot_weights[hour] * activation
    return curve


def absorb(
    state: OnlineShapeState,
    inputs: DayInputs,
    curve: list[float],
) -> OnlineShapeState:
    """Learn from one settled day and return the updated state.

    The order matters: the effector weights are corrected against the error the
    engine actually made, and only then do the silhouette and the level drift.
    That way the weights learn what the running averages could not.

    Args:
        state: Learned state.
        inputs: That day's calendar and weather, as they were known a day ahead.
        curve: The day's settled hourly spot prices, EUR/kWh, clock order.

    Returns:
        The updated state. Unchanged when the curve is not 24 hours long.
    """
    if len(curve) != HOURS:
        return state
    bucket = inputs.bucket
    mean = sum(curve) / HOURS
    levels = list(state.levels)
    silhouettes = [list(row) for row in state.silhouettes]
    bucket_days = list(state.bucket_days)
    weights = [[list(knot) for knot in effector] for effector in state.weights]

    if bucket_days[bucket] < MIN_BUCKET_DAYS:
        # Cold start from the working principle itself: the first day of a
        # bucket *is* that bucket's average day.
        levels[bucket] = mean
        silhouettes[bucket] = [value - mean for value in curve]
    else:
        predicted = predict_curve(state, inputs) or [mean] * HOURS
        residual = [curve[hour] - predicted[hour] for hour in range(HOURS)]
        for effector_index, per_knot in enumerate(_activations(inputs)):
            for knot, knot_activation in enumerate(per_knot):
                row = weights[effector_index][knot]
                for hour in range(HOURS):
                    activation = knot_activation[hour]
                    if not activation:
                        continue
                    moved = row[hour] + LR_WEIGHT * residual[hour] * activation
                    row[hour] = min(WEIGHT_CLIP_EUR_KWH,
                                    max(-WEIGHT_CLIP_EUR_KWH, moved))
        levels[bucket] += LR_LEVEL * (mean - levels[bucket])
        for hour in range(HOURS):
            target = curve[hour] - mean
            silhouettes[bucket][hour] += LR_SILHOUETTE * (target - silhouettes[bucket][hour])

    bucket_days[bucket] += 1
    return OnlineShapeState(
        levels=tuple(levels),
        silhouettes=tuple(tuple(row) for row in silhouettes),
        bucket_days=tuple(bucket_days),
        weights=tuple(tuple(tuple(knot) for knot in effector) for effector in weights),
        days_seen=state.days_seen + 1,
    )


# ---------------------------------------------------------------------------
# Turning what the integration can read into what the engine needs
# ---------------------------------------------------------------------------


def solar_profile(
    day: date,
    latitude: float,
    longitude: float,
    local_tz: tzinfo,
    cloud_coverage_pct: float | None,
    position: Callable[[datetime, float, float], object] | None = None,
    irradiance: Callable[[float], float] | None = None,
) -> tuple[float, ...]:
    """Return a 24-hour relative irradiance profile for one local day.

    No weather integration publishes irradiance, but the clear-sky geometry is
    computable from the site's coordinates alone, and cloud cover — which they
    do publish — scales it. That is enough for an effector whose *timing* comes
    from its own learned 24-hour weight profile.

    Args:
        day: Local date.
        latitude: Site latitude.
        longitude: Site longitude.
        local_tz: Local timezone.
        cloud_coverage_pct: Forecast cloud cover, 0-100, or None for clear.
        position: Solar-position function; defaults to the pipeline's.
        irradiance: Clear-sky beam function; defaults to the pipeline's.

    Returns:
        Twenty-four values in 0-1, scaled against a fixed clear-sky reference
        so that both the season and the cloud cover move the level.
    """
    if position is None or irradiance is None:
        from .clear_sky import clear_sky_dni_wm2  # noqa: PLC0415 — avoids a cycle
        from .solar_geometry import solar_position  # noqa: PLC0415
        position = position or solar_position
        irradiance = irradiance or clear_sky_dni_wm2

    cloud = 0.0 if cloud_coverage_pct is None else min(100.0, max(0.0, cloud_coverage_pct))
    # A fully overcast sky still passes roughly a quarter of clear-sky global.
    transmission = 1.0 - 0.75 * (cloud / 100.0)
    values: list[float] = []
    for hour in range(HOURS):
        when = datetime.combine(day, time(hour=hour, minute=30), tzinfo=local_tz)
        sun = position(when.astimezone(None) if when.tzinfo is None else when,
                       latitude, longitude)
        elevation = getattr(sun, "elevation_deg", 0.0)
        if elevation <= 0.0:
            values.append(0.0)
            continue
        ghi = irradiance(elevation) * math.sin(math.radians(elevation))
        values.append(max(0.0, ghi) * transmission)
    return tuple(min(1.0, value / _SOLAR_REFERENCE_WM2) for value in values)


def build_inputs(
    day: date,
    weather: WeatherDaily | None,
    latitude: float,
    longitude: float,
    local_tz: tzinfo,
    is_holiday: Callable[[date], bool] | None = None,
    neighbour_holiday: Callable[[date], float] | None = None,
) -> DayInputs:
    """Assemble one day's effector inputs from what the integration can read.

    Args:
        day: Local date.
        weather: That day's daily forecast row, or None when uncovered.
        latitude: Site latitude.
        longitude: Site longitude.
        local_tz: Local timezone.
        is_holiday: Local public-holiday predicate.
        neighbour_holiday: Returns the fraction of neighbouring zones on
            holiday that day.

    Returns:
        The day's inputs. A day with no weather still carries its calendar,
        and the weather effectors simply sit at their neutral values.
    """
    holiday = bool(is_holiday(day)) if is_holiday is not None else False
    return DayInputs(
        day=day,
        bucket=bucket_of(day, holiday),
        wind_index=wind_power_index(weather.wind_speed_kmh if weather else None),
        solar_profile=solar_profile(
            day, latitude, longitude, local_tz,
            weather.cloud_coverage_pct if weather else None,
        ),
        temperature_c=weather.temperature_c if weather else None,
        is_holiday=holiday and day.weekday() < 5,
        neighbour_share=(neighbour_holiday(day) if neighbour_holiday is not None else 0.0),
    )


def neighbour_share_fn(
    country: str | None,
    predicate_for: Callable[[str], Callable[[date], bool] | None],
) -> Callable[[date], float]:
    """Return a function giving the share of neighbouring zones on holiday.

    Args:
        country: The install's own holiday country.
        predicate_for: Builds a holiday predicate for a country code.

    Returns:
        A function of local date returning 0-1; always 0 when the country has
        no declared neighbours or the calendars are unavailable.
    """
    codes = NEIGHBOURS.get((country or "").upper(), ())
    predicates = [p for p in (predicate_for(code) for code in codes) if p is not None]
    if not predicates:
        return lambda _day: 0.0

    def share(day: date) -> float:
        """Return the fraction of neighbouring zones on holiday."""
        return sum(1 for predicate in predicates if predicate(day)) / len(predicates)

    return share


def curve_from_slots(
    slots: list,
    day: date,
    local_tz: tzinfo,
) -> list[float] | None:
    """Average a day's price slots into 24 hourly values in clock order.

    Args:
        slots: PriceSlots covering the day, any resolution.
        day: Local date to extract.
        local_tz: Local timezone.

    Returns:
        Twenty-four spot prices, or None when an hour has no slot at all. The
        market ran hourly before October 2025 and quarter-hourly after, and an
        hourly curve is the one definition continuous across that break.
    """
    buckets: list[list[float]] = [[] for _ in range(HOURS)]
    for slot in slots:
        local = slot.start.astimezone(local_tz)
        if local.date() != day:
            continue
        buckets[local.hour].append(slot.spot_eur_kwh)
    if any(not bucket for bucket in buckets):
        return None
    return [sum(bucket) / len(bucket) for bucket in buckets]


def state_matches(state: OnlineShapeState) -> bool:
    """Return whether a stored state still fits this module's effector table.

    The weights are positional, so a state written before an effector was added
    or a knot count changed cannot be read back. Detecting that is cheap and the
    remedy is to start again: the engine is back to useful accuracy within a few
    weeks, and every settled day it needs is already in the history.

    Args:
        state: State restored from the store.

    Returns:
        True when the shape is compatible with :data:`EFFECTORS`.
    """
    if len(state.levels) != len(BUCKETS) or len(state.bucket_days) != len(BUCKETS):
        return False
    if len(state.silhouettes) != len(BUCKETS):
        return False
    if any(len(row) != HOURS for row in state.silhouettes):
        return False
    if len(state.weights) != len(EFFECTORS):
        return False
    for effector, weights in zip(EFFECTORS, state.weights):
        if len(weights) != effector.knots:
            return False
        if any(len(knot) != HOURS for knot in weights):
            return False
    return True
