"""Pricing deep check and its TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


def _resolve_tz(name: str) -> tzinfo:
    """Return the tzinfo for an IANA name, falling back to UTC.

    Args:
        name: IANA timezone name from the debug payload (e.g. "Europe/Riga").

    Returns:
        The resolved tzinfo, or UTC when the name is empty/unknown.
    """
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:  # noqa: BLE001 - unknown tz name → safe UTC fallback
        return timezone.utc


def _select_band_fees(tariff: dict, local_dt: datetime) -> tuple[float, float]:
    """Mirror the coordinator's TOU band selection for one local slot start.

    Picks the weekday/weekend schedule by day-of-week, then the band whose
    start_minute is the latest at or before the slot's minute-of-day (wrapping
    to the day's last band before the first start). Falls back to the flat
    distribution fees when the applicable schedule is empty.

    Args:
        tariff: Serialised tariff_config dict (with weekday/weekend bands).
        local_dt: Slot start projected into the installation's local timezone.

    Returns:
        (buy_distribution_fee, sell_distribution_fee) for the slot.
    """
    flat_buy = tariff.get("distribution_fee", 0.0)
    flat_sell = tariff.get("sell_distribution_fee", 0.0)
    key = "weekend_bands" if local_dt.weekday() >= 5 else "weekday_bands"
    bands = tariff.get(key) or []
    if not bands:
        return flat_buy, flat_sell
    minute = local_dt.hour * 60 + local_dt.minute
    ordered = sorted(bands, key=lambda b: b.get("start_minute", 0))
    chosen = ordered[-1]
    for band in ordered:
        if band.get("start_minute", 0) <= minute:
            chosen = band
        else:
            break
    return chosen.get("distribution_fee", flat_buy), chosen.get("sell_distribution_fee", flat_sell)


@dataclass
class PricingCheckResult:
    """Result of the pricing deep-check: tariff formula vs module output."""

    skipped: bool = False
    skip_reason: str = ""
    module_slot_count: int = 0
    resolution_s: int = 0
    computed_at: str = ""
    negative_sell_count: int = 0
    tariff_config: dict = field(default_factory=dict)
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_pricing(snap: Snapshot) -> PricingCheckResult:
    """Verify every pricing slot against the tariff formula.

    Args:
        snap: Coordinator snapshot containing pipeline.pricing and inputs.tariff_config.

    Returns:
        PricingCheckResult with per-slot formula comparisons and overall pass/fail.
    """
    result = PricingCheckResult()

    pricing = snap.pipeline.get("pricing")
    if not pricing:
        result.skipped = True
        result.skip_reason = "pipeline.pricing is null"
        return result

    tariff = snap.inputs.get("tariff_config")
    if not tariff:
        result.skipped = True
        result.skip_reason = "inputs.tariff_config is null"
        return result

    result.module_slot_count = pricing.get("slot_count", 0)
    result.resolution_s = pricing.get("resolution_s", 0)
    result.computed_at = pricing.get("computed_at", "")
    result.negative_sell_count = pricing.get("negative_sell_count", 0)
    result.tariff_config = dict(tariff)

    markup = tariff.get("markup", 0.0)
    tax = tariff.get("tax_rate", 0.0)
    s_markup = tariff.get("sell_markup", 0.0)
    s_tax = tariff.get("sell_tax_rate", 0.0)
    sell_mode = tariff.get("sell_mode", "spot")
    fixed_sell = tariff.get("fixed_sell_price", 0.0)
    local_tz = _resolve_tz(snap.config.get("time_zone", ""))
    TOL = 1e-3

    for s in pricing.get("slots") or []:
        spot = s.get("spot", 0.0)
        act_buy = s.get("buy", 0.0)
        act_sell = s.get("sell", 0.0)
        # Resolve the per-slot TOU distribution fees the same way the coordinator
        # does, projecting the UTC slot start into local time first.
        try:
            local_start = datetime.fromisoformat(s.get("start", "")).astimezone(local_tz)
            dist, s_dist = _select_band_fees(tariff, local_start)
            local_ok = True
        except (ValueError, AttributeError):
            dist = tariff.get("distribution_fee", 0.0)
            s_dist = tariff.get("sell_distribution_fee", 0.0)
            local_ok = False
        exp_buy = (spot + dist + markup) * (1 + tax)
        # Mirror pipeline/tariff.py:sell_price per sell_mode. In "schedule" mode
        # the band's sell distribution fee is the absolute price; in "feed" mode
        # the slot's separate export price is used (fallback to fixed).
        if sell_mode == "fixed":
            exp_sell = fixed_sell
        elif sell_mode == "schedule":
            exp_sell = s_dist if local_ok else fixed_sell
        elif sell_mode == "feed":
            export = s.get("export")
            exp_sell = export if export is not None else fixed_sell
        else:
            exp_sell = (spot - s_dist - s_markup) * (1 - s_tax)
        ok = abs(act_buy - exp_buy) <= TOL and abs(act_sell - exp_sell) <= TOL
        if not ok:
            result.mismatches.append(s.get("start", ""))
            result.overall_ok = False
        result.slot_rows.append({
            "start": s.get("start", ""),
            "spot": spot,
            "exp_buy": exp_buy,
            "act_buy": act_buy,
            "exp_sell": exp_sell,
            "act_sell": act_sell,
            "ok": ok,
        })

    return result


class PricingSlotsTable(Static):
    """DataTable: Time | Spot | Exp Buy | Act Buy | Exp Sell | Act Sell | ✓/✗."""

    DEFAULT_CSS = """
    PricingSlotsTable { height: auto; }
    PricingSlotsTable DataTable { height: 22; }
    """

    def __init__(self, pc: PricingCheckResult) -> None:
        """Initialise with the pricing check result.

        Args:
            pc: Result of check_pricing() containing per-slot formula comparisons.
        """
        super().__init__()
        self._pc = pc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per pricing slot."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Spot", "Exp Buy", "Act Buy", "Exp Sell", "Act Sell", "")
        dim = "dim"

        for row in self._pc.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                time_str = dt.strftime("%m-%d %H:%M")
            except (ValueError, AttributeError):
                time_str = row["start"]

            ok = row["ok"]
            bad_style = "red" if not ok else ""
            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{row['spot']:.4f}"),
                Text(f"{row['exp_buy']:.4f}", style=dim),
                Text(f"{row['act_buy']:.4f}", style=bad_style),
                Text(f"{row['exp_sell']:.4f}", style=dim),
                Text(f"{row['act_sell']:.4f}", style=bad_style),
                Text("✓" if ok else "✗", style="green" if ok else "red"),
            )


class PricingCheckWidget(Static):
    """Collapsible pricing deep-check: formula verification per slot + tariff config."""

    DEFAULT_CSS = "PricingCheckWidget { height: auto; }"

    def __init__(self, pc: PricingCheckResult) -> None:
        """Initialise with the pre-computed pricing check result.

        Args:
            pc: Result of check_pricing() for one coordinator.
        """
        super().__init__()
        self._pc = pc

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and optional Tariff Config sub-Collapsible."""
        pc = self._pc

        if pc.skipped:
            yield Static(f"  ⚠  pricing_check   SKIP   {pc.skip_reason}")
            return

        color = "green" if pc.overall_ok else "red"
        mark = "✓" if pc.overall_ok else "✗"
        status = "PASS" if pc.overall_ok else "FAIL"
        neg = f"  {pc.negative_sell_count} negative-sell" if pc.negative_sell_count else ""
        res_min = pc.resolution_s // 60
        title = (
            f"[{color}]{mark}[/{color}]  pricing_check   [{color}]{status}[/{color}]"
            f"   {pc.module_slot_count} slots  res={res_min}min{neg}"
        )

        with Collapsible(title=title, collapsed=True):
            with Collapsible(title="Slots", collapsed=False):
                yield PricingSlotsTable(pc)
            with Collapsible(title="Tariff Config", collapsed=True):
                lines = "\n".join(f"  {k}: {v}" for k, v in pc.tariff_config.items())
                yield Static(lines)
