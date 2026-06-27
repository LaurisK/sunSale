"""Battery state and battery-runtime deep checks and their TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, DataTable, Static

from .snapshot import Snapshot


@dataclass
class BatteryCheckResult:
    """Result of the battery deep-check: SOC range, capacity, power limits, and degradation cost."""

    skipped: bool = False
    skip_reason: str = ""
    # BatteryState (inputs.battery)
    soc: float | None = None
    power_kw: float | None = None
    estimated_capacity_kwh: float | None = None
    grid_power_kw: float | None = None
    # BatteryStatus (pipeline.battery_status)
    total_capacity_kwh: float | None = None
    max_charge_power_kw: float | None = None
    max_discharge_power_kw: float | None = None
    remaining_capacity_kwh: float | None = None
    # DegradationCost (pipeline.degradation_cost_per_kwh)
    degradation_cost_per_kwh: float | None = None
    expected_remaining_capacity_kwh: float | None = None
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_battery(snap: Snapshot) -> BatteryCheckResult:
    """Validate battery SOC, capacity consistency, and degradation cost presence.

    Args:
        snap: Coordinator snapshot containing inputs.battery, pipeline.battery_status,
              and pipeline.degradation_cost_per_kwh.

    Returns:
        BatteryCheckResult with observed values and overall pass/fail.
    """
    result = BatteryCheckResult()

    battery = snap.inputs.get("battery")
    if battery is None:
        result.skipped = True
        result.skip_reason = "inputs.battery is null (no battery configured)"
        return result

    result.soc = battery.get("soc")
    result.power_kw = battery.get("power_kw")
    result.estimated_capacity_kwh = battery.get("estimated_capacity_kwh")
    result.grid_power_kw = snap.inputs.get("grid_power_kw")
    result.degradation_cost_per_kwh = snap.pipeline.get("degradation_cost_per_kwh")

    bs = snap.pipeline.get("battery_status")
    if bs:
        result.total_capacity_kwh = bs.get("total_capacity_kwh")
        result.max_charge_power_kw = bs.get("max_charge_power_kw")
        result.max_discharge_power_kw = bs.get("max_discharge_power_kw")
        result.remaining_capacity_kwh = bs.get("remaining_capacity_kwh")

    # soc is stored as 0.0–1.0 fraction
    if result.soc is not None and not (0.0 <= result.soc <= 1.0):
        result.mismatches.append("soc_out_of_range")
        result.overall_ok = False

    if result.total_capacity_kwh and result.soc is not None and result.remaining_capacity_kwh is not None:
        expected_rem = result.soc * result.total_capacity_kwh
        result.expected_remaining_capacity_kwh = expected_rem
        if abs(expected_rem - result.remaining_capacity_kwh) > 0.1:
            result.mismatches.append("remaining_capacity_inconsistent")
            result.overall_ok = False

    if result.estimated_capacity_kwh:
        if result.degradation_cost_per_kwh is None or result.degradation_cost_per_kwh <= 0:
            result.mismatches.append("degradation_cost_missing")
            result.overall_ok = False

    return result






# Per-side tolerances for the bake-in fault threshold. A record is flagged
# only when both the absolute and relative thresholds are exceeded; the
# combined rule is ``|counter - baked| > max(abs_tol, rel_tol × counter)``.


class _BatteryDataTable(Static):
    """DataTable of battery field/value/status rows."""

    DEFAULT_CSS = """
    _BatteryDataTable { height: auto; }
    _BatteryDataTable DataTable { height: 12; }
    """

    def __init__(self, bc: BatteryCheckResult) -> None:
        """Initialise with the battery check result.

        Args:
            bc: Result of check_battery() containing observed battery values.
        """
        super().__init__()
        self._bc = bc

    def compose(self) -> ComposeResult:
        """Yield the DataTable placeholder; rows are added in on_mount."""
        yield DataTable(show_cursor=False)

    def on_mount(self) -> None:
        """Populate the DataTable with battery state, status, and degradation rows."""
        bc = self._bc
        table = self.query_one(DataTable)
        table.add_columns("Field", "Value", "")
        dim = "dim"

        def _fv(v: float | None, fmt: str = ".3f") -> Text:
            """Format a numeric field, dimming the cell when the value is None."""
            return Text(f"{v:{fmt}}") if v is not None else Text("—", style=dim)

        # soc is 0.0–1.0 fraction; display as percentage
        soc_ok = bc.soc is None or 0.0 <= bc.soc <= 1.0
        table.add_row(
            Text("SOC"),
            Text(f"{bc.soc:.1%}") if bc.soc is not None else Text("—", style=dim),
            Text("✓" if soc_ok else "✗", style="green" if soc_ok else "red"),
        )
        table.add_row(Text("Battery power (kW)"), _fv(bc.power_kw), Text(""))
        table.add_row(Text("Grid power (kW)"), _fv(bc.grid_power_kw), Text(""))
        table.add_row(Text("Est. capacity (kWh)"), _fv(bc.estimated_capacity_kwh), Text(""))

        if bc.total_capacity_kwh is not None:
            table.add_row(Text("Total capacity (kWh)"), _fv(bc.total_capacity_kwh), Text(""))
        if bc.max_charge_power_kw is not None:
            table.add_row(Text("Max charge (kW)"), _fv(bc.max_charge_power_kw), Text(""))
        if bc.max_discharge_power_kw is not None:
            table.add_row(Text("Max discharge (kW)"), _fv(bc.max_discharge_power_kw), Text(""))

        if bc.remaining_capacity_kwh is not None:
            rem_ok = "remaining_capacity_inconsistent" not in bc.mismatches
            exp_str = (
                f"exp {bc.expected_remaining_capacity_kwh:.3f} → "
                if bc.expected_remaining_capacity_kwh is not None else ""
            )
            table.add_row(
                Text("Remaining capacity (kWh)"),
                Text(f"{exp_str}{bc.remaining_capacity_kwh:.3f}", style="" if rem_ok else "red"),
                Text("✓" if rem_ok else "✗", style="green" if rem_ok else "red"),
            )

        deg_ok = not (
            bc.estimated_capacity_kwh
            and (bc.degradation_cost_per_kwh is None or bc.degradation_cost_per_kwh <= 0)
        )
        table.add_row(
            Text("Degradation cost (€/kWh)"),
            _fv(bc.degradation_cost_per_kwh, ".5f"),
            Text("✓" if deg_ok else "✗", style="green" if deg_ok else "red"),
        )


class BatteryCheckWidget(Static):
    """Collapsible battery deep-check: SOC, power, capacity, and degradation cost."""

    DEFAULT_CSS = "BatteryCheckWidget { height: auto; }"

    def __init__(self, bc: BatteryCheckResult) -> None:
        """Initialise with the pre-computed battery check result.

        Args:
            bc: Result of check_battery() for one coordinator.
        """
        super().__init__()
        self._bc = bc

    def compose(self) -> ComposeResult:
        """Render a simple data table with battery state and sanity checks."""
        bc = self._bc

        if bc.skipped:
            yield Static(f"  ⚠  battery_check   SKIP   {bc.skip_reason}")
            return

        color = "green" if bc.overall_ok else "red"
        mark = "✓" if bc.overall_ok else "✗"
        status = "PASS" if bc.overall_ok else "FAIL"
        soc_str = f"{bc.soc:.1%}" if bc.soc is not None else "—"
        title = (
            f"[{color}]{mark}[/{color}]  battery_check   [{color}]{status}[/{color}]"
            f"   SOC={soc_str}"
        )

        with Collapsible(title=title, collapsed=True):
            yield _BatteryDataTable(bc)


@dataclass
class BatteryRuntimeCheckResult:
    """Result of the battery-runtime deep-check: usable kWh, drain rate, and runtime estimate."""

    skipped: bool = False
    skip_reason: str = ""
    remaining_kwh_usable: float = 0.0
    avg_drain_kw_next_hour: float = 0.0
    runtime_minutes: float | None = None
    expected_runtime_minutes: float | None = None
    until: str | None = None
    horizon_hours: int = 0
    computed_at: str = ""
    mismatches: list[str] = field(default_factory=list)
    overall_ok: bool = True


def check_battery_runtime(snap: Snapshot) -> BatteryRuntimeCheckResult:
    """Validate battery runtime estimate: non-negative remaining kWh and drain rate.

    Args:
        snap: Coordinator snapshot containing pipeline.battery_runtime.

    Returns:
        BatteryRuntimeCheckResult with runtime data and overall pass/fail.
    """
    result = BatteryRuntimeCheckResult()

    brt = snap.pipeline.get("battery_runtime")
    if not brt:
        result.skipped = True
        result.skip_reason = "pipeline.battery_runtime is null"
        return result

    result.remaining_kwh_usable = brt.get("remaining_kwh_usable", 0.0)
    result.avg_drain_kw_next_hour = brt.get("avg_drain_kw_next_hour", 0.0)
    result.runtime_minutes = brt.get("runtime_minutes")
    result.until = brt.get("until")
    result.horizon_hours = brt.get("horizon_hours", 0)
    result.computed_at = brt.get("computed_at", "")

    if result.remaining_kwh_usable < -1e-6:
        result.mismatches.append("negative_remaining_kwh")
        result.overall_ok = False

    if result.avg_drain_kw_next_hour < 0:
        result.mismatches.append("negative_drain_rate")
        result.overall_ok = False

    if result.avg_drain_kw_next_hour > 0:
        expected_rt = (result.remaining_kwh_usable / result.avg_drain_kw_next_hour) * 60.0
        result.expected_runtime_minutes = round(expected_rt, 1)
        if result.runtime_minutes is not None and abs(result.runtime_minutes - expected_rt) > 1.0:
            result.mismatches.append("runtime_formula_mismatch")
            result.overall_ok = False

    return result


class BatteryRuntimeCheckWidget(Static):
    """Collapsible battery-runtime deep-check: usable kWh, drain rate, and until estimate."""

    DEFAULT_CSS = "BatteryRuntimeCheckWidget { height: auto; }"

    def __init__(self, brt: BatteryRuntimeCheckResult) -> None:
        """Initialise with the pre-computed battery runtime check result.

        Args:
            brt: Result of check_battery_runtime() for one coordinator.
        """
        super().__init__()
        self._brt = brt

    def compose(self) -> ComposeResult:
        """Render runtime fields as a simple Static block."""
        brt = self._brt

        if brt.skipped:
            yield Static(f"  ⚠  battery_runtime_check   SKIP   {brt.skip_reason}")
            return

        color = "green" if brt.overall_ok else "red"
        mark = "✓" if brt.overall_ok else "✗"
        status = "PASS" if brt.overall_ok else "FAIL"
        rt_str = f"{brt.runtime_minutes:.0f}min" if brt.runtime_minutes is not None else "∞"
        title = (
            f"[{color}]{mark}[/{color}]  battery_runtime_check   [{color}]{status}[/{color}]"
            f"   {brt.remaining_kwh_usable:.3f}kWh usable"
            f"   drain={brt.avg_drain_kw_next_hour:.3f}kW"
            f"   runtime={rt_str}"
        )

        with Collapsible(title=title, collapsed=True):
            rt_ok = "runtime_formula_mismatch" not in brt.mismatches
            rt_style = "green" if rt_ok else "red"
            exp_rt_str = (
                f"  Expected runtime: [{rt_style}]{brt.expected_runtime_minutes:.0f}[/{rt_style}]min"
                f"  (usable/drain×60)  actual: {rt_str}\n"
                if brt.expected_runtime_minutes is not None else ""
            )
            lines = [
                f"  Remaining usable: {brt.remaining_kwh_usable:.4f} kWh",
                f"  Avg drain next hour: {brt.avg_drain_kw_next_hour:.4f} kW",
                f"  Runtime: {rt_str}",
                f"  Until: {brt.until or '—'}",
                f"  Horizon: {brt.horizon_hours}h",
                f"  Computed at: {brt.computed_at}",
            ]
            yield Static(exp_rt_str + "\n".join(lines), markup=True)
