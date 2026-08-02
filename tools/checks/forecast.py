"""Solar-forecast deep checks (forecast, accuracy, quality) and their widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot, _day_eid, _remaining_eid, _tomorrow_eid


@dataclass
class EntitySlots:
    """Per-entity slot data extracted from a single Open Meteo / forecast.solar feed."""

    entity_id: str
    day_label: str
    resolution_min: int
    slots: list[tuple[datetime, float]]
    total_kwh: float


@dataclass
class ForecastCheckResult:
    """Aggregated cross-checks for the forecast pipeline stage (TUI deep mode)."""

    skipped: bool = False
    skip_reason: str = ""
    n_arrays: int = 0
    array_eids: list[str] = field(default_factory=list)
    entity_slots: list[EntitySlots] = field(default_factory=list)
    expected_totals: dict[str, float] = field(default_factory=dict)
    module_totals: dict[str, float] = field(default_factory=dict)
    module_slot_count: int = 0
    module_yesterday_kwh: float = 0.0
    yesterday_store_date: str = ""
    yesterday_store_slots: list[tuple[datetime, float]] = field(default_factory=list)  # (utc_dt, kwh)
    yesterday_store_total_kwh: float = 0.0
    module_today_remaining_kwh: float | None = None
    entity_remaining: dict[str, float] = field(default_factory=dict)
    module_slots: list[dict] = field(default_factory=list)
    negative_slots: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def _parse_watts(watts: dict) -> tuple[list[tuple[datetime, float]], int]:
    """Parse {iso_str: watts} dict into sorted (datetime, watts) list + resolution_min."""
    parsed: list[tuple[datetime, float]] = []
    for ts_str, w in watts.items():
        try:
            dt = datetime.fromisoformat(str(ts_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            parsed.append((dt.astimezone(UTC), float(w)))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda x: x[0])
    if len(parsed) >= 2:
        delta = (parsed[1][0] - parsed[0][0]).total_seconds()
        res = 15 if abs(delta - 900) < 60 else 60
    else:
        res = 60
    return parsed, res


def check_forecast(snap: Snapshot) -> ForecastCheckResult:
    """Deep forecast validation: raw entity data → expected totals → vs module output."""
    result = ForecastCheckResult()

    # Prefer the runtime-resolved base entities (device-based selection resolves
    # config-entry IDs to base sensors in the coordinator); fall back to the
    # legacy entity keys for older debug payloads.
    base_eids = list(snap.config.get("solar_forecast_base_entities") or [])
    if not base_eids:
        base_eids = [
            eid
            for eid in (
                snap.config.get("solar_forecast_entity", ""),
                snap.config.get("solar_forecast_entity_2", ""),
            )
            if eid
        ]

    if not base_eids:
        result.skipped = True
        result.skip_reason = "No solar forecast entities configured"
        return result

    arrays_with_data = [
        eid for eid in base_eids
        if isinstance(((snap.raw_entities.get(eid) or {}).get("attributes") or {}).get("watts"), dict)
    ]
    if not arrays_with_data:
        result.skipped = True
        result.skip_reason = "Open-Meteo Solar Forecast not present (no 'watts' attribute on entity)"
        return result

    result.n_arrays = len(arrays_with_data)
    result.array_eids = list(arrays_with_data)

    now = datetime.now(UTC)
    today = now.date()

    day_plan: list[tuple[str, date]] = [
        ("today", today),
        ("tomorrow", today + timedelta(days=1)),
    ] + [(f"d{n}", today + timedelta(days=n)) for n in range(2, 7)]

    combined_by_day: dict[str, float] = {label: 0.0 for label, _ in day_plan}

    for base_eid in arrays_with_data:
        eid_day_pairs: list[tuple[str, str]] = [
            (base_eid, "today"),
            (_tomorrow_eid(base_eid), "tomorrow"),
        ] + [(_day_eid(base_eid, n), f"d{n}") for n in range(2, 7)]

        for eid, day_label in eid_day_pairs:
            if not eid:
                continue
            state = snap.raw_entities.get(eid)
            if not state:
                continue
            watts = (state.get("attributes") or {}).get("watts")
            if not isinstance(watts, dict):
                continue

            slots, res = _parse_watts(watts)
            slot_h = res / 60.0
            total = round(sum(w * slot_h / 1000.0 for _, w in slots), 4)
            combined_by_day[day_label] = round(combined_by_day[day_label] + total, 4)

            result.entity_slots.append(EntitySlots(
                entity_id=eid,
                day_label=day_label,
                resolution_min=res,
                slots=slots,
                total_kwh=total,
            ))

    result.expected_totals = dict(combined_by_day)

    forecast = snap.pipeline.get("forecast") or {}
    result.module_slot_count = forecast.get("slot_count", 0)
    result.module_yesterday_kwh = forecast.get("total_yesterday_kwh", 0.0)
    result.module_today_remaining_kwh = forecast.get("today_remaining_kwh")

    yday_store = snap.inputs.get("yesterday_solar") or {}
    result.yesterday_store_date = yday_store.get("date") or ""
    yday_total = 0.0
    for entry in yday_store.get("entries") or []:
        try:
            dt = datetime.fromisoformat(entry["start"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            kwh = float(entry["kwh"])
            result.yesterday_store_slots.append((dt.astimezone(UTC), kwh))
            yday_total += kwh
        except (KeyError, ValueError, TypeError):
            continue
    result.yesterday_store_total_kwh = round(yday_total, 4)
    result.module_slots = forecast.get("slots") or []
    result.module_totals = {
        "today":    forecast.get("total_today_kwh", 0.0),
        "tomorrow": forecast.get("total_tomorrow_kwh", 0.0),
        "d2":       forecast.get("total_d2_kwh", 0.0),
        "d3":       forecast.get("total_d3_kwh", 0.0),
        "d4":       forecast.get("total_d4_kwh", 0.0),
        "d5":       forecast.get("total_d5_kwh", 0.0),
        "d6":       forecast.get("total_d6_kwh", 0.0),
    }

    for base_eid in arrays_with_data:
        r_eid = _remaining_eid(base_eid)
        if not r_eid:
            continue
        state = snap.raw_entities.get(r_eid)
        if state is None:
            continue
        try:
            result.entity_remaining[r_eid] = float(state["state"])
        except (KeyError, ValueError, TypeError):
            pass

    for s in result.module_slots:
        if s.get("expected_kwh", 0.0) < -1e-6:
            result.negative_slots.append(s["start"])
            result.overall_ok = False

    TOL = 0.001
    if result.entity_remaining and result.module_today_remaining_kwh is not None:
        expected_rem = sum(result.entity_remaining.values())
        if abs(expected_rem - result.module_today_remaining_kwh) > TOL:
            result.mismatches.append("remaining")
            result.overall_ok = False

    for day_label, _ in day_plan:
        exp = combined_by_day.get(day_label, 0.0)
        act = result.module_totals.get(day_label, 0.0)
        if abs(exp - act) > TOL:
            result.mismatches.append(day_label)
            result.overall_ok = False

    return result


class ForecastSlotsTable(Static):
    """DataTable widget: array_1 | array_2 | total | target | module, today + tomorrow."""

    DEFAULT_CSS = """
    ForecastSlotsTable { height: auto; }
    ForecastSlotsTable DataTable { height: 25; }
    """

    def __init__(
        self,
        entity_slots: list[EntitySlots],
        array_eids: list[str],
        module_slots: list[dict],
        yesterday_store_slots: list[tuple[datetime, float]] | None = None,
    ) -> None:
        """Initialise with forecast check data.

        Args:
            entity_slots: All parsed raw entity slots from check_forecast.
            array_eids: Base (today) entity IDs for up to two solar arrays.
            module_slots: Module GenerationSlot dicts from the pipeline debug endpoint.
            yesterday_store_slots: Parsed coordinator-store entries for yesterday (utc_dt, kwh).
        """
        super().__init__()
        self._entity_slots = entity_slots
        self._array_eids = array_eids
        self._module_slots = module_slots
        self._yesterday_store: dict[datetime, float] = {
            dt: kwh for dt, kwh in (yesterday_store_slots or [])
        }

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with slot rows for yesterday, today, and tomorrow."""
        now_utc = datetime.now(UTC)
        today_d = now_utc.date()
        yesterday_d = today_d - timedelta(days=1)
        tomorrow_d = today_d + timedelta(days=1)
        in_range = frozenset((yesterday_d, today_d, tomorrow_d))

        array_data: list[dict[datetime, float]] = []
        for base_eid in (list(self._array_eids) + ["", ""])[:2]:
            tomorrow_eid = _tomorrow_eid(base_eid) if base_eid else ""
            dmap: dict[datetime, float] = {}
            for es in self._entity_slots:
                # Yesterday has no entity source; only today + tomorrow come from raw entities.
                if es.entity_id in (base_eid, tomorrow_eid) and es.day_label in ("today", "tomorrow"):
                    for dt, w in es.slots:
                        if dt.date() in in_range:
                            dmap[dt] = w
            array_data.append(dmap)

        all_ent_times: set[datetime] = set()
        for dmap in array_data:
            all_ent_times.update(dmap.keys())
        sorted_ent = sorted(all_ent_times)
        unit = "W"
        if len(sorted_ent) >= 2:
            delta_s = (sorted_ent[1] - sorted_ent[0]).total_seconds()
            unit = "W" if abs(delta_s - 900) < 60 else "kWh"

        module_data: dict[datetime, float] = {}
        for s in self._module_slots:
            try:
                dt = datetime.fromisoformat(s["start"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                dt_utc = dt.astimezone(UTC)
                if dt_utc.date() in in_range:
                    module_data[dt_utc] = s.get("expected_kwh", 0.0)
            except (KeyError, ValueError):
                continue

        all_times = sorted(
            t for t in (all_ent_times | module_data.keys())
            if t.date() in in_range
        )

        def _short(eid: str) -> str:
            """Strip the ``sensor.`` prefix from an entity ID for compact column headers."""
            return eid.removeprefix("sensor.") if eid else "—"

        col1 = _short(self._array_eids[0]) if self._array_eids else "array_1"
        col2 = _short(self._array_eids[1]) if len(self._array_eids) > 1 else "array_2"

        table = self.query_one(DataTable)
        table.add_columns(
            "Time",
            f"{col1} ({unit})",
            f"{col2} ({unit})",
            f"total ({unit})",
            "target (/4)",
            "module (kWh)",
        )

        dim = "dim"
        prev_date: date | None = None
        for dt in all_times:
            v1 = array_data[0].get(dt, 0.0)
            v2 = array_data[1].get(dt, 0.0)
            v_mod = module_data.get(dt)
            total = v1 + v2
            target = total / 4

            if dt.date() != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 6)
                if dt.date() == yesterday_d:
                    label = "yesterday (module only — no entity source)"
                elif dt.date() == today_d:
                    label = "today"
                else:
                    label = "tomorrow"
                table.add_row(Text(label, style="bold"), *[""] * 5)
                prev_date = dt.date()

            is_yday = dt.date() == yesterday_d
            if is_yday:
                store_kwh = self._yesterday_store.get(dt)
                table.add_row(
                    Text(dt.strftime("%H:%M"), style="cyan"),
                    Text("—", style=dim),
                    Text("—", style=dim),
                    Text(f"{store_kwh:.4f}kWh", style=dim if store_kwh == 0 else "")
                    if store_kwh is not None else Text("—", style=dim),
                    Text("—", style=dim),
                    Text(f"{v_mod:.4f}", style=dim if v_mod == 0 else "") if v_mod is not None
                    else Text("—", style=dim),
                )
            else:
                table.add_row(
                    Text(dt.strftime("%H:%M"), style="cyan"),
                    Text(f"{v1:.0f}", style=dim if v1 == 0 else ""),
                    Text(f"{v2:.0f}", style=dim if v2 == 0 else ""),
                    Text(f"{total:.0f}", style=dim if total == 0 else ""),
                    Text(f"{target:.2f}", style=dim if target == 0 else ""),
                    Text(f"{v_mod:.3f}", style=dim if v_mod == 0 else "") if v_mod is not None
                    else Text("—", style=dim),
                )


class ForecastSummaryTable(Static):
    """DataTable of per-day totals: array_1 | array_2 | total | module | check."""

    DEFAULT_CSS = """
    ForecastSummaryTable { height: auto; }
    ForecastSummaryTable DataTable { height: 12; }
    """

    def __init__(self, fc: ForecastCheckResult) -> None:
        """Initialise with the forecast check result.

        Args:
            fc: Result of check_forecast() containing entity slots and module totals.
        """
        super().__init__()
        self._fc = fc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per day (today → d6) plus remaining."""
        fc = self._fc

        slot_lookup: dict[tuple[str, str], float] = {
            (es.entity_id, es.day_label): es.total_kwh
            for es in fc.entity_slots
        }

        def _short(eid: str) -> str:
            """Strip the ``sensor.`` prefix from an entity ID for compact column headers."""
            return eid.removeprefix("sensor.") if eid else "—"

        col1 = _short(fc.array_eids[0]) if fc.array_eids else "array_1"
        col2 = _short(fc.array_eids[1]) if len(fc.array_eids) > 1 else "array_2"

        table = self.query_one(DataTable)
        table.add_columns(
            "day",
            f"{col1} (kWh)",
            f"{col2} (kWh)",
            "total (kWh)",
            "module (kWh)",
            "",
        )

        # Yesterday row first — store total vs module total; no per-array breakdown available.
        dim = "dim"
        store_known = bool(fc.yesterday_store_slots)
        yday_label = f"yesterday ({fc.yesterday_store_date})" if fc.yesterday_store_date else "yesterday"
        table.add_row(
            Text(yday_label, style="bold dim"),
            Text("—", style=dim),
            Text("—", style=dim),
            Text(f"{fc.yesterday_store_total_kwh:.4f}", style=dim if fc.yesterday_store_total_kwh == 0 else "")
            if store_known else Text("—", style=dim),
            Text(f"{fc.module_yesterday_kwh:.4f}", style=dim if fc.module_yesterday_kwh == 0 else ""),
            Text(""),   # display only — no pass/fail check for yesterday
        )

        day_plan: list[tuple[str, int | None]] = (
            [("today", None), ("tomorrow", None)]
            + [(f"d{n}", n) for n in range(2, 7)]
        )

        for day_label, n in day_plan:
            a_kwh: list[float] = []
            for base_eid in (list(fc.array_eids) + ["", ""])[:2]:
                if not base_eid:
                    a_kwh.append(0.0)
                    continue
                if day_label == "today":
                    eid = base_eid
                elif day_label == "tomorrow":
                    eid = _tomorrow_eid(base_eid)
                else:
                    eid = _day_eid(base_eid, n)  # type: ignore[arg-type]
                a_kwh.append(slot_lookup.get((eid, day_label), 0.0))

            total = a_kwh[0] + a_kwh[1]
            mod = fc.module_totals.get(day_label, 0.0)
            is_bad = day_label in fc.mismatches
            check_style = "red" if is_bad else "green"

            table.add_row(
                Text(day_label, style="bold"),
                Text(f"{a_kwh[0]:.4f}", style=dim if a_kwh[0] == 0 else ""),
                Text(f"{a_kwh[1]:.4f}", style=dim if a_kwh[1] == 0 else ""),
                Text(f"{total:.4f}", style=dim if total == 0 else ""),
                Text(f"{mod:.4f}", style=dim if mod == 0 else ""),
                Text("✗" if is_bad else "✓", style=check_style),
            )

            if day_label == "today":
                rem_mod = fc.module_today_remaining_kwh
                rem_arr: list[float | None] = []
                for base_eid in (list(fc.array_eids) + ["", ""])[:2]:
                    r_eid = _remaining_eid(base_eid) if base_eid else ""
                    rem_arr.append(fc.entity_remaining.get(r_eid) if r_eid else None)
                rem_total = (
                    sum(v for v in rem_arr if v is not None)
                    if any(v is not None for v in rem_arr) else None
                )
                is_rem_bad = "remaining" in fc.mismatches
                rem_check_style = "red" if is_rem_bad else "green"

                def _fmt(v: float | None) -> Text:
                    """Format a kWh value for the table cell, dim when None or zero."""
                    if v is None:
                        return Text("—", style=dim)
                    return Text(f"{v:.4f}", style=dim if v == 0 else "")

                table.add_row(
                    Text("  remaining", style=dim),
                    _fmt(rem_arr[0]),
                    _fmt(rem_arr[1]),
                    _fmt(rem_total),
                    _fmt(rem_mod),
                    Text("✗" if is_rem_bad else "✓", style=rem_check_style)
                    if rem_mod is not None else Text(""),
                )

        if fc.negative_slots:
            table.add_row(
                Text("negative slots", style="red bold"),
                Text(""), Text(""), Text(""),
                Text(str(len(fc.negative_slots)), style="red"),
                Text("✗", style="red"),
            )


class ForecastCheckWidget(Static):
    """Collapsible forecast deep-check with two sub-sections: Slots and Summary."""

    DEFAULT_CSS = "ForecastCheckWidget { height: auto; }"

    def __init__(self, fc: ForecastCheckResult) -> None:
        """Initialise with the pre-computed forecast check result.

        Args:
            fc: Result of check_forecast() for one coordinator.
        """
        super().__init__()
        self._fc = fc

    def compose(self) -> ComposeResult:
        """Render two sub-Collapsibles: Slots (time-level) and Summary (day-level)."""
        fc = self._fc

        if fc.skipped:
            yield Static(f"  ⚠  forecast_check   SKIP   {fc.skip_reason}")
            return

        color = "green" if fc.overall_ok else "red"
        mark = "✓" if fc.overall_ok else "✗"
        status = "PASS" if fc.overall_ok else "FAIL"
        title = f"[{color}]{mark}[/{color}]  forecast_check   [{color}]{status}[/{color}]   {fc.n_arrays} array(s)"

        with Collapsible(title=title, collapsed=True):
            yield Static(f"  Arrays: {', '.join(fc.array_eids)}")

            with Collapsible(title="Slots", collapsed=False):
                yield ForecastSlotsTable(fc.entity_slots, fc.array_eids, fc.module_slots, fc.yesterday_store_slots)
                if fc.negative_slots:
                    yield Static(f"  ⚠ {len(fc.negative_slots)} negative slot(s): {fc.negative_slots[:3]}")

            with Collapsible(title="Summary", collapsed=False):
                yield Static(f"  slot_count: {fc.module_slot_count}")
                yield ForecastSummaryTable(fc)


@dataclass
class ForecastAccuracyCheckResult:
    """Result of the forecast-accuracy deep-check: error arithmetic and aggregate metrics."""

    skipped: bool = False
    skip_reason: str = ""
    slot_count: int = 0
    total_forecast_kwh: float = 0.0
    total_observed_kwh: float = 0.0
    censored_slot_count: int = 0
    total_error_kwh: float = 0.0
    mean_absolute_error_kwh: float = 0.0
    bias_kwh: float = 0.0
    mape: float | None = None
    computed_at: str = ""
    slot_rows: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_forecast_accuracy(snap: Snapshot) -> ForecastAccuracyCheckResult:
    """Verify per-slot error arithmetic and total_error_kwh sum match slot data.

    Args:
        snap: Coordinator snapshot containing pipeline.forecast_error.

    Returns:
        ForecastAccuracyCheckResult with per-slot errors and overall pass/fail.
    """
    result = ForecastAccuracyCheckResult()

    fe = snap.pipeline.get("forecast_error")
    if not fe:
        result.skipped = True
        result.skip_reason = "pipeline.forecast_error is null (no inverter history for comparison)"
        return result

    result.slot_count = fe.get("slot_count", 0)
    result.total_forecast_kwh = fe.get("total_forecast_kwh", 0.0)
    result.total_observed_kwh = fe.get("total_observed_kwh", 0.0)
    result.censored_slot_count = fe.get("censored_slot_count", 0)
    result.total_error_kwh = fe.get("total_error_kwh", 0.0)
    result.mean_absolute_error_kwh = fe.get("mean_absolute_error_kwh", 0.0)
    result.bias_kwh = fe.get("bias_kwh", 0.0)
    result.mape = fe.get("mean_absolute_percentage_error")
    result.computed_at = fe.get("computed_at", "")

    # Aggregate totals exclude censored (curtailment-suspect) slots, so the
    # recomputed sum must skip them too; censored slots must under-generate
    # (a positive error can never be curtailment).
    comp_error = 0.0
    censored_count = 0
    matched = result.total_observed_kwh >= 0  # -1 sentinel = no matched slots
    for s in fe.get("slots") or []:
        start_str = s.get("start", "")
        f_kwh = s.get("forecast_kwh", 0.0)
        o_kwh = s.get("observed_kwh", 0.0)
        e_kwh = s.get("error_kwh", 0.0)
        censored = bool(s.get("censored", False))
        ok = abs(e_kwh - (o_kwh - f_kwh)) < 1e-4
        if not ok:
            result.mismatches.append(start_str)
            result.overall_ok = False
        if censored:
            censored_count += 1
            if o_kwh >= 0 and e_kwh >= 0:
                result.mismatches.append(f"{start_str}:censored_not_negative")
                result.overall_ok = False
        elif o_kwh >= 0:
            comp_error += e_kwh
        result.slot_rows.append({
            "start": start_str,
            "forecast_kwh": f_kwh,
            "observed_kwh": o_kwh,
            "error_kwh": e_kwh,
            "relative_error": s.get("relative_error"),
            "censored": censored,
            "ok": ok,
        })

    if censored_count != result.censored_slot_count:
        result.mismatches.append("censored_slot_count_mismatch")
        result.overall_ok = False

    if matched and abs(comp_error - result.total_error_kwh) > 0.01:
        result.mismatches.append("total_error_sum_mismatch")
        result.overall_ok = False

    return result


class ForecastAccuracySlotsTable(Static):
    """DataTable: Time | Forecast (kWh) | Observed (kWh) | Error (kWh) | Rel Error | ✓/✗."""

    DEFAULT_CSS = """
    ForecastAccuracySlotsTable { height: auto; }
    ForecastAccuracySlotsTable DataTable { height: 18; }
    """

    def __init__(self, fa: ForecastAccuracyCheckResult) -> None:
        """Initialise with the forecast accuracy check result.

        Args:
            fa: Result of check_forecast_accuracy() containing per-slot error data.
        """
        super().__init__()
        self._fa = fa

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per error slot."""
        table = self.query_one(DataTable)
        table.add_columns("Time", "Forecast (kWh)", "Observed (kWh)", "Error (kWh)", "Rel Err", "")
        dim = "dim"
        prev_date = None

        # Trailing-column markers: ✗ = arithmetic mismatch, ⊘ = censored
        # (curtailment-suspect no-export slot, excluded from stats/buckets).

        for row in self._fa.slot_rows:
            try:
                dt = datetime.fromisoformat(row["start"]).astimezone(UTC)
                time_str = dt.strftime("%H:%M")
                cur_date = dt.date()
            except (ValueError, AttributeError):
                time_str = row["start"]
                cur_date = None

            if cur_date is not None and cur_date != prev_date:
                if prev_date is not None:
                    table.add_row(*[Text("─", style=dim)] * 6)
                table.add_row(Text(str(cur_date), style="bold"), *[""] * 5)
                prev_date = cur_date

            err = row["error_kwh"]
            ok = row["ok"]
            censored = row.get("censored", False)
            err_style = "green" if err > 0.01 else ("red" if err < -0.01 else dim)
            rel = row["relative_error"]
            rel_str = f"{rel:.1%}" if rel is not None else "—"
            if not ok:
                mark = Text("✗", style="red")
            elif censored:
                mark = Text("⊘", style="yellow")
            else:
                mark = Text("")

            table.add_row(
                Text(time_str, style="cyan"),
                Text(f"{row['forecast_kwh']:.4f}", style=dim),
                Text(f"{row['observed_kwh']:.4f}", style=dim if censored else ""),
                Text(f"{err:+.4f}", style=err_style),
                Text(rel_str, style=dim),
                mark,
            )


class ForecastAccuracyCheckWidget(Static):
    """Collapsible forecast-accuracy deep-check: per-slot errors and aggregate metrics."""

    DEFAULT_CSS = "ForecastAccuracyCheckWidget { height: auto; }"

    def __init__(self, fa: ForecastAccuracyCheckResult) -> None:
        """Initialise with the pre-computed forecast accuracy check result.

        Args:
            fa: Result of check_forecast_accuracy() for one coordinator.
        """
        super().__init__()
        self._fa = fa

    def compose(self) -> ComposeResult:
        """Render Slots sub-Collapsible and metric summary."""
        fa = self._fa

        if fa.skipped:
            yield Static(f"  ⚠  forecast_accuracy_check   SKIP   {fa.skip_reason}")
            return

        color = "green" if fa.overall_ok else "red"
        mark = "✓" if fa.overall_ok else "✗"
        status = "PASS" if fa.overall_ok else "FAIL"
        mape_str = f"  MAPE={fa.mape:.1%}" if fa.mape is not None else ""
        title = (
            f"[{color}]{mark}[/{color}]  forecast_accuracy_check   [{color}]{status}[/{color}]"
            f"   {fa.slot_count} slots"
            f"   MAE={fa.mean_absolute_error_kwh:.4f}kWh  bias={fa.bias_kwh:+.4f}kWh{mape_str}"
        )

        with Collapsible(title=title, collapsed=True):
            censored_str = (
                f"  censored={fa.censored_slot_count}" if fa.censored_slot_count else ""
            )
            lines = (
                f"  forecast={fa.total_forecast_kwh:.3f}kWh  "
                f"observed={fa.total_observed_kwh:.3f}kWh  "
                f"error={fa.total_error_kwh:+.3f}kWh{censored_str}"
            )
            yield Static(lines)
            with Collapsible(title="Slots", collapsed=False):
                yield ForecastAccuracySlotsTable(fa)


@dataclass
class ForecastQualityCheckResult:
    """Result of the forecast quality deep-check: EMA bucket counts and metric ranges."""

    skipped: bool = False
    skip_reason: str = ""
    sunrise_utc: str = ""
    sunset_utc: str = ""
    group1_bucket_count: int = 0
    group2_bucket_count: int = 0
    group3_bucket_count: int = 0
    group3_pending_count: int = 0
    group1_buckets: list[dict] = field(default_factory=list)
    group2_buckets: list[dict] = field(default_factory=list)
    group3_buckets: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_forecast_quality(snap: Snapshot) -> ForecastQualityCheckResult:
    """Validate forecast quality store structure and metric plausibility.

    Args:
        snap: Coordinator snapshot containing pipeline.forecast_quality.

    Returns:
        ForecastQualityCheckResult with bucket counts and overall pass/fail.
    """
    result = ForecastQualityCheckResult()
    fq = snap.pipeline.get("forecast_quality")
    if not fq:
        result.skipped = True
        result.skip_reason = "pipeline.forecast_quality is null (no quality data yet)"
        return result

    result.sunrise_utc = fq.get("sunrise_utc") or ""
    result.sunset_utc  = fq.get("sunset_utc") or ""
    result.group3_pending_count = fq.get("group3_pending_count", 0)

    def _validate_buckets(group_dict: dict, label: str) -> list[dict]:
        """Sanity-check every EMA bucket in one quality group and return per-bucket rows.

        Args:
            group_dict: Mapping of bucket key → metrics dict from the debug payload.
            label: Group label used in the issue list when something fails.

        Returns:
            One dict per bucket, each carrying its metrics and any detected issues.
        """
        rows: list[dict] = []
        for key, m in (group_dict or {}).items():
            n = m.get("n", 0)
            mae = m.get("mae_wh")
            rmse = m.get("rmse_wh")
            mape = m.get("mape_pct")
            r2   = m.get("r2")
            ok = True
            issues = []
            if n < 0:
                ok = False
                issues.append("negative_n")
            if mae is not None and mae < 0:
                ok = False
                issues.append("negative_mae")
            if rmse is not None and rmse < 0:
                ok = False
                issues.append("negative_rmse")
            if mape is not None and mape < 0:
                ok = False
                issues.append("negative_mape")
            if r2 is not None and not (-10.0 <= r2 <= 1.0):
                ok = False
                issues.append("r2_out_of_range")
            if not ok:
                result.mismatches.append(f"{label}[{key}]: {','.join(issues)}")
                result.overall_ok = False
            rows.append({
                "key": key, "n": n, "mae_wh": mae, "rmse_wh": rmse,
                "bias_wh": m.get("bias_wh"), "mape_pct": mape, "r2": r2,
                "ok": ok,
            })
        return rows

    result.group1_buckets = _validate_buckets(fq.get("group1") or {}, "group1")
    result.group2_buckets = _validate_buckets(fq.get("group2") or {}, "group2")
    result.group3_buckets = _validate_buckets(fq.get("group3") or {}, "group3")
    result.group1_bucket_count = len(result.group1_buckets)
    result.group2_bucket_count = len(result.group2_buckets)
    result.group3_bucket_count = len(result.group3_buckets)
    return result


class ForecastQualityBucketTable(Static):
    """DataTable: bucket key | n | Bias | MAE | RMSE | MAPE% | R² | ✓/✗."""

    DEFAULT_CSS = """
    ForecastQualityBucketTable { height: auto; }
    ForecastQualityBucketTable DataTable { height: 14; }
    """

    def __init__(self, title: str, rows: list[dict]) -> None:
        """Initialise with a group title and pre-computed bucket rows.

        Args:
            title: Human-readable group label (e.g. "Group 1 — Intensity").
            rows: List of dicts from _validate_buckets() with key, n, metrics, ok.
        """
        super().__init__()
        self._title = title
        self._rows = rows

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield Static(f"  {self._title}", markup=False)
        yield DataTable(show_cursor=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Populate the DataTable with one row per bucket."""
        table = self.query_one(DataTable)
        table.add_columns("Bucket", "n", "Bias Wh", "MAE Wh", "RMSE Wh", "MAPE %", "R²", "")
        def fmt(v: float | None) -> str:
            """Format a value to 1 decimal place, or an em dash when None."""
            return f"{v:.1f}" if v is not None else "—"

        def fmt4(v: float | None) -> str:
            """Format a value to 4 decimal places, or an em dash when None."""
            return f"{v:.4f}" if v is not None else "—"

        for row in self._rows:
            ok_style = "" if row["ok"] else "red"
            table.add_row(
                Text(str(row["key"]),     style="cyan"),
                Text(str(row["n"])),
                Text(fmt(row["bias_wh"]),  style=ok_style),
                Text(fmt(row["mae_wh"]),   style=ok_style),
                Text(fmt(row["rmse_wh"]),  style=ok_style),
                Text(fmt(row["mape_pct"]), style=ok_style),
                Text(fmt4(row["r2"])),
                Text("✓" if row["ok"] else "✗", style="green" if row["ok"] else "red"),
            )


class ForecastQualityCheckWidget(Static):
    """Collapsible forecast quality deep-check: sunrise/sunset, per-group bucket tables."""

    DEFAULT_CSS = "ForecastQualityCheckWidget { height: auto; }"

    def __init__(self, fqr: ForecastQualityCheckResult) -> None:
        """Initialise with the pre-computed forecast quality check result.

        Args:
            fqr: Result of check_forecast_quality() for one coordinator.
        """
        super().__init__()
        self._fqr = fqr

    def compose(self) -> ComposeResult:
        """Render quality store summary and per-group bucket tables as a collapsible block."""
        fqr = self._fqr

        if fqr.skipped:
            yield Static(f"  ⚠  forecast_quality   SKIP   {fqr.skip_reason}")
            return

        color = "green" if fqr.overall_ok else "red"
        mark = "✓" if fqr.overall_ok else "✗"
        status = "PASS" if fqr.overall_ok else "FAIL"
        title = (
            f"[{color}]{mark}[/{color}]  forecast_quality   [{color}]{status}[/{color}]"
            f"   G1={fqr.group1_bucket_count}b"
            f"  G2={fqr.group2_bucket_count}b"
            f"  G3={fqr.group3_bucket_count}b"
            f"  pending={fqr.group3_pending_count}"
        )

        with Collapsible(title=title, collapsed=True):
            sunrise_str = fqr.sunrise_utc or "—"
            sunset_str  = fqr.sunset_utc  or "—"
            yield Static(
                f"  Sunrise UTC: {sunrise_str}\n"
                f"  Sunset  UTC: {sunset_str}\n"
                f"  Group3 pending: {fqr.group3_pending_count}",
                markup=False,
            )
            if fqr.mismatches:
                yield Static(
                    "  [red]Mismatches:[/red] " + ", ".join(fqr.mismatches),
                    markup=True,
                )
            if fqr.group1_buckets:
                sorted_g1 = sorted(fqr.group1_buckets, key=lambda r: int(r["key"]))
                yield ForecastQualityBucketTable("Group 1 — Intensity (forecast Wh bin)", sorted_g1)
            if fqr.group2_buckets:
                sorted_g2 = sorted(fqr.group2_buckets, key=lambda r: int(r["key"]))
                yield ForecastQualityBucketTable("Group 2 — Solar-Day Position (#1=sunrise, #N=sunset)", sorted_g2)
            if fqr.group3_buckets:
                sorted_g3 = sorted(fqr.group3_buckets, key=lambda r: int(r["key"]))
                yield ForecastQualityBucketTable("Group 3 — Forecast Horizon (d0=today … d6=6d ahead)", sorted_g3)
