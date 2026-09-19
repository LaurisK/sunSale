"""Price-level deep check and its TUI widget.

Recomputes the cheap / normal / expensive classification independently from
``pipeline.pricing`` (the buy prices it consumes) and the config the series
reports, then compares every exposed field: the slot set and buy prices, each
local day's thresholds and slot count, every slot's level and rank, the level
counts, and the ``current`` status the HA entities publish.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

from textual.app import ComposeResult
from textual.widgets import Collapsible, Static

from .snapshot import Snapshot

_PRICE_TOL = 1e-4
_RANK_TOL = 1e-3
_MAX_LISTED = 8


def _resolve_local_tz(tz_name: str | None) -> tzinfo:
    """Return the tzinfo for an IANA name, falling back to UTC."""
    if not tz_name:
        return UTC
    try:
        return ZoneInfo(tz_name)
    except Exception:    # ZoneInfoNotFoundError + anything unexpected
        return UTC


def _share_count(n: int, share: float) -> int:
    """Mirror of the integration's share → slot-count rule (ceil, 0 for a 0 share)."""
    return 0 if share <= 0 else min(n, math.ceil(n * share - 1e-9))


def _thresholds(prices: list[float], cheap_share: float, expensive_share: float) -> tuple:
    """Mirror of ``pipeline/price_level.day_thresholds`` incl. the half-a-tie-group rule.

    Args:
        prices: One day's buy prices, sorted ascending.
        cheap_share: Fraction of the day counted cheap.
        expensive_share: Fraction of the day counted expensive.

    Returns:
        (cheap_threshold, expensive_threshold), either possibly None.
    """
    n = len(prices)
    cheap_n, expensive_n = _share_count(n, cheap_share), _share_count(n, expensive_share)
    cheap = expensive = None
    if cheap_n:
        edge = prices[cheap_n - 1]
        lo, hi = bisect_left(prices, edge), bisect_right(prices, edge)
        cheap = edge if (cheap_n - lo) * 2 >= hi - lo else (prices[lo - 1] if lo > 0 else None)
    if expensive_n:
        edge = prices[n - expensive_n]
        lo, hi = bisect_left(prices, edge), bisect_right(prices, edge)
        expensive = edge if (hi - (n - expensive_n)) * 2 >= hi - lo else (prices[hi] if hi < n else None)
    return cheap, expensive


def _expected_level(buy: float, cheap: float | None, expensive: float | None, cfg: dict) -> str:
    """Mirror of ``pipeline/price_level.level_for`` on the debug-payload config."""
    below, above = cfg.get("cheap_below"), cfg.get("expensive_above")
    abs_cheap = below is not None and buy <= below
    abs_expensive = above is not None and buy >= above
    if abs_cheap != abs_expensive:
        return "cheap" if abs_cheap else "expensive"
    rank_cheap = cheap is not None and buy <= cheap
    rank_expensive = expensive is not None and buy >= expensive
    if rank_cheap and not rank_expensive:
        return "cheap"
    if rank_expensive and not rank_cheap:
        return "expensive"
    return "normal"


def _differs(a: float | None, b: float | None) -> bool:
    """Return True when two optional prices differ beyond tolerance."""
    if a is None or b is None:
        return a is not b
    return abs(a - b) > _PRICE_TOL


@dataclass
class PriceLevelCheckResult:
    """Result of the price-level deep check."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    day_count: int = 0
    counts: dict = field(default_factory=dict)
    current_level: str | None = None
    config: dict = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True

    def fail(self, msg: str) -> None:
        """Record one mismatch and mark the check failed."""
        self.mismatches.append(msg)
        self.overall_ok = False


def check_price_level(snap: Snapshot) -> PriceLevelCheckResult:
    """Validate pipeline.price_level against an independent recomputation from pipeline.pricing.

    Args:
        snap: Coordinator snapshot containing pipeline.price_level and pipeline.pricing.

    Returns:
        PriceLevelCheckResult with the counts and every mismatch found.
    """
    result = PriceLevelCheckResult()
    pl = snap.pipeline.get("price_level")
    pricing = snap.pipeline.get("pricing")
    if not pl:
        result.skipped = True
        result.skip_reason = "pipeline.price_level is null (no prices on this install)"
        return result
    if not pricing:
        result.skipped = True
        result.skip_reason = "pipeline.pricing is null — nothing to recompute from"
        return result

    cfg = pl.get("config") or {}
    result.config = cfg
    slots = pl.get("slots") or []
    result.slot_count = len(slots)
    result.day_count = len(pl.get("days") or [])
    result.counts = dict(pl.get("counts") or {})
    result.current_level = (pl.get("current") or {}).get("level")
    local_tz = _resolve_local_tz(snap.config.get("time_zone"))

    # 1. Consumes: the same slots and buy prices as pipeline.pricing.
    price_slots = pricing.get("slots") or []
    if [s["start"] for s in slots] != [s["start"] for s in price_slots]:
        result.fail(f"slot set differs from pipeline.pricing ({len(slots)} vs {len(price_slots)} slots)")
        return result
    for s, p in zip(slots, price_slots, strict=True):
        if abs(s["buy"] - p["buy"]) > _PRICE_TOL:
            result.fail(f"{s['start']}: buy {s['buy']} ≠ pricing {p['buy']}")

    # 2. Per-day thresholds, recomputed from the pricing buys by local date.
    by_day: dict[date, list[float]] = {}
    for p in price_slots:
        by_day.setdefault(datetime.fromisoformat(p["start"]).astimezone(local_tz).date(), []).append(p["buy"])
    expected_days: dict[str, tuple[int, float | None, float | None, list[float]]] = {}
    for d, buys in by_day.items():
        prices = sorted(buys)
        cheap, expensive = _thresholds(prices, cfg.get("cheap_share", 0.0), cfg.get("expensive_share", 0.0))
        expected_days[d.isoformat()] = (len(prices), cheap, expensive, prices)
    exposed_days = {d["date"]: d for d in pl.get("days") or []}
    if set(exposed_days) != set(expected_days):
        result.fail(f"days {sorted(exposed_days)} ≠ expected {sorted(expected_days)}")
    for key, (n, cheap, expensive, _) in expected_days.items():
        d = exposed_days.get(key)
        if d is None:
            continue
        if d["slot_count"] != n:
            result.fail(f"{key}: slot_count {d['slot_count']} ≠ {n}")
        if _differs(d["cheap_threshold"], cheap) or _differs(d["expensive_threshold"], expensive):
            result.fail(
                f"{key}: thresholds {d['cheap_threshold']}/{d['expensive_threshold']} ≠ {cheap}/{expensive}"
            )

    # 3. Every slot's level and rank; 4. counts vs the per-slot tally.
    tally = {"cheap": 0, "normal": 0, "expensive": 0}
    wrong = 0
    for s in slots:
        tally[s["level"]] = tally.get(s["level"], 0) + 1
        exp = expected_days.get(s["day"])
        if exp is None:
            result.fail(f"{s['start']}: day {s['day']} not among the recomputed days")
            continue
        n, cheap, expensive, prices = exp
        level = _expected_level(s["buy"], cheap, expensive, cfg)
        rank = bisect_left(prices, s["buy"]) / (n - 1) if n > 1 else 0.5
        if s["level"] != level or abs(s["rank"] - rank) > _RANK_TOL:
            wrong += 1
            if wrong <= _MAX_LISTED:
                result.fail(f"{s['start']}: level/rank {s['level']}/{s['rank']} ≠ {level}/{rank:.4f}")
    if wrong > _MAX_LISTED:
        result.fail(f"… {wrong - _MAX_LISTED} more slot mismatches")
    if tally != {k: result.counts.get(k, 0) for k in tally}:
        result.fail(f"counts {result.counts} ≠ per-slot tally {tally}")

    # 5. Exposes: ``current`` (what the entities publish) is the slot covering now.
    now = datetime.now(UTC)
    covering = next(
        (s for s, p in zip(slots, price_slots, strict=True)
         if datetime.fromisoformat(p["start"]) <= now < datetime.fromisoformat(p["start"])
         + _resolution(pricing)),
        None,
    )
    if covering is not None and result.current_level not in (None, covering["level"]):
        result.fail(f"current level {result.current_level} ≠ slot-at-now level {covering['level']}")

    return result


def _resolution(pricing: dict):
    """Return the pricing slot length as a timedelta (default 15 min)."""
    from datetime import timedelta
    return timedelta(seconds=pricing.get("resolution_s") or 900)


class PriceLevelCheckWidget(Static):
    """Collapsible price-level deep check: counts, config and mismatches."""

    DEFAULT_CSS = "PriceLevelCheckWidget { height: auto; }"

    def __init__(self, pr: PriceLevelCheckResult) -> None:
        """Initialise with the pre-computed price-level check result.

        Args:
            pr: Result of check_price_level() for one coordinator.
        """
        super().__init__()
        self._pr = pr

    def compose(self) -> ComposeResult:
        """Render the counts and any mismatches as a collapsible block."""
        pr = self._pr
        if pr.skipped:
            yield Static(f"  ⚠  price_level_check   SKIP   {pr.skip_reason}")
            return

        color = "green" if pr.overall_ok else "red"
        mark = "✓" if pr.overall_ok else "✗"
        status = "PASS" if pr.overall_ok else "FAIL"
        counts = pr.counts
        title = (
            f"[{color}]{mark}[/{color}]  price_level_check   [{color}]{status}[/{color}]"
            f"   now={pr.current_level or '—'}  cheap={counts.get('cheap', 0)}"
            f"  normal={counts.get('normal', 0)}  expensive={counts.get('expensive', 0)}"
            f"  slots={pr.slot_count}  days={pr.day_count}"
        )
        with Collapsible(title=title, collapsed=pr.overall_ok):
            cfg = pr.config
            yield Static(
                f"  Shares: cheap {cfg.get('cheap_share', 0):.0%} / expensive {cfg.get('expensive_share', 0):.0%}\n"
                f"  Limits: always cheap ≤ {cfg.get('cheap_below')}  always expensive ≥ {cfg.get('expensive_above')}",
            )
            for msg in pr.mismatches:
                yield Static(f"  [red]✗ {msg}[/red]", markup=True)
