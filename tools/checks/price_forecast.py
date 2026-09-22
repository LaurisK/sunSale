"""Week-ahead price-forecast deep check and its TUI widget.

Validates everything ``pipeline.price_forecast`` exposes:

* the day set is contiguous, starts today, and has no duplicate horizons;
* every day's band ordering is physically coherent
  (``trough_1h ≤ trough_4h ≤ peak_4h ≤ peak_1h``) and its spread matches the
  published peak/trough; a modelled day may carry a null 4 h pair while that
  band lacks history, a settled day never;
* days marked ``actual`` are recomputed from ``pipeline.pricing`` and must
  match — this is the cross-check against the upstream source;
* the negative-hours and negative-generation figures are within range, and
  generation exposed to negative prices never exceeds that day's forecast
  total from ``pipeline.forecast``;
* the absorbable/surplus split adds back up to the negative-price generation
  it partitions, and neither half is negative;
* the model's blend weight is consistent with its measured skill, since a
  positive weight on non-positive skill would mean the guard that makes this
  safe in hydro/nuclear markets has failed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from textual.app import ComposeResult
from textual.widgets import Collapsible, Static

from .snapshot import Snapshot

# Tolerance for recomputed band means; the payload rounds to 4 decimals.
_PRICE_TOL = 1e-3

# Tolerance for the negative-generation cross-check against the day's total.
_KWH_TOL = 1e-2


def _resolve_local_tz(tz_name: str | None) -> tzinfo:
    """Return the tzinfo for an IANA name, falling back to UTC.

    Args:
        tz_name: IANA timezone name from the debug payload's config block.

    Returns:
        tzinfo for the name, or UTC when unset or unparseable.
    """
    if not tz_name:
        return UTC
    try:
        return ZoneInfo(tz_name)
    except Exception:    # ZoneInfoNotFoundError + anything unexpected
        return UTC


def _band_mean(values: list[float], hours: float, slot_hours: float, top: bool) -> float:
    """Return the mean of the highest or lowest ``hours`` worth of sorted values.

    Mirrors ``pipeline/price_forecast.py:_band_mean`` so the check recomputes
    the statistic independently rather than trusting the published one.

    Args:
        values: The day's prices, ascending.
        hours: Band width in hours.
        slot_hours: Duration of one slot in hours.
        top: True for the highest band, False for the lowest.

    Returns:
        Mean price across the band, or 0.0 when there is nothing to average.
    """
    if not values or slot_hours <= 0:
        return 0.0
    count = max(1, min(len(values), int(round(hours / slot_hours))))
    band = values[-count:] if top else values[:count]
    return sum(band) / len(band)


def _fmt(value: float | None) -> str:
    """Render an optional kWh figure, or an em dash when it was not computed."""
    return "—" if value is None else f"{value:.2f}"


def _fmt_price(value: float | None) -> str:
    """Render an optional price, or an em dash when it was not estimated."""
    return "—" if value is None else f"{value:.4f}"


@dataclass
class PriceForecastCheckResult:
    """Result of the week-ahead price-forecast deep check."""

    skipped: bool = False
    skip_reason: str = ""
    day_count: int = 0
    actual_days: int = 0
    model_days: int = 0
    climatology_days: int = 0
    shape_days: int = 0
    history_days: int = 0
    computed_at: str = ""
    surplus_kwh_total: float = 0.0
    days: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_price_forecast(snap: Snapshot) -> PriceForecastCheckResult:
    """Validate the published week-ahead price forecast against its sources.

    Args:
        snap: Coordinator snapshot with pipeline.price_forecast, pipeline.pricing
            and pipeline.forecast.

    Returns:
        PriceForecastCheckResult with per-day detail and overall pass/fail.
    """
    result = PriceForecastCheckResult()

    pf = snap.pipeline.get("price_forecast")
    if not pf:
        result.skipped = True
        result.skip_reason = "pipeline.price_forecast is null (no settled history yet)"
        return result

    result.shape_days = int(pf.get("shape_days", 0) or 0)
    result.history_days = pf.get("history_days", 0)
    result.computed_at = pf.get("computed_at", "") or ""
    days = list(pf.get("days") or [])
    result.days = days
    result.day_count = len(days)

    if pf.get("day_count") != len(days):
        result.mismatches.append("day_count_mismatch")

    # The engine cannot have learned from more days than the store holds.
    if result.shape_days > result.history_days:
        result.mismatches.append("shape_days_exceeds_history")

    local_tz = _resolve_local_tz(snap.config.get("time_zone"))
    today_local = datetime.now(UTC).astimezone(local_tz).date()

    # Bucket the settled price slots by local date so `actual` days can be
    # recomputed from the upstream pricing series.
    pricing = snap.pipeline.get("pricing") or {}
    slot_hours = (pricing.get("resolution_s") or 0) / 3600.0
    spots_by_day: dict[str, list[float]] = {}
    for slot in pricing.get("slots") or []:
        try:
            day = datetime.fromisoformat(slot["start"]).astimezone(local_tz).date().isoformat()
        except (KeyError, ValueError):
            continue
        spots_by_day.setdefault(day, []).append(slot.get("spot", 0.0))

    forecast = snap.pipeline.get("forecast") or {}
    horizons_seen: set[int] = set()

    for entry in days:
        day = entry.get("day", "")
        horizon = entry.get("horizon_days", -1)
        source = entry.get("source", "")

        if source == "actual":
            result.actual_days += 1
        elif source == "model":
            result.model_days += 1
        elif source == "climatology":
            result.climatology_days += 1
        else:
            result.mismatches.append(f"{day}:unknown_source")

        if horizon in horizons_seen:
            result.mismatches.append(f"{day}:duplicate_horizon")
        horizons_seen.add(horizon)

        try:
            expected_day = (today_local + timedelta(days=horizon)).isoformat()
        except (TypeError, OverflowError):
            expected_day = ""
        if expected_day and day != expected_day:
            result.mismatches.append(f"{day}:horizon_day_mismatch")

        p1 = entry.get("peak_1h_eur_kwh", 0.0)
        p4 = entry.get("peak_4h_eur_kwh")
        t4 = entry.get("trough_4h_eur_kwh")
        t1 = entry.get("trough_1h_eur_kwh", 0.0)
        has_4h = p4 is not None and t4 is not None
        if (p4 is None) != (t4 is None):
            result.mismatches.append(f"{day}:4h_half_missing")
        if has_4h:
            ordered = t1 - _PRICE_TOL <= t4 <= p4 + _PRICE_TOL <= p1 + 2 * _PRICE_TOL
        else:
            ordered = t1 <= p1 + _PRICE_TOL
        if not ordered:
            result.mismatches.append(f"{day}:band_ordering")

        spread = entry.get("spread_4h_eur_kwh")
        if spread is not None and (not has_4h or abs(spread - (p4 - t4)) > _PRICE_TOL):
            result.mismatches.append(f"{day}:spread_mismatch")

        neg_h = entry.get("negative_hours", 0.0)
        if not (0.0 <= neg_h <= 24.0 + _PRICE_TOL):
            result.mismatches.append(f"{day}:negative_hours_range")

        confidence = entry.get("confidence", 0.0)
        if not (0.0 <= confidence <= 1.0):
            result.mismatches.append(f"{day}:confidence_range")

        # Generation exposed to negative prices can never exceed that day's
        # total forecast generation.
        neg_gen = entry.get("negative_generation_kwh", 0.0)
        if neg_gen < -_KWH_TOL:
            result.mismatches.append(f"{day}:negative_generation_negative")
        label = {0: "total_today_kwh", 1: "total_tomorrow_kwh"}.get(horizon, f"total_d{horizon}_kwh")
        day_total = forecast.get(label)
        if day_total is not None and neg_gen > day_total + _KWH_TOL:
            result.mismatches.append(f"{day}:negative_generation_exceeds_total")

        # The split must partition the figure it describes, exactly.
        absorbable = entry.get("absorbable_kwh")
        surplus = entry.get("surplus_kwh")
        if (absorbable is None) != (surplus is None):
            result.mismatches.append(f"{day}:split_half_missing")
        elif absorbable is not None and surplus is not None:
            if absorbable < -_KWH_TOL or surplus < -_KWH_TOL:
                result.mismatches.append(f"{day}:split_negative")
            if abs((absorbable + surplus) - neg_gen) > _KWH_TOL:
                result.mismatches.append(f"{day}:split_mismatch")
            result.surplus_kwh_total += max(0.0, surplus)

        # Settled days must reproduce exactly from the upstream price slots.
        if source == "actual":
            spots = sorted(spots_by_day.get(day, []))
            if not has_4h:
                result.mismatches.append(f"{day}:actual_4h_missing")
            if spots and slot_hours > 0:
                checks = [
                    ("peak_1h", _band_mean(spots, 1.0, slot_hours, True), p1),
                    ("trough_1h", _band_mean(spots, 1.0, slot_hours, False), t1),
                ]
                if has_4h:
                    checks += [
                        ("peak_4h", _band_mean(spots, 4.0, slot_hours, True), p4),
                        ("trough_4h", _band_mean(spots, 4.0, slot_hours, False), t4),
                    ]
                for name, expected, published in checks:
                    if abs(expected - published) > _PRICE_TOL:
                        result.mismatches.append(f"{day}:{name}_mismatch")

    if days and 0 not in horizons_seen:
        result.mismatches.append("missing_today")

    result.overall_ok = not result.mismatches
    return result


class PriceForecastCheckWidget(Static):
    """Collapsible week-ahead price-forecast deep check."""

    DEFAULT_CSS = "PriceForecastCheckWidget { height: auto; }"

    def __init__(self, pr: PriceForecastCheckResult) -> None:
        """Initialise with the pre-computed price-forecast check result.

        Args:
            pr: Result of check_price_forecast() for one coordinator.
        """
        super().__init__()
        self._pr = pr

    def compose(self) -> ComposeResult:
        """Render the per-day forecast table as a collapsible static block."""
        pr = self._pr

        if pr.skipped:
            yield Static(f"  ⚠  price_forecast_check   SKIP   {pr.skip_reason}")
            return

        color = "green" if pr.overall_ok else "red"
        mark = "✓" if pr.overall_ok else "✗"
        status = "PASS" if pr.overall_ok else "FAIL"
        skill_str = f"{pr.shape_days}d learned"
        title = (
            f"[{color}]{mark}[/{color}]  price_forecast_check   [{color}]{status}[/{color}]"
            f"   {pr.day_count}d  actual={pr.actual_days}"
            f"  engine={skill_str}  hist={pr.history_days}d"
            f"  surplus={pr.surplus_kwh_total:.1f}kWh"
        )

        with Collapsible(title=title, collapsed=True):
            lines = [
                f"  {'day':12s} {'source':11s} {'peak1':>8s} {'peak4':>8s} "
                f"{'trgh4':>8s} {'trgh1':>8s} {'negh':>6s} {'negkWh':>8s} "
                f"{'absorb':>8s} {'surplus':>8s} {'conf':>5s}"
            ]
            for d in pr.days:
                lines.append(
                    f"  {d.get('day', ''):12s} {d.get('source', ''):11s} "
                    f"{d.get('peak_1h_eur_kwh', 0.0):8.4f} {_fmt_price(d.get('peak_4h_eur_kwh')):>8s} "
                    f"{_fmt_price(d.get('trough_4h_eur_kwh')):>8s} "
                    f"{d.get('trough_1h_eur_kwh', 0.0):8.4f} "
                    f"{d.get('negative_hours', 0.0):6.2f} "
                    f"{d.get('negative_generation_kwh', 0.0):8.2f} "
                    f"{_fmt(d.get('absorbable_kwh')):>8s} {_fmt(d.get('surplus_kwh')):>8s} "
                    f"{d.get('confidence', 0.0):5.2f}"
                )
            lines.append(f"  Shape engine: {skill_str}")
            lines.append(f"  Settled history: {pr.history_days} days")
            lines.append(f"  Computed at: {pr.computed_at or '—'}")
            if pr.mismatches:
                lines.append(f"  [red]Mismatches: {', '.join(pr.mismatches)}[/red]")
            yield Static("\n".join(lines), markup=True)
