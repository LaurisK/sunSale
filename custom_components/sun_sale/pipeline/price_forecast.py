"""Week-ahead price forecast: daily peak / trough / negative-price statistics.

The day-ahead auction covers at most ~48 h, shrinking to ~24 h just before
publication, so every decision about holding energy past that edge is made
blind. This module extends the view to a full week on the same axis as the
week-ahead *generation* forecast, publishing four statistics per local day:
the mean of the highest and lowest 1 h and 3 h of the day, how many hours sit
at or below the export break-even, and how much PV is expected to land in
them.

Two properties drive the design, both established by the multi-region study in
``docs/price_forecast_week_ahead.md``:

**The baseline is a trailing day-class median, and it is hard to beat.** A
naive weather regression measured *worse* than doing nothing (−5.9 %) until it
was reframed to predict the anomaly against that baseline (+33.7 %). So the
climatology is not a fallback bolted on afterwards — it is the prediction, and
the weather model only ever contributes a correction on top of it.

**The weather model does not work in every market.** Where variable renewables
set the price it is worth +25…+37 %; where dispatchable hydro or nuclear sets
it (reservoir water value, outage scheduling) it is worth −16 %, because price
follows an operator's optimisation rather than the weather. Since sunSale is
region-agnostic and cannot know which case an install is in, the model's
contribution is **weighted by its own measured out-of-sample skill** against
the climatology baseline (``_backtest_skill``). An install in Norway scores
zero or negative skill, the weight collapses to zero, and the published
forecast is the climatology — no per-region configuration, no special casing.

Pure Python — no Home Assistant imports, no third-party deps.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, tzinfo

from ..contract.const import (
    PRICE_FORECAST_CONFIDENCE_DECAY,
    PRICE_FORECAST_FULL_SKILL,
    PRICE_FORECAST_HORIZON_DAYS,
    PRICE_FORECAST_MAX_MODEL_WEIGHT,
    PRICE_FORECAST_MIN_CLIMATOLOGY_DAYS,
    PRICE_FORECAST_MIN_MODEL_DAYS,
    PRICE_FORECAST_MIN_SKILL,
    PRICE_FORECAST_RIDGE_LAMBDA,
    PRICE_FORECAST_SKILL_WINDOW_DAYS,
    PRICE_FORECAST_TRAIN_DAYS,
    PRICE_FORECAST_VINTAGE_LEAD_DAYS,
)
from ..contract.models import (
    PRICE_STAT_SOURCE_ACTUAL,
    PRICE_STAT_SOURCE_CLIMATOLOGY,
    PRICE_STAT_SOURCE_MODEL,
    BaseLoadProfile,
    DayClass,
    DayFeatureVintage,
    GenerationSeries,
    PriceCurveHistory,
    PriceDayRecord,
    PriceDayStats,
    PriceForecast,
    PriceSeries,
    WeatherForecastData,
)
from .profitability import classify_day

# Targets predicted by the model, in the order they are reported.
_TARGETS: tuple[str, ...] = (
    "peak_1h_eur_kwh",
    "peak_3h_eur_kwh",
    "trough_1h_eur_kwh",
    "trough_3h_eur_kwh",
    "negative_hours",
)

# Band widths, in hours, behind the 1 h / 3 h statistics.
_BAND_1H = 1.0
_BAND_3H = 3.0

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

# Cache for the rolling skill backtest, which is far too expensive to redo on
# every 5-minute cycle and only changes when a new day is settled into the
# history. Keyed by the shape of the history it was computed from.
_skill_cache: dict[tuple, tuple[float | None, float]] = {}


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
        "peak_3h_eur_kwh": _band_mean(spot, _BAND_3H, slot_hours, top=True),
        "trough_1h_eur_kwh": _band_mean(spot, _BAND_1H, slot_hours, top=False),
        "trough_3h_eur_kwh": _band_mean(spot, _BAND_3H, slot_hours, top=False),
        "negative_hours": below * slot_hours,
        "mean_eur_kwh": sum(spot) / len(spot),
    }


def export_break_even(
    sell_distribution_fee: float,
    sell_markup: float,
) -> float:
    """Return the spot price below which exporting stops paying.

    Sell price is ``(spot − fee − markup) × (1 − tax)``, so the sign flips at
    ``fee + markup`` — not at zero. On a strict ``spot < 0`` test most
    loss-making hours would go uncounted; across the markets studied, hours
    below the wedge outnumber hours below zero by roughly 3:1.

    Args:
        sell_distribution_fee: Per-kWh distribution fee on the sell side.
        sell_markup: Per-kWh supplier markup on the sell side.

    Returns:
        Spot price in EUR/kWh at which the effective sell price reaches zero.
    """
    return sell_distribution_fee + sell_markup


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
    slots = slots_for_local_day(price_series.slots, day, local_tz)
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
        day_class=classify_day(day),
        peak_1h_eur_kwh=stats["peak_1h_eur_kwh"],
        peak_3h_eur_kwh=stats["peak_3h_eur_kwh"],
        trough_1h_eur_kwh=stats["trough_1h_eur_kwh"],
        trough_3h_eur_kwh=stats["trough_3h_eur_kwh"],
        negative_hours=stats["negative_hours"],
        mean_eur_kwh=stats["mean_eur_kwh"],
        wind_speed_kmh=wx.wind_speed_kmh if wx else None,
        temperature_c=wx.temperature_c if wx else None,
        solar_kwh=solar_kwh,
        negative_generation_kwh=negative_generation_for_day(
            slots, generation, negative_threshold_eur_kwh
        ),
    )


# ---------------------------------------------------------------------------
# Ridge regression on daily anomalies
# ---------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float:
    """Return the median of a non-empty sequence."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _solve_ridge(
    rows: Sequence[Sequence[float]],
    targets: Sequence[float],
    lam: float,
) -> list[float] | None:
    """Solve a small ridge-regularised least-squares problem.

    Builds the normal equations and solves them by Gaussian elimination with
    partial pivoting. The design is at most a handful of columns wide, so the
    cubic solve is irrelevant next to accumulating the matrix.

    Args:
        rows: Design matrix, one row per observation.
        targets: Response vector, same length as ``rows``.
        lam: Ridge penalty; applied to every column except the intercept.

    Returns:
        Coefficient vector, or None when the system is singular or empty.
    """
    if not rows or len(rows) != len(targets):
        return None
    width = len(rows[0])
    if width == 0 or len(rows) < width:
        return None

    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for row, y in zip(rows, targets):
        for i in range(width):
            ri = row[i]
            if ri:
                xty[i] += ri * y
                target_row = xtx[i]
                for j in range(i, width):
                    target_row[j] += ri * row[j]
    for i in range(width):
        for j in range(i):
            xtx[i][j] = xtx[j][i]
    # The intercept is left unpenalised so the fit can still centre itself.
    for i in range(1, width):
        xtx[i][i] += lam

    aug = [[*xtx[i], xty[i]] for i in range(width)]
    for col in range(width):
        pivot = max(range(col, width), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_val = aug[col][col]
        for r in range(width):
            if r == col:
                continue
            factor = aug[r][col] / pivot_val
            if factor:
                for c in range(col, width + 1):
                    aug[r][c] -= factor * aug[col][c]
    return [aug[i][width] / aug[i][i] for i in range(width)]


def _feature_baseline(records: Sequence[PriceDayRecord]) -> dict[str, float]:
    """Return the mean of each weather feature across the training records."""
    def _mean(getter: Callable[[PriceDayRecord], float | None]) -> float:
        vals = [v for v in (getter(r) for r in records) if v is not None]
        return sum(vals) / len(vals) if vals else 0.0

    return {
        "wind": _mean(lambda r: r.wind_speed_kmh),
        "temp": _mean(lambda r: r.temperature_c),
        "solar": _mean(lambda r: r.solar_kwh),
    }


def _features(
    day_class: DayClass,
    wind_speed_kmh: float | None,
    temperature_c: float | None,
    solar_kwh: float | None,
    base: dict[str, float],
) -> list[float]:
    """Build the design row for one day.

    Weather enters as an anomaly against the training-window mean rather than
    as a level: the level is already carried by the day-class climatology, and
    regressing on it directly is what made the naive specification lose to the
    baseline. The weekend interaction is kept because demand responds to wind
    differently when the working-day load shape is absent.

    Args:
        day_class: Day bucket.
        wind_speed_kmh: Daily mean wind speed, or None.
        temperature_c: Daily mean temperature, or None.
        solar_kwh: That day's PV forecast total, or None.
        base: Feature baseline from ``_feature_baseline``.

    Returns:
        Design row, intercept first.
    """
    wind = (wind_speed_kmh if wind_speed_kmh is not None else base["wind"]) - base["wind"]
    temp = (temperature_c if temperature_c is not None else base["temp"]) - base["temp"]
    solar = (solar_kwh if solar_kwh is not None else base["solar"]) - base["solar"]
    weekend = 1.0 if day_class is DayClass.WEEKEND else 0.0
    return [1.0, wind, temp, solar / 10.0, weekend, wind * weekend]


def _class_medians(
    records: Sequence[PriceDayRecord],
    target: str,
) -> dict[DayClass, float]:
    """Return the per-day-class median of one target across the records."""
    buckets: dict[DayClass, list[float]] = {}
    for rec in records:
        buckets.setdefault(rec.day_class, []).append(getattr(rec, target))
    return {cls: _median(vals) for cls, vals in buckets.items() if vals}


def _climatology_value(
    medians: dict[DayClass, float],
    day_class: DayClass,
) -> float | None:
    """Return the baseline for a day class, falling back across classes.

    Args:
        medians: Per-class medians from ``_class_medians``.
        day_class: Class being predicted.

    Returns:
        Median for the class, else the weekday median, else the mean of what
        exists; None when the mapping is empty.
    """
    if not medians:
        return None
    if day_class in medians:
        return medians[day_class]
    if DayClass.WEEKDAY in medians:
        return medians[DayClass.WEEKDAY]
    return sum(medians.values()) / len(medians)


def _fit_target(
    records: Sequence[PriceDayRecord],
    target: str,
    base: dict[str, float],
    medians: dict[DayClass, float],
) -> list[float] | None:
    """Fit the anomaly model for one target over the training records."""
    rows: list[list[float]] = []
    ys: list[float] = []
    for rec in records:
        baseline = _climatology_value(medians, rec.day_class)
        if baseline is None:
            continue
        rows.append(_features(
            rec.day_class, rec.wind_speed_kmh, rec.temperature_c, rec.solar_kwh, base
        ))
        ys.append(getattr(rec, target) - baseline)
    return _solve_ridge(rows, ys, PRICE_FORECAST_RIDGE_LAMBDA)


def _predict(
    beta: Sequence[float] | None,
    row: Sequence[float],
    baseline: float,
) -> float:
    """Return the baseline plus the model's anomaly correction."""
    if beta is None:
        return baseline
    return baseline + sum(b * x for b, x in zip(beta, row))


# ---------------------------------------------------------------------------
# Skill measurement — the guard that makes this safe to ship everywhere
# ---------------------------------------------------------------------------


def _backtest_skill(records: Sequence[PriceDayRecord]) -> tuple[float | None, float]:
    """Measure the model's out-of-sample skill and derive its blend weight.

    Walks the most recent ``PRICE_FORECAST_SKILL_WINDOW_DAYS`` settled days.
    For each, fits on the days strictly before it and compares the model's
    absolute error against the climatology baseline's, pooled across targets
    after normalising each target by its own baseline error (so a large-
    magnitude target cannot dominate a small one).

    Args:
        records: Settled history, sorted ascending by day.

    Returns:
        Tuple of (skill, weight). Skill is the fractional MAE improvement over
        climatology — negative when the model is losing — or None when there is
        too little history to judge. Weight is the resulting model contribution
        in [0, 1], zero whenever skill is not positive.
    """
    if len(records) < PRICE_FORECAST_MIN_MODEL_DAYS:
        return None, 0.0

    cache_key = (len(records), records[-1].day, records[0].day)
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]

    start = max(PRICE_FORECAST_MIN_CLIMATOLOGY_DAYS,
                len(records) - PRICE_FORECAST_SKILL_WINDOW_DAYS)
    err_clim = 0.0
    err_model = 0.0
    scored = 0
    for i in range(start, len(records)):
        train = records[max(0, i - PRICE_FORECAST_TRAIN_DAYS):i]
        if len(train) < PRICE_FORECAST_MIN_CLIMATOLOGY_DAYS:
            continue
        actual = records[i]
        base = _feature_baseline(train)
        row = _features(
            actual.day_class, actual.wind_speed_kmh, actual.temperature_c,
            actual.solar_kwh, base,
        )
        for target in _TARGETS:
            medians = _class_medians(train, target)
            baseline = _climatology_value(medians, actual.day_class)
            if baseline is None:
                continue
            truth = getattr(actual, target)
            # Normalise so every target contributes comparably to the pooled
            # score regardless of its units (EUR/kWh versus hours).
            scale = max(abs(baseline), 1e-3)
            beta = _fit_target(train, target, base, medians)
            err_clim += abs(truth - baseline) / scale
            err_model += abs(truth - _predict(beta, row, baseline)) / scale
            scored += 1

    if not scored or err_clim <= 0:
        result: tuple[float | None, float] = (None, 0.0)
    else:
        skill = 1.0 - (err_model / err_clim)
        # A barely-positive score is noise, not evidence — see
        # PRICE_FORECAST_MIN_SKILL. Below the floor the model gets nothing; above
        # it the weight ramps in, and the baseline always keeps a stake.
        if skill <= PRICE_FORECAST_MIN_SKILL:
            weight = 0.0
        else:
            span = max(PRICE_FORECAST_FULL_SKILL - PRICE_FORECAST_MIN_SKILL, 1e-9)
            ramp = min(1.0, (skill - PRICE_FORECAST_MIN_SKILL) / span)
            weight = ramp * PRICE_FORECAST_MAX_MODEL_WEIGHT
        result = (skill, weight)

    _skill_cache.clear()
    _skill_cache[cache_key] = result
    return result


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
) -> PriceForecast:
    """Build the week-ahead price forecast.

    Days already covered by the settled auction are reported verbatim from
    ``price_series``; the rest are predicted from the day-class climatology,
    optionally corrected by the weather model in proportion to its measured
    skill (see the module docstring).

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

    Returns:
        PriceForecast covering ``horizon_days`` local days; empty when there is
        neither settled data nor usable history.
    """
    if now is None:
        now = datetime.now(UTC)
    tz = local_tz or UTC
    today = now.astimezone(tz).date()

    records = tuple(history.records) if history is not None else ()
    train = records[-PRICE_FORECAST_TRAIN_DAYS:]
    have_climatology = len(train) >= PRICE_FORECAST_MIN_CLIMATOLOGY_DAYS

    skill, weight = _backtest_skill(records)
    base = _feature_baseline(train) if have_climatology else {"wind": 0.0, "temp": 0.0, "solar": 0.0}
    medians_by_target = (
        {t: _class_medians(train, t) for t in _TARGETS} if have_climatology else {}
    )
    betas = (
        {t: _fit_target(train, t, base, medians_by_target[t]) for t in _TARGETS}
        if have_climatology and weight > 0 else {}
    )
    neg_ratio = _negative_generation_ratio(train)

    slot_hours = (
        price_series.resolution.total_seconds() / 3600.0
        if price_series is not None and price_series.resolution else 0.0
    )
    weather_by_day = weather.by_day() if weather is not None else {}
    solar_by_day = _forecast_solar_by_day(generation, today)

    days: list[PriceDayStats] = []
    for horizon in range(horizon_days):
        day = today + timedelta(days=horizon)
        day_class = classify_day(day)

        settled = (
            slots_for_local_day(price_series.slots, day, tz) if price_series is not None else []
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
                    peak_3h_eur_kwh=stats["peak_3h_eur_kwh"],
                    trough_1h_eur_kwh=stats["trough_1h_eur_kwh"],
                    trough_3h_eur_kwh=stats["trough_3h_eur_kwh"],
                    negative_hours=stats["negative_hours"],
                    negative_generation_kwh=neg_gen,
                    confidence=1.0,
                    absorbable_kwh=absorbable,
                    surplus_kwh=surplus,
                ))
                continue

        if not have_climatology:
            continue

        wx = weather_by_day.get(day)
        solar_kwh = solar_by_day.get(day)
        row = _features(
            day_class,
            wx.wind_speed_kmh if wx else None,
            wx.temperature_c if wx else None,
            solar_kwh,
            base,
        )
        predicted: dict[str, float] = {}
        for target in _TARGETS:
            baseline = _climatology_value(medians_by_target[target], day_class)
            if baseline is None:
                predicted[target] = 0.0
                continue
            modelled = _predict(betas.get(target), row, baseline)
            predicted[target] = (1.0 - weight) * baseline + weight * modelled

        negative_hours = max(0.0, min(24.0, predicted["negative_hours"]))
        peak_3h = predicted["peak_3h_eur_kwh"]
        trough_3h = predicted["trough_3h_eur_kwh"]
        # A predicted trough above its peak is physically impossible and can
        # occur when two independently-fitted targets disagree at the edges.
        if trough_3h > peak_3h:
            trough_3h = peak_3h = (trough_3h + peak_3h) / 2.0

        # With no weather for this day every feature sits at its baseline, so
        # the model's correction is identically zero — report that honestly as
        # climatology rather than implying a weather-informed prediction.
        modelled = weight > 0 and wx is not None
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
            source=PRICE_STAT_SOURCE_MODEL if modelled else PRICE_STAT_SOURCE_CLIMATOLOGY,
            peak_1h_eur_kwh=max(predicted["peak_1h_eur_kwh"], peak_3h),
            peak_3h_eur_kwh=peak_3h,
            trough_1h_eur_kwh=min(predicted["trough_1h_eur_kwh"], trough_3h),
            trough_3h_eur_kwh=trough_3h,
            negative_hours=negative_hours,
            negative_generation_kwh=neg_gen,
            confidence=PRICE_FORECAST_CONFIDENCE_DECAY ** horizon,
            absorbable_kwh=absorbable,
            surplus_kwh=surplus,
        ))

    return PriceForecast(
        days=tuple(days),
        computed_at=now,
        model_skill=skill,
        model_weight=weight,
        history_days=len(records),
    )


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
    )
    vintages = sorted([*history.vintages, vintage], key=lambda v: v.day)
    return PriceCurveHistory(records=history.records, vintages=tuple(vintages))


def settle_days(
    history: PriceCurveHistory,
    price_series: PriceSeries | None,
    generation: GenerationSeries | None,
    weather: WeatherForecastData | None,
    today: date,
    local_tz: tzinfo | None,
    negative_threshold_eur_kwh: float,
    retention_days: int,
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

    Returns:
        Updated history, or None when nothing changed.
    """
    if price_series is None or not price_series.slots:
        return None

    known = history.by_day()
    vintages = history.vintage_by_day()
    added: list[PriceDayRecord] = []
    for offset in (1, 0):
        day = today - timedelta(days=offset)
        if day in known:
            continue
        record = build_day_record(
            price_series, generation, weather, day, local_tz, negative_threshold_eur_kwh
        )
        if record is None:
            continue
        vintage = vintages.get(day)
        if vintage is not None:
            record = replace(
                record,
                wind_speed_kmh=vintage.wind_speed_kmh,
                temperature_c=vintage.temperature_c,
                solar_kwh=vintage.solar_kwh if vintage.solar_kwh is not None else record.solar_kwh,
            )
        added.append(record)

    # Vintages are consumed once their day settles, and dropped if their day
    # passed without ever settling, so the list cannot grow without bound.
    kept_vintages = tuple(v for v in history.vintages if v.day > today)
    if not added and len(kept_vintages) == len(history.vintages):
        return None

    cutoff = today - timedelta(days=retention_days)
    records = sorted(
        [r for r in (*history.records, *added) if r.day >= cutoff],
        key=lambda r: r.day,
    )
    return PriceCurveHistory(records=tuple(records), vintages=kept_vintages)
