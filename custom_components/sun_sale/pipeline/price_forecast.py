"""Week-ahead price forecast: daily peak / trough / negative-price statistics.

The day-ahead auction covers at most ~48 h, shrinking to ~24 h just before
publication, so every decision about holding energy past that edge is made
blind. This module extends the view to a full week on the same axis as the
week-ahead *generation* forecast, publishing four statistics per local day:
the mean of the highest and lowest 1 h and 4 h of the day, how many hours sit
at or below the export break-even, and how much PV is expected to land in
them.

Days the auction has already settled are reported verbatim. The rest come from
:mod:`pipeline.online_shape`, which predicts the day **in clock order** and
learns online from each day as it settles; this module reads the four published
statistics off that predicted curve, so they are always mutually consistent and
a day is either forecast or withheld — never averaged into a shape nobody
expects. See ``docs/price_forecast_online_shape.md``.

This module also owns the settled-day history the engine trains on
(``settle_days``, ``build_day_record``) and the PV-in-loss-making-hours figure
that rides alongside the price statistics.

Pure Python — no Home Assistant imports, no third-party deps.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from ..contract.const import (
    ENERGY_DYNAMIC,
    PRICE_FORECAST_CONFIDENCE_DECAY,
    PRICE_FORECAST_HORIZON_DAYS,
    PRICE_FORECAST_VINTAGE_LEAD_DAYS,
)
from ..contract.models import (
    PredictedDay,
    PriceCurvePoint,
    PriceErrorPoint,
    PRICE_STAT_SOURCE_ACTUAL,
    PRICE_STAT_SOURCE_MODEL,
    BaseLoadProfile,
    DayFeatureVintage,
    GenerationSeries,
    OnlineShapeState,
    PriceCurveHistory,
    PriceDayRecord,
    PriceDayStats,
    PriceForecast,
    PriceSeries,
    TariffConfig,
    WeatherForecastData,
)
from . import online_shape
from . import tariff as tariff_module
from .profitability import classify_day

# Band widths, in hours, behind the 1 h / 4 h statistics. Each band is the
# cheapest / dearest slots anywhere in the day, not a contiguous window: a
# battery can charge and discharge in scattered slots.
_BAND_1H = 1.0
_BAND_4H = 4.0

# Fraction of a day's slots that must be present before its statistics are
# trusted. Guards the local-midnight boundary and partial auction publication,
# where a handful of slots would otherwise define a "daily" peak.
_MIN_DAY_COVERAGE = 0.9

# Neutral fallback for the share of a day's PV that lands in negative-price
# hours: the hours spread evenly across a nominal 12 h daylight window. Used
# only until the install has enough settled days to fit its own ratio, and
# deliberately conservative — real negative hours cluster around solar noon,
# so this understates rather than overstates the exposure.
_NEUTRAL_DAYLIGHT_HOURS = 12.0


# ---------------------------------------------------------------------------
# Day statistics from settled prices
# ---------------------------------------------------------------------------


def _band_mean(values: Sequence[float], hours: float, slot_hours: float, top: bool) -> float:
    """Return the mean of the highest or lowest ``hours`` worth of a day.

    Args:
        values: The day's prices, ascending.
        hours: Width of the band in hours.
        slot_hours: Duration of one slot in hours.
        top: True for the highest band, False for the lowest.

    Returns:
        Mean price across the band; the single extreme value when the day is
        shorter than the band.
    """
    if not values:
        return 0.0
    count = max(1, min(len(values), int(round(hours / slot_hours)) if slot_hours > 0 else 1))
    band = values[-count:] if top else values[:count]
    return sum(band) / len(band)


def slots_for_local_day(
    slots: Sequence,
    day: date,
    local_tz: tzinfo | None,
) -> list:
    """Return the slots whose start falls on ``day`` in local time.

    Args:
        slots: Any sequence of objects exposing a timezone-aware ``start``.
        day: Local date to select.
        local_tz: Local timezone; UTC when None.

    Returns:
        List of matching slots, in input order.
    """
    tz = local_tz or UTC
    return [s for s in slots if s.start.astimezone(tz).date() == day]


def day_price_stats(
    price_slots: Sequence,
    slot_hours: float,
    negative_threshold_eur_kwh: float,
) -> dict[str, float] | None:
    """Compute one day's price statistics from its settled slots.

    Args:
        price_slots: That day's PriceSlots.
        slot_hours: Duration of one slot in hours.
        negative_threshold_eur_kwh: Spot price at or below which exporting stops
            paying — the tariff wedge, not zero. See ``export_break_even``.

    Returns:
        Dict of the statistics, or None when the day has no slots.
    """
    if not price_slots:
        return None
    spot = sorted(s.spot_eur_kwh for s in price_slots)
    below = sum(1 for v in spot if v <= negative_threshold_eur_kwh)
    return {
        "peak_1h_eur_kwh": _band_mean(spot, _BAND_1H, slot_hours, top=True),
        "peak_4h_eur_kwh": _band_mean(spot, _BAND_4H, slot_hours, top=True),
        "trough_1h_eur_kwh": _band_mean(spot, _BAND_1H, slot_hours, top=False),
        "trough_4h_eur_kwh": _band_mean(spot, _BAND_4H, slot_hours, top=False),
        "negative_hours": below * slot_hours,
        "mean_eur_kwh": sum(spot) / len(spot),
    }


def export_break_even(tariff: TariffConfig) -> float:
    """Return the spot price below which exporting stops paying.

    A dynamic sell price is ``(spot − markup − grid_fee) × (1 − vat)``, so the
    sign flips at ``markup + grid_fee`` — not at zero. On a strict
    ``spot < 0`` test most loss-making hours would go uncounted; across the
    markets studied, hours below the wedge outnumber hours below zero by
    roughly 3:1. With several grid-fee tariffs the cheapest one is used, so a
    counted hour loses money whichever tariff is active.

    Args:
        tariff: User-configured tariff.

    Returns:
        Spot price per kWh at which the effective sell price reaches zero, or
        ``-inf`` for a fixed sell price, which no spot price makes loss-making.
    """
    sell = tariff.sell
    if sell.energy_mode != ENERGY_DYNAMIC:
        return float("-inf")
    return sell.markup + min(sell.grid_fees)


def negative_generation_for_day(
    price_slots: Sequence,
    generation: GenerationSeries | None,
    negative_threshold_eur_kwh: float,
) -> float:
    """Sum the forecast PV energy landing in that day's loss-making slots.

    Args:
        price_slots: That day's PriceSlots.
        generation: Slot-aligned generation series, or None.
        negative_threshold_eur_kwh: Export break-even spot price.

    Returns:
        Expected kWh generated while the spot price sits at or below the
        break-even; 0.0 when generation is unavailable.
    """
    if generation is None or not generation.slots or not price_slots:
        return 0.0
    by_start = {g.start: g.expected_kwh for g in generation.slots}
    return sum(
        by_start.get(s.start, 0.0)
        for s in price_slots
        if s.spot_eur_kwh <= negative_threshold_eur_kwh
    )


def _consumption_during(
    negative_slots: Sequence,
    slot_hours: float,
    base_load: BaseLoadProfile | None,
    local_tz: tzinfo | None,
) -> float:
    """Estimate household draw across a settled day's loss-making slots.

    Args:
        negative_slots: The day's slots at or below the break-even.
        slot_hours: Duration of one slot in hours.
        base_load: Hourly baseload floor profile, or None.
        local_tz: Local timezone used to bucket slots to profile hours.

    Returns:
        Expected kWh consumed during those slots; 0.0 without a profile.
    """
    if base_load is None or not negative_slots:
        return 0.0
    tz = local_tz or UTC
    return sum(base_load.at(s.start, tz) * slot_hours for s in negative_slots)


def _absorption_split(
    negative_generation_kwh: float,
    consumption_kwh: float,
    battery_capacity_kwh: float | None,
) -> tuple[float | None, float | None]:
    """Split negative-price generation into what the site can take and what it cannot.

    Headroom is deliberately the **best case**: the whole battery plus the load
    running through those hours, i.e. what could be absorbed if the battery
    were completely empty when the day starts. That makes ``surplus`` a lower
    bound on unavoidable spill — if even an empty battery cannot take it, no
    schedule can, and the only remaining lever is to have sold more earlier.

    Args:
        negative_generation_kwh: PV expected during loss-making hours.
        consumption_kwh: Household draw across the same hours.
        battery_capacity_kwh: Usable battery capacity, or None when unknown.

    Returns:
        Tuple of (absorbable, surplus), both None when capacity is unknown.
    """
    if battery_capacity_kwh is None:
        return None, None
    headroom = max(0.0, battery_capacity_kwh) + max(0.0, consumption_kwh)
    absorbable = min(negative_generation_kwh, headroom)
    return absorbable, max(0.0, negative_generation_kwh - absorbable)


def build_day_record(
    price_series: PriceSeries,
    generation: GenerationSeries | None,
    weather: WeatherForecastData | None,
    day: date,
    local_tz: tzinfo | None,
    negative_threshold_eur_kwh: float,
    is_holiday: Callable[[date], bool] | None = None,
) -> PriceDayRecord | None:
    """Build the persisted record for one settled local day.

    Both the targets and the features that explain them are captured together,
    so the model can later be refit from the store alone.

    Args:
        price_series: Series covering the day.
        generation: Slot-aligned generation series, or None.
        weather: Daily weather forecast, used for that day's features.
        day: Local date to record.
        local_tz: Local timezone.
        negative_threshold_eur_kwh: Export break-even spot price.

    Returns:
        The record, or None when the day is not covered densely enough to be
        representative.
    """
    slots = slots_for_local_day(price_series.priced_slots, day, local_tz)
    slot_hours = price_series.resolution.total_seconds() / 3600.0
    if not slots or slot_hours <= 0:
        return None
    if len(slots) * slot_hours < 24.0 * _MIN_DAY_COVERAGE:
        return None

    stats = day_price_stats(slots, slot_hours, negative_threshold_eur_kwh)
    if stats is None:
        return None

    wx = (weather.by_day().get(day) if weather is not None else None)
    gen_slots = slots_for_local_day(generation.slots, day, local_tz) if generation else []
    solar_kwh = sum(g.expected_kwh for g in gen_slots) if gen_slots else None

    return PriceDayRecord(
        day=day,
        day_class=classify_day(day, is_holiday),
        peak_1h_eur_kwh=stats["peak_1h_eur_kwh"],
        peak_4h_eur_kwh=stats["peak_4h_eur_kwh"],
        trough_1h_eur_kwh=stats["trough_1h_eur_kwh"],
        trough_4h_eur_kwh=stats["trough_4h_eur_kwh"],
        negative_hours=stats["negative_hours"],
        mean_eur_kwh=stats["mean_eur_kwh"],
        wind_speed_kmh=wx.wind_speed_kmh if wx else None,
        temperature_c=wx.temperature_c if wx else None,
        solar_kwh=solar_kwh,
        negative_generation_kwh=negative_generation_for_day(
            slots, generation, negative_threshold_eur_kwh
        ),
        cloud_coverage_pct=wx.cloud_coverage_pct if wx else None,
        # The hourly curve is what the online shape engine trains on. A day
        # whose slots do not cover every hour is stored without one rather than
        # interpolated, because a filled hole is indistinguishable from data.
        curve=tuple(online_shape.curve_from_slots(slots, day, local_tz or UTC) or ())
        or None,
    )


def _negative_generation_ratio(records: Sequence[PriceDayRecord]) -> float:
    """Fit the share of daily PV that lands in one negative-price hour.

    Regresses ``negative_generation / solar`` on ``negative_hours`` through the
    origin. A learned ratio matters because negative hours cluster around solar
    noon in PV-driven markets, so the naive "hours divided by daylight" figure
    understates exposure — often badly.

    Args:
        records: Settled history.

    Returns:
        Fraction of the day's PV per negative hour, clamped to a sane range.
    """
    num = 0.0
    den = 0.0
    for rec in records:
        if not rec.solar_kwh or rec.negative_generation_kwh is None:
            continue
        if rec.negative_hours <= 0:
            continue
        ratio = rec.negative_generation_kwh / rec.solar_kwh
        num += ratio * rec.negative_hours
        den += rec.negative_hours * rec.negative_hours
    if den <= 0:
        return 1.0 / _NEUTRAL_DAYLIGHT_HOURS
    return min(1.0, max(0.0, num / den))


# ---------------------------------------------------------------------------
# The online shape engine
# ---------------------------------------------------------------------------


def train_shape_state(
    history: PriceCurveHistory,
    latitude: float | None,
    longitude: float | None,
    local_tz: tzinfo | None,
    is_holiday: Callable[[date], bool] | None,
    neighbour_share: Callable[[date], float] | None,
) -> OnlineShapeState:
    """Replay every stored day with a curve through the online engine.

    Used when the store has records but no state — a first run after the engine
    shipped, or after an effector change invalidated the old positions. Replay
    is the same loop the engine runs live, so a replayed state and a state that
    grew day by day are the same thing.

    Args:
        history: Settled history.
        latitude: Site latitude.
        longitude: Site longitude.
        local_tz: Local timezone.
        is_holiday: Local public-holiday predicate.
        neighbour_share: Share of neighbouring zones on holiday.

    Returns:
        The trained state; untrained when no record carries a curve.
    """
    state = online_shape.initial_state()
    tz = local_tz or UTC
    for record in history.records:
        if not record.curve:
            continue
        state = online_shape.absorb(
            state,
            _record_inputs(record, latitude, longitude, tz, is_holiday, neighbour_share),
            list(record.curve),
        )
    return state


def _record_inputs(
    record: PriceDayRecord,
    latitude: float | None,
    longitude: float | None,
    local_tz: tzinfo,
    is_holiday: Callable[[date], bool] | None,
    neighbour_share: Callable[[date], float] | None,
) -> online_shape.DayInputs:
    """Rebuild one settled day's effector inputs from its stored record."""
    holiday = bool(is_holiday(record.day)) if is_holiday is not None else False
    return online_shape.DayInputs(
        day=record.day,
        bucket=online_shape.bucket_of(record.day, holiday),
        wind_index=online_shape.wind_power_index(record.wind_speed_kmh),
        solar_profile=online_shape.solar_profile(
            record.day, latitude or 0.0, longitude or 0.0, local_tz,
            record.cloud_coverage_pct,
        ),
        temperature_c=record.temperature_c,
        is_holiday=holiday and record.day.weekday() < 5,
        neighbour_share=(neighbour_share(record.day) if neighbour_share else 0.0),
    )


def _modelled_day_from_curve(
    curve: list[float],
    negative_threshold_eur_kwh: float,
) -> dict[str, float]:
    """Read the published statistics off a predicted hourly curve.

    Reading them off one curve is what makes them mutually consistent: a
    predicted trough can no longer come out above its own peak, which two
    independently-fitted band models could and did.

    Args:
        curve: Twenty-four predicted spot prices, EUR/kWh.
        negative_threshold_eur_kwh: Export break-even spot price.

    Returns:
        The same statistics ``day_price_stats`` returns for a settled day.
    """
    spot = sorted(curve)
    below = sum(1 for value in spot if value <= negative_threshold_eur_kwh)
    return {
        "peak_1h_eur_kwh": _band_mean(spot, _BAND_1H, 1.0, top=True),
        "peak_4h_eur_kwh": _band_mean(spot, _BAND_4H, 1.0, top=True),
        "trough_1h_eur_kwh": _band_mean(spot, _BAND_1H, 1.0, top=False),
        "trough_4h_eur_kwh": _band_mean(spot, _BAND_4H, 1.0, top=False),
        "negative_hours": float(below),
        "mean_eur_kwh": sum(curve) / len(curve),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_price_forecast(
    price_series: PriceSeries | None,
    history: PriceCurveHistory | None,
    weather: WeatherForecastData | None,
    generation: GenerationSeries | None,
    negative_threshold_eur_kwh: float,
    now: datetime | None = None,
    local_tz: tzinfo | None = None,
    horizon_days: int = PRICE_FORECAST_HORIZON_DAYS,
    battery_capacity_kwh: float | None = None,
    base_load: BaseLoadProfile | None = None,
    shape_state: OnlineShapeState | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    is_holiday: Callable[[date], bool] | None = None,
    neighbour_share: Callable[[date], float] | None = None,
    tariff: TariffConfig | None = None,
) -> PriceForecast:
    """Build the week-ahead price forecast.

    Days already covered by the settled auction are reported verbatim from
    ``price_series``; the rest come from the online shape engine. A day the
    engine cannot speak for — a brand-new install, before its first day
    settles — is left out of the horizon rather than filled in.

    Args:
        price_series: Settled prices covering yesterday → tomorrow, or None.
        history: Rolling settled-day history used to fit, or None.
        weather: Daily weather forecast for the horizon, or None.
        generation: Slot-aligned generation series, used for the negative-price
            generation figure and as the model's solar feature.
        negative_threshold_eur_kwh: Export break-even spot price.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: HA-configured local timezone.
        horizon_days: Number of days to publish, starting today.
        battery_capacity_kwh: Usable battery capacity, used to split each day's
            negative-price generation into absorbable and surplus. ``None``
            leaves the split unreported rather than assuming a size.
        base_load: Hourly baseload profile; the household draw through the
            loss-making hours counts as headroom alongside the battery.
        shape_state: Learned state of the online shape engine, which is what
            every modelled day comes from.
        latitude: Site latitude, for the modelled clear-sky solar profile.
        longitude: Site longitude, same.
        is_holiday: Local public-holiday predicate.
        neighbour_share: Returns the share of neighbouring bidding zones on
            holiday for a local date.
        tariff: User tariff, used only to publish the predicted buy and sell
            prices alongside the predicted spot.

    Returns:
        PriceForecast covering ``horizon_days`` local days; empty when there is
        neither settled data nor usable history.
    """
    if now is None:
        now = datetime.now(UTC)
    tz = local_tz or UTC
    today = now.astimezone(tz).date()

    records = tuple(history.records) if history is not None else ()
    # The state lives on the history, so a caller that passes one need not
    # also thread the other.
    if shape_state is None and history is not None:
        shape_state = history.shape_state
    neg_ratio = _negative_generation_ratio(records)

    slot_hours = (
        price_series.resolution.total_seconds() / 3600.0
        if price_series is not None and price_series.resolution else 0.0
    )
    weather_by_day = weather.by_day() if weather is not None else {}
    solar_by_day = _forecast_solar_by_day(generation, today)

    days: list[PriceDayStats] = []
    predicted_curves: dict[date, list[float]] = {}
    for horizon in range(horizon_days):
        day = today + timedelta(days=horizon)
        day_class = classify_day(day)

        settled = (
            slots_for_local_day(price_series.priced_slots, day, tz) if price_series is not None else []
        )
        covered = bool(settled) and slot_hours > 0 and (
            len(settled) * slot_hours >= 24.0 * _MIN_DAY_COVERAGE
        )

        if covered:
            stats = day_price_stats(settled, slot_hours, negative_threshold_eur_kwh)
            if stats is not None:
                neg_gen = negative_generation_for_day(
                    settled, generation, negative_threshold_eur_kwh
                )
                # The settled day names its loss-making slots exactly, so the
                # household draw across them is summed rather than estimated.
                loss_slots = [
                    s for s in settled
                    if s.spot_eur_kwh <= negative_threshold_eur_kwh
                ]
                absorbable, surplus = _absorption_split(
                    neg_gen,
                    _consumption_during(loss_slots, slot_hours, base_load, tz),
                    battery_capacity_kwh,
                )
                days.append(PriceDayStats(
                    day=day,
                    day_class=day_class,
                    horizon_days=horizon,
                    source=PRICE_STAT_SOURCE_ACTUAL,
                    peak_1h_eur_kwh=stats["peak_1h_eur_kwh"],
                    peak_4h_eur_kwh=stats["peak_4h_eur_kwh"],
                    trough_1h_eur_kwh=stats["trough_1h_eur_kwh"],
                    trough_4h_eur_kwh=stats["trough_4h_eur_kwh"],
                    negative_hours=stats["negative_hours"],
                    negative_generation_kwh=neg_gen,
                    confidence=1.0,
                    absorbable_kwh=absorbable,
                    surplus_kwh=surplus,
                ))
                continue

        wx = weather_by_day.get(day)
        solar_kwh = solar_by_day.get(day)

        # The engine predicts the day in clock order; the four published
        # bands are read off that one curve, so they cannot contradict each
        # other. A day it cannot speak for is left out rather than filled with
        # an average nobody asked for.
        engine_curve = (
            online_shape.predict_curve(
                shape_state,
                online_shape.build_inputs(
                    day, wx, latitude or 0.0, longitude or 0.0, tz,
                    is_holiday, neighbour_share,
                ),
            )
            if shape_state is not None else None
        )
        if engine_curve is None:
            continue

        predicted_curves[day] = engine_curve
        stats = _modelled_day_from_curve(engine_curve, negative_threshold_eur_kwh)
        peak_1h = stats["peak_1h_eur_kwh"]
        peak_4h = stats["peak_4h_eur_kwh"]
        trough_1h = stats["trough_1h_eur_kwh"]
        trough_4h = stats["trough_4h_eur_kwh"]
        negative_hours = max(0.0, min(24.0, stats["negative_hours"]))

        neg_gen = min(
            solar_kwh or 0.0,
            (solar_kwh or 0.0) * neg_ratio * negative_hours,
        )
        # Which hours go negative is unknown this far out, so the draw across
        # them is the profile's typical rate over their predicted duration.
        typical_kw = base_load.overall_median_kw if base_load is not None else 0.0
        absorbable, surplus = _absorption_split(
            neg_gen, typical_kw * negative_hours, battery_capacity_kwh,
        )
        days.append(PriceDayStats(
            day=day,
            day_class=day_class,
            horizon_days=horizon,
            source=PRICE_STAT_SOURCE_MODEL,
            peak_1h_eur_kwh=peak_1h,
            peak_4h_eur_kwh=peak_4h,
            trough_1h_eur_kwh=trough_1h,
            trough_4h_eur_kwh=trough_4h,
            negative_hours=negative_hours,
            negative_generation_kwh=neg_gen,
            confidence=PRICE_FORECAST_CONFIDENCE_DECAY ** horizon,
            absorbable_kwh=absorbable,
            surplus_kwh=surplus,
        ))

    return PriceForecast(
        days=tuple(days),
        computed_at=now,
        history_days=len(records),
        shape_days=shape_state.days_seen if shape_state is not None else 0,
        predicted_slots=_predicted_slots(predicted_curves, tz, tariff, is_holiday),
        error_slots=_error_slots(records, tz, today),
    )


def _predicted_slots(
    curves: dict[date, list[float]],
    local_tz: tzinfo,
    tariff: TariffConfig | None,
    is_holiday: Callable[[date], bool] | None,
) -> tuple[PriceCurvePoint, ...]:
    """Flatten the predicted day curves into an hourly series for the dashboard.

    Buy and sell are applied here rather than left to the consumer: the tariff
    formula knows about seasons, day types and time-of-use bands, and a chart
    drawing a predicted buy line beside a settled one must use the same rules
    for both.

    Args:
        curves: Predicted spot prices per local day, 24 values each.
        local_tz: Local timezone.
        tariff: User tariff; without it only the spot is meaningful and buy and
            sell repeat it.
        is_holiday: Public-holiday predicate for the tariff's holiday bands.

    Returns:
        Hourly points, ascending.
    """
    out: list[PriceCurvePoint] = []
    for day in sorted(curves):
        for hour, spot in enumerate(curves[day]):
            start = datetime.combine(day, time(hour=hour), tzinfo=local_tz)
            if tariff is None:
                out.append(PriceCurvePoint(start, spot, spot, spot))
                continue
            out.append(PriceCurvePoint(
                start=start,
                spot_eur_kwh=spot,
                buy_eur_kwh=tariff_module.buy_price(spot, tariff, start, is_holiday),
                sell_eur_kwh=tariff_module.sell_price(spot, tariff, start,
                                                      is_holiday=is_holiday),
            ))
    return tuple(out)


def _error_slots(
    records: Sequence[PriceDayRecord],
    local_tz: tzinfo,
    today: date,
    days_back: int = 2,
) -> tuple[PriceErrorPoint, ...]:
    """Return the hourly forecast error for the days that have both curves.

    Only the days the dashboard's own window covers are published — the store
    keeps far more, but nothing draws them.

    Args:
        records: Settled history.
        local_tz: Local timezone.
        today: Local date of the cycle.
        days_back: How many days before today to include.

    Returns:
        Hourly points, ascending.
    """
    cutoff = today - timedelta(days=days_back)
    out: list[PriceErrorPoint] = []
    for record in records:
        if record.day < cutoff or not record.curve or not record.predicted_curve:
            continue
        for hour, (actual, forecast) in enumerate(
            zip(record.curve, record.predicted_curve)
        ):
            out.append(PriceErrorPoint(
                start=datetime.combine(record.day, time(hour=hour), tzinfo=local_tz),
                forecast_eur_kwh=forecast,
                actual_eur_kwh=actual,
                error_eur_kwh=actual - forecast,
            ))
    return tuple(out)


def _forecast_solar_by_day(
    generation: GenerationSeries | None,
    today: date,
) -> dict[date, float]:
    """Map each horizon day to its expected PV total.

    Today and tomorrow come from the slot grid; days 2–6 come from the
    extended daily totals, which the forecast stage sums outside the price
    grid because no price slots exist that far ahead.

    Args:
        generation: Generation series, or None.
        today: Local date of the cycle.

    Returns:
        Dict of local date → expected kWh; empty when generation is absent.
    """
    if generation is None:
        return {}
    out = {
        today: generation.total_today_kwh,
        today + timedelta(days=1): generation.total_tomorrow_kwh,
    }
    for offset in range(2, 7):
        value = getattr(generation, f"total_d{offset}_kwh", 0.0)
        out[today + timedelta(days=offset)] = value
    return out


# ---------------------------------------------------------------------------
# History maintenance
# ---------------------------------------------------------------------------


def capture_feature_vintage(
    history: PriceCurveHistory,
    weather: WeatherForecastData | None,
    generation: GenerationSeries | None,
    today: date,
    lead_days: int = PRICE_FORECAST_VINTAGE_LEAD_DAYS,
) -> PriceCurveHistory | None:
    """Freeze the model features for the day ``lead_days`` ahead.

    Captured once per target day and never overwritten, so what the model is
    later trained on is exactly what was knowable at that lead time. See
    :class:`DayFeatureVintage` for why this matters.

    Args:
        history: Current history.
        weather: Daily weather forecast, or None.
        generation: Generation series supplying the solar feature, or None.
        today: Local date of the cycle.
        lead_days: Forecast lead time to capture at.

    Returns:
        Updated history, or None when nothing changed.
    """
    target = today + timedelta(days=lead_days)
    existing = history.vintage_by_day()
    if target in existing:
        return None

    wx = weather.by_day().get(target) if weather is not None else None
    solar = _forecast_solar_by_day(generation, today).get(target)
    if wx is None and solar is None:
        return None

    vintage = DayFeatureVintage(
        day=target,
        lead_days=lead_days,
        wind_speed_kmh=wx.wind_speed_kmh if wx else None,
        temperature_c=wx.temperature_c if wx else None,
        solar_kwh=solar,
        cloud_coverage_pct=wx.cloud_coverage_pct if wx else None,
    )
    vintages = sorted([*history.vintages, vintage], key=lambda v: v.day)
    return PriceCurveHistory(records=history.records, vintages=tuple(vintages))


def capture_prediction(
    history: PriceCurveHistory,
    forecast: PriceForecast | None,
    today: date,
    local_tz: tzinfo | None,
    lead_days: int = 1,
) -> PriceCurveHistory | None:
    """Freeze what the engine says about the day ``lead_days`` ahead.

    Stored once per target day and never revised, so the error published after
    the auction settles is what the forecast actually said at the time. This is
    the same contract ``capture_feature_vintage`` keeps for the features.

    Args:
        history: Current history.
        forecast: This cycle's forecast, or None.
        today: Local date of the cycle.
        local_tz: Local timezone.
        lead_days: Lead to capture at.

    Returns:
        Updated history, or None when nothing changed.
    """
    if forecast is None or not forecast.predicted_slots:
        return None
    target = today + timedelta(days=lead_days)
    if target in history.prediction_by_day():
        return None
    tz = local_tz or UTC
    by_hour = {
        point.start.astimezone(tz).hour: point.spot_eur_kwh
        for point in forecast.predicted_slots
        if point.start.astimezone(tz).date() == target
    }
    if len(by_hour) != online_shape.HOURS:
        return None
    frozen = PredictedDay(
        day=target,
        lead_days=lead_days,
        curve=tuple(by_hour[hour] for hour in range(online_shape.HOURS)),
    )
    return replace(
        history,
        predictions=tuple(sorted([*history.predictions, frozen], key=lambda p: p.day)),
    )


def settle_days(
    history: PriceCurveHistory,
    price_series: PriceSeries | None,
    generation: GenerationSeries | None,
    weather: WeatherForecastData | None,
    today: date,
    local_tz: tzinfo | None,
    negative_threshold_eur_kwh: float,
    retention_days: int,
    latitude: float | None = None,
    longitude: float | None = None,
    is_holiday: Callable[[date], bool] | None = None,
    neighbour_share: Callable[[date], float] | None = None,
) -> PriceCurveHistory | None:
    """Record any settled days not yet in the history, and prune.

    Today and yesterday are both considered: the day-ahead auction fixes a
    whole day in advance, so today's statistics are already final, and
    revisiting yesterday lets a restart backfill the day it missed.

    Features come from the frozen vintage for that day when one exists,
    falling back to the current forecast only for days that predate the
    install's first capture.

    Args:
        history: Current history.
        price_series: Settled prices, or None.
        generation: Generation series, or None.
        weather: Daily weather forecast, used only as the vintage fallback.
        today: Local date of the cycle.
        local_tz: Local timezone.
        negative_threshold_eur_kwh: Export break-even spot price.
        retention_days: Days of settled records to keep.
        latitude: Site latitude, for the shape engine's solar profile.
        longitude: Site longitude, same.
        is_holiday: Local public-holiday predicate.
        neighbour_share: Share of neighbouring zones on holiday.

    Returns:
        Updated history, or None when nothing changed. The online shape
        engine learns from each newly settled day here, which is the only
        place its state advances.
    """
    if price_series is None or not price_series.priced_slots:
        return None

    known = history.by_day()
    vintages = history.vintage_by_day()
    predictions = history.prediction_by_day()
    added: list[PriceDayRecord] = []
    for offset in (1, 0):
        day = today - timedelta(days=offset)
        if day in known:
            continue
        record = build_day_record(
            price_series, generation, weather, day, local_tz,
            negative_threshold_eur_kwh, is_holiday,
        )
        if record is None:
            continue
        predicted = predictions.get(day)
        if predicted is not None and len(predicted.curve) == online_shape.HOURS:
            record = replace(record, predicted_curve=predicted.curve)
        vintage = vintages.get(day)
        if vintage is not None:
            record = replace(
                record,
                wind_speed_kmh=vintage.wind_speed_kmh,
                temperature_c=vintage.temperature_c,
                solar_kwh=vintage.solar_kwh if vintage.solar_kwh is not None else record.solar_kwh,
                cloud_coverage_pct=(
                    vintage.cloud_coverage_pct
                    if vintage.cloud_coverage_pct is not None else record.cloud_coverage_pct
                ),
            )
        added.append(record)

    # Vintages are consumed once their day settles, and dropped if their day
    # passed without ever settling, so the list cannot grow without bound.
    kept_vintages = tuple(v for v in history.vintages if v.day > today)
    # A prediction is consumed by the day it was made for, and dropped if that
    # day passed without settling, so the list cannot grow without bound.
    kept_predictions = tuple(p for p in history.predictions if p.day > today)
    if (not added and len(kept_vintages) == len(history.vintages)
            and len(kept_predictions) == len(history.predictions)):
        return None

    cutoff = today - timedelta(days=retention_days)
    records = sorted(
        [r for r in (*history.records, *added) if r.day >= cutoff],
        key=lambda r: r.day,
    )
    updated = PriceCurveHistory(
        records=tuple(records),
        vintages=kept_vintages,
        predictions=kept_predictions,
        shape_state=history.shape_state,
    )
    return replace(
        updated,
        shape_state=advance_shape_state(
            updated, added, latitude, longitude, local_tz,
            is_holiday, neighbour_share,
        ),
    )


def advance_shape_state(
    history: PriceCurveHistory,
    added: Sequence[PriceDayRecord],
    latitude: float | None,
    longitude: float | None,
    local_tz: tzinfo | None,
    is_holiday: Callable[[date], bool] | None,
    neighbour_share: Callable[[date], float] | None,
) -> OnlineShapeState:
    """Fold newly settled days into the engine's state.

    A state that is missing — a first run, or one discarded because the
    effector table changed — is rebuilt by replaying the whole stored history,
    which costs a few hundred multiply-adds per day and happens once.

    Args:
        history: The updated history, including the new records.
        added: The records settled in this pass.
        latitude: Site latitude.
        longitude: Site longitude.
        local_tz: Local timezone.
        is_holiday: Local public-holiday predicate.
        neighbour_share: Share of neighbouring zones on holiday.

    Returns:
        The advanced state.
    """
    tz = local_tz or UTC
    if history.shape_state is None:
        return train_shape_state(
            history, latitude, longitude, tz, is_holiday, neighbour_share
        )
    state = history.shape_state
    for record in sorted(added, key=lambda r: r.day):
        if not record.curve:
            continue
        state = online_shape.absorb(
            state,
            _record_inputs(record, latitude, longitude, tz, is_holiday, neighbour_share),
            list(record.curve),
        )
    return state
