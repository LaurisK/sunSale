"""Lightweight built-in validators registered into the global registry."""
from __future__ import annotations

from datetime import datetime

from .registry import validator
from .snapshot import Snapshot


@validator("config_entities_resolve", "config")
def _config_entities_resolve(snap: Snapshot) -> tuple[bool, str]:
    """Check that every configured HA entity exists on the live system."""
    missing = [eid for eid, state in snap.raw_entities.items() if state is None]
    if missing:
        return False, f"unresolved: {missing}"
    return True, f"{len(snap.raw_entities)} entities resolved"


@validator("pricing_spot_matches_nordpool", "pricing")
def _pricing_spot_matches_nordpool(snap: Snapshot) -> tuple[bool, str]:
    """Every pricing slot whose start aligns with a Nordpool source entry
    must carry the same spot price."""
    pricing = snap.pipeline.get("pricing")
    if pricing is None:
        return False, "pipeline.pricing is null"
    nordpool_eid = snap.config.get("nordpool_entity")
    nordpool = snap.raw_entities.get(nordpool_eid) if nordpool_eid else None
    if nordpool is None:
        return False, "nordpool entity not fetched"
    attrs = nordpool.get("attributes", {}) or {}
    source_entries = (attrs.get("raw_today") or []) + (attrs.get("raw_tomorrow") or [])
    source_by_start: dict[str, float] = {}
    for entry in source_entries:
        try:
            t = datetime.fromisoformat(entry["start"])
        except (KeyError, ValueError):
            continue
        source_by_start[t.astimezone().isoformat()] = entry.get("value")
    overlap = 0
    mismatches: list[str] = []
    for s in pricing.get("slots") or []:
        try:
            slot_t = datetime.fromisoformat(s["start"]).astimezone().isoformat()
        except ValueError:
            continue
        if slot_t in source_by_start:
            overlap += 1
            src = source_by_start[slot_t]
            if src is not None and abs(s["spot"] - src) > 1e-4:
                mismatches.append(f"{s['start']}: spot {s['spot']} vs source {src}")
                if len(mismatches) > 3:
                    break
    if overlap == 0:
        return False, "no pricing slot starts overlap with Nordpool source"
    if mismatches:
        return False, "; ".join(mismatches)
    return True, f"{overlap}/{len(pricing.get('slots') or [])} pricing slots match Nordpool source spot"


@validator("pricing_covers_source", "pricing")
def _pricing_covers_source(snap: Snapshot) -> tuple[bool, str]:
    """All Nordpool source entries should appear in the pricing slot set."""
    pricing = snap.pipeline.get("pricing") or {}
    nordpool_eid = snap.config.get("nordpool_entity")
    nordpool = snap.raw_entities.get(nordpool_eid) if nordpool_eid else None
    if nordpool is None:
        return False, "nordpool entity not fetched"
    attrs = nordpool.get("attributes", {}) or {}
    source_entries = (attrs.get("raw_today") or []) + (attrs.get("raw_tomorrow") or [])
    if not source_entries:
        return True, "no source entries"
    slot_starts: set[str] = set()
    for s in pricing.get("slots") or []:
        try:
            slot_starts.add(datetime.fromisoformat(s["start"]).astimezone().isoformat())
        except ValueError:
            pass
    missing = []
    for entry in source_entries:
        try:
            t = datetime.fromisoformat(entry["start"]).astimezone().isoformat()
        except (KeyError, ValueError):
            continue
        if t not in slot_starts:
            missing.append(entry["start"])
    if missing:
        return False, f"{len(missing)} source entries missing from pricing (e.g. {missing[:2]})"
    return True, f"all {len(source_entries)} source entries present in pricing"


@validator("tariff_math_consistent", "pricing")
def _tariff_math(snap: Snapshot) -> tuple[bool, str]:
    """Check that each slot's buy/sell prices match the configured tariff formula."""
    pricing = snap.pipeline.get("pricing") or {}
    tariff = snap.inputs.get("tariff_config")
    slots = pricing.get("slots") or []
    if not tariff or not slots:
        return False, "missing tariff_config or pricing slots"
    dist = tariff["distribution_fee"]
    markup = tariff["markup"]
    tax = tariff["tax_rate"]
    s_dist = tariff["sell_distribution_fee"]
    s_markup = tariff["sell_markup"]
    s_tax = tariff["sell_tax_rate"]
    tol = 1e-3
    mismatches: list[str] = []
    for s in slots[:96]:
        spot = s["spot"]
        expected_buy = (spot + dist + markup) * (1 + tax)
        expected_sell = (spot - s_dist - s_markup) * (1 - s_tax)
        if abs(s["buy"] - expected_buy) > tol:
            mismatches.append(f"{s['start']}: buy {s['buy']} vs expected {expected_buy:.4f}")
        if abs(s["sell"] - expected_sell) > tol:
            mismatches.append(f"{s['start']}: sell {s['sell']} vs expected {expected_sell:.4f}")
        if len(mismatches) > 3:
            break
    if mismatches:
        return False, "; ".join(mismatches)
    return True, f"{len(slots)} slots match formula"


@validator("pricing_slots_contiguous", "pricing")
def _pricing_contiguous(snap: Snapshot) -> tuple[bool, str]:
    """Check that pricing slot starts are spaced exactly one resolution apart."""
    pricing = snap.pipeline.get("pricing") or {}
    slots = pricing.get("slots") or []
    res_s = pricing.get("resolution_s", 0)
    if len(slots) < 2 or res_s <= 0:
        return True, "n/a"
    prev = datetime.fromisoformat(slots[0]["start"])
    for s in slots[1:]:
        cur = datetime.fromisoformat(s["start"])
        delta = (cur - prev).total_seconds()
        if abs(delta - res_s) > 1:
            return False, f"gap at {s['start']}: {delta:.0f}s (expected {res_s}s)"
        prev = cur
    return True, f"{len(slots)} slots contiguous at {res_s}s"


@validator("calculation_within_pricing_window", "calculation")
def _calc_within_pricing(snap: Snapshot) -> tuple[bool, str]:
    """Check that no calculation slot falls outside the pricing slot range."""
    pricing = snap.pipeline.get("pricing") or {}
    calc = snap.pipeline.get("calculation") or {}
    p_slots = pricing.get("slots") or []
    c_slots = calc.get("slots") or []
    if not p_slots or not c_slots:
        return True, "n/a"
    p_start = datetime.fromisoformat(p_slots[0]["start"])
    p_end = datetime.fromisoformat(p_slots[-1]["start"])
    out_of_window = []
    for s in c_slots:
        t = datetime.fromisoformat(s["start"])
        if t < p_start or t > p_end:
            out_of_window.append(s["start"])
    if out_of_window:
        return False, f"{len(out_of_window)} calculation slots outside pricing window"
    return True, f"{len(c_slots)} calculation slots within pricing window"


@validator("schedule_chronological", "schedule")
def _schedule_chronological(snap: Snapshot) -> tuple[bool, str]:
    """Check that schedule slots are emitted in non-decreasing start-time order."""
    schedule = snap.outputs.get("schedule")
    if schedule is None:
        return True, "no schedule"
    slots = schedule.get("slots") or []
    if len(slots) < 2:
        return True, f"{len(slots)} slot(s)"
    last = datetime.fromisoformat(slots[0]["start"])
    for s in slots[1:]:
        cur = datetime.fromisoformat(s["start"])
        if cur < last:
            return False, f"slot at {s['start']} precedes prior {last.isoformat()}"
        last = cur
    return True, f"{len(slots)} slots in order"


@validator("schedule_aligned_with_pricing", "schedule")
def _schedule_aligned(snap: Snapshot) -> tuple[bool, str]:
    """Check that every schedule slot starts on a pricing-slot boundary."""
    schedule = snap.outputs.get("schedule")
    pricing = snap.pipeline.get("pricing") or {}
    if schedule is None:
        return True, "no schedule"
    p_starts = {s["start"] for s in pricing.get("slots") or []}
    if not p_starts:
        return True, "no pricing slots to compare"
    misaligned = [s["start"] for s in schedule.get("slots", []) if s["start"] not in p_starts]
    if misaligned:
        return False, f"{len(misaligned)} schedule slot(s) not aligned to a pricing slot start"
    return True, "all schedule slots aligned"


_VALID_STORAGE_MODES = {
    "feed_in", "self_use", "no_export", "discharge", "grid_charge", "stand_by",
    "auto", "track", "unknown",
}


@validator("inverter_mode_plan_uses_known_modes", "inverter_mode")
def _inverter_mode_plan_modes_valid(snap: Snapshot) -> tuple[bool, str]:
    """Every plan slot's ``mode`` must be a known StorageMode value."""
    block = snap.outputs.get("inverter_mode")
    if not block:
        return True, "no inverter_mode block"
    plan = block.get("plan") or []
    if not plan:
        return True, "no plan slots"
    unknown = [s["mode"] for s in plan if s.get("mode") not in _VALID_STORAGE_MODES]
    if unknown:
        return False, f"{len(unknown)} plan slot(s) with unknown mode: {unknown[:3]}"
    return True, f"{len(plan)} plan slots, all modes recognised"


@validator("inverter_mode_history_strictly_changes", "inverter_mode")
def _inverter_mode_history_strict(snap: Snapshot) -> tuple[bool, str]:
    """History entries must be strictly mode-change events (no consecutive duplicates)."""
    block = snap.outputs.get("inverter_mode")
    if not block:
        return True, "no inverter_mode block"
    history = block.get("history") or []
    last_mode: str | None = None
    duplicates = 0
    for entry in history:
        mode = entry.get("mode")
        if mode == last_mode:
            duplicates += 1
        last_mode = mode
    if duplicates:
        return False, f"{duplicates} consecutive duplicate mode entries in history"
    return True, f"{len(history)} mode-change events, no duplicates"


@validator("inverter_mode_history_chronological", "inverter_mode")
def _inverter_mode_history_chronological(snap: Snapshot) -> tuple[bool, str]:
    """History entries must be sorted ascending by timestamp."""
    block = snap.outputs.get("inverter_mode")
    if not block:
        return True, "no inverter_mode block"
    history = block.get("history") or []
    if len(history) < 2:
        return True, f"{len(history)} entries"
    last_t = datetime.fromisoformat(history[0]["t"])
    for entry in history[1:]:
        cur_t = datetime.fromisoformat(entry["t"])
        if cur_t < last_t:
            return False, f"entry at {entry['t']} precedes prior {last_t.isoformat()}"
        last_t = cur_t
    return True, f"{len(history)} entries chronologically ordered"


@validator("inverter_mode_observed_matches_history_tail", "inverter_mode")
def _inverter_mode_observed_matches_tail(snap: Snapshot) -> tuple[bool, str]:
    """The current observed mode should match the last history entry's mode."""
    block = snap.outputs.get("inverter_mode")
    if not block:
        return True, "no inverter_mode block"
    reading = block.get("reading") or {}
    history = block.get("history") or []
    observed = reading.get("mode")
    if observed is None or not history:
        return True, "no observed reading or empty history"
    tail = history[-1].get("mode")
    if observed != tail:
        return False, f"observed={observed} but history tail={tail}"
    return True, f"observed mode {observed} matches history tail"


@validator("battery_soc_sane", "battery")
def _battery_soc(snap: Snapshot) -> tuple[bool, str]:
    """Check that the observed battery SoC is a valid 0..1 fraction."""
    battery = snap.inputs.get("battery")
    if battery is None:
        return True, "no battery state"
    soc = battery.get("soc")
    if soc is None:
        return False, "battery.soc is null"
    if not (0.0 <= soc <= 1.0):
        return False, f"soc={soc:.4f} outside [0.0, 1.0]"
    return True, f"soc={soc:.1%}"


@validator("degradation_cost_present", "battery")
def _degradation_cost(snap: Snapshot) -> tuple[bool, str]:
    """Check that a positive degradation cost is reported once capacity is known."""
    deg = snap.pipeline.get("degradation_cost_per_kwh")
    battery = snap.inputs.get("battery") or {}
    if battery.get("estimated_capacity_kwh"):
        if deg is None or deg <= 0:
            return False, f"degradation_cost_per_kwh={deg} but estimated_capacity exists"
        return True, f"{deg:.5f} EUR/kWh"
    return True, "no capacity yet"
