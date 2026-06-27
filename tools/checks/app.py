"""The Textual TUI shell: deep-category set and IntegrationCheckApp."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Static

from .snapshot import Snapshot
from .registry import CheckResult
from .forecast import (
    ForecastCheckResult,
    ForecastCheckWidget,
    ForecastAccuracyCheckResult,
    ForecastAccuracyCheckWidget,
    ForecastQualityCheckResult,
    ForecastQualityCheckWidget,
)
from .pricing import PricingCheckResult, PricingCheckWidget
from .calculation import CalculationCheckResult, CalculationCheckWidget
from .schedule import ScheduleCheckResult, ScheduleCheckWidget
from .inverter import InverterModeCheckResult, InverterModeCheckWidget
from .battery import (
    BatteryCheckResult,
    BatteryCheckWidget,
    BatteryRuntimeCheckResult,
    BatteryRuntimeCheckWidget,
)
from .observed import (
    ObservedGenerationCheckResult,
    ObservedGenerationCheckWidget,
    ObservedGridCheckResult,
    ObservedGridCheckWidget,
    BakedObservedCheckResult,
    BakedObservedCheckWidget,
)
from .consumption import (
    BaseLoadCheckResult,
    BaseLoadCheckWidget,
    HouseholdConsumptionCheckResult,
    HouseholdConsumptionCheckWidget,
)
from .profitability import ProfitabilityCheckResult, ProfitabilityCheckWidget
from .billing import MonthlyBillCheckResult, MonthlyBillCheckWidget
from .derived import (
    ObservedConsumptionCheckResult,
    ObservedConsumptionCheckWidget,
    ObservedLossesCheckResult,
    ObservedLossesCheckWidget,
)


_DEEP_CATS: frozenset[str] = frozenset({
    "forecast", "pricing", "calculation", "schedule", "battery",
    "observed_generation", "observed_grid", "forecast_accuracy",
    "base_load", "battery_runtime",
    "household_consumption", "profitability", "forecast_quality",
    "monthly_bill", "baked_observed",
    "observed_consumption", "observed_losses",
    "inverter_mode",
})


class IntegrationCheckApp(App):
    """Textual TUI running in inline mode for the sunSale integration check."""

    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { height: auto; }
    Collapsible { height: auto; }
    ForecastCheckWidget { height: auto; }
    ForecastSlotsTable DataTable { height: 25; }
    ForecastSummaryTable DataTable { height: 12; }
    PricingCheckWidget { height: auto; }
    PricingSlotsTable DataTable { height: 22; }
    CalculationCheckWidget { height: auto; }
    CalculationSlotsTable DataTable { height: 22; }
    ScheduleCheckWidget { height: auto; }
    _SocSparkline Sparkline { height: 5; }
    ScheduleSlotsTable DataTable { height: 18; }
    BatteryCheckWidget { height: auto; }
    _BatteryDataTable DataTable { height: 12; }
    ObservedGenerationCheckWidget { height: auto; }
    ObservedGenerationSlotsTable DataTable { height: 18; }
    ObservedGridCheckWidget { height: auto; }
    ObservedGridSlotsTable DataTable { height: 22; }
    BakedObservedCheckWidget { height: auto; }
    BakedObservedRowsTable DataTable { height: 20; }
    ForecastAccuracyCheckWidget { height: auto; }
    ForecastAccuracySlotsTable DataTable { height: 18; }
    BaseLoadCheckWidget { height: auto; }
    BaseLoadSlotsTable DataTable { height: 15; }
    BatteryRuntimeCheckWidget { height: auto; }
    HouseholdConsumptionCheckWidget { height: auto; }
    ProfitabilityCheckWidget { height: auto; }
    ForecastQualityCheckWidget { height: auto; }
    ForecastQualityBucketTable DataTable { height: 14; }
    MonthlyBillCheckWidget { height: auto; }
    InverterModeCheckWidget { height: auto; }
    InverterModeBandsTable DataTable { height: 22; }
    """

    def __init__(
        self,
        report: list[tuple[Snapshot, list[CheckResult]]],
        forecast_results: dict[str, ForecastCheckResult],
        pricing_results: dict[str, PricingCheckResult],
        calculation_results: dict[str, CalculationCheckResult],
        schedule_results: dict[str, ScheduleCheckResult],
        battery_results: dict[str, BatteryCheckResult],
        observed_gen_results: dict[str, ObservedGenerationCheckResult],
        observed_grid_results: dict[str, ObservedGridCheckResult],
        baked_observed_results: dict[str, BakedObservedCheckResult],
        forecast_acc_results: dict[str, ForecastAccuracyCheckResult],
        base_load_results: dict[str, BaseLoadCheckResult],
        battery_runtime_results: dict[str, BatteryRuntimeCheckResult],
        household_consumption_results: dict[str, HouseholdConsumptionCheckResult],
        profitability_results: dict[str, ProfitabilityCheckResult],
        forecast_quality_results: dict[str, ForecastQualityCheckResult],
        monthly_bill_results: dict[str, MonthlyBillCheckResult],
        observed_consumption_results: dict[str, ObservedConsumptionCheckResult],
        observed_losses_results: dict[str, ObservedLossesCheckResult],
        inverter_mode_results: dict[str, InverterModeCheckResult],
    ) -> None:
        """Initialise with validator report and all deep-check results.

        Args:
            report: Per-snapshot check results from run_checks().
            forecast_results: Per-entry solar forecast deep-check results.
            pricing_results: Per-entry pricing deep-check results.
            calculation_results: Per-entry calculation deep-check results.
            schedule_results: Per-entry schedule deep-check results.
            battery_results: Per-entry battery state/status deep-check results.
            observed_gen_results: Per-entry observed generation deep-check results.
            observed_grid_results: Per-entry observed grid import/export deep-check results.
            baked_observed_results: Per-entry bake-in (counter vs baked) deep-check results.
            forecast_acc_results: Per-entry forecast accuracy deep-check results.
            base_load_results: Per-entry base load profile deep-check results.
            battery_runtime_results: Per-entry battery runtime deep-check results.
            household_consumption_results: Per-entry household consumption deep-check results.
            profitability_results: Per-entry profitability score deep-check results.
            forecast_quality_results: Per-entry forecast quality EMA bucket deep-check results.
            monthly_bill_results: Per-entry monthly electricity bill deep-check results.
            observed_consumption_results: Per-entry observed consumption deep-check results.
            observed_losses_results: Per-entry observed losses deep-check results.
            inverter_mode_results: Per-entry stitched history+plan band deep-check results.
        """
        super().__init__()
        self._report = report
        self._forecast_results = forecast_results
        self._pricing_results = pricing_results
        self._calculation_results = calculation_results
        self._schedule_results = schedule_results
        self._battery_results = battery_results
        self._observed_gen_results = observed_gen_results
        self._observed_grid_results = observed_grid_results
        self._baked_observed_results = baked_observed_results
        self._forecast_acc_results = forecast_acc_results
        self._base_load_results = base_load_results
        self._battery_runtime_results = battery_runtime_results
        self._household_consumption_results = household_consumption_results
        self._profitability_results = profitability_results
        self._forecast_quality_results = forecast_quality_results
        self._monthly_bill_results = monthly_bill_results
        self._observed_consumption_results = observed_consumption_results
        self._observed_losses_results = observed_losses_results
        self._inverter_mode_results = inverter_mode_results
        self.exit_code = 0

    def _all_deep(self) -> list:
        """Return every deep-check result across all modules and entries.

        Returns:
            Flat list of result objects each having skipped and overall_ok attributes.
        """
        return (
            list(self._forecast_results.values())
            + list(self._pricing_results.values())
            + list(self._calculation_results.values())
            + list(self._schedule_results.values())
            + list(self._battery_results.values())
            + list(self._observed_gen_results.values())
            + list(self._observed_grid_results.values())
            + list(self._baked_observed_results.values())
            + list(self._forecast_acc_results.values())
            + list(self._base_load_results.values())
            + list(self._battery_runtime_results.values())
            + list(self._household_consumption_results.values())
            + list(self._profitability_results.values())
            + list(self._forecast_quality_results.values())
            + list(self._monthly_bill_results.values())
            + list(self._observed_consumption_results.values())
            + list(self._observed_losses_results.values())
            + list(self._inverter_mode_results.values())
        )

    def compose(self) -> ComposeResult:
        """Yield validator lines for non-deep categories, then one deep widget per module."""
        for snap, results in self._report:
            cur_cat: str | None = None
            for res in results:
                if res.category in _DEEP_CATS:
                    continue
                if res.category != cur_cat:
                    yield Static(f"  [dim][{res.category}][/dim]", markup=True)
                    cur_cat = res.category
                color = "green" if res.ok else "red"
                mark = "✓" if res.ok else "✗"
                yield Static(
                    f"  [{color}]{mark}[/{color}]  {res.name}  [dim]{res.detail[:90]}[/dim]",
                    markup=True,
                )

            eid = snap.entry_id
            for label, widget_cls, results_map in (
                ("forecast",                ForecastCheckWidget,                self._forecast_results),
                ("pricing",                 PricingCheckWidget,                 self._pricing_results),
                ("calculation",             CalculationCheckWidget,             self._calculation_results),
                ("schedule",                ScheduleCheckWidget,                self._schedule_results),
                ("inverter_mode",           InverterModeCheckWidget,            self._inverter_mode_results),
                ("battery",                 BatteryCheckWidget,                 self._battery_results),
                ("observed_generation",     ObservedGenerationCheckWidget,      self._observed_gen_results),
                ("observed_grid",           ObservedGridCheckWidget,            self._observed_grid_results),
                ("baked_observed",          BakedObservedCheckWidget,           self._baked_observed_results),
                ("forecast_accuracy",       ForecastAccuracyCheckWidget,        self._forecast_acc_results),
                ("base_load",               BaseLoadCheckWidget,                self._base_load_results),
                ("battery_runtime",         BatteryRuntimeCheckWidget,          self._battery_runtime_results),
                ("household_consumption",   HouseholdConsumptionCheckWidget,    self._household_consumption_results),
                ("observed_consumption",    ObservedConsumptionCheckWidget,     self._observed_consumption_results),
                ("observed_losses",         ObservedLossesCheckWidget,          self._observed_losses_results),
                ("profitability",           ProfitabilityCheckWidget,           self._profitability_results),
                ("forecast_quality",        ForecastQualityCheckWidget,         self._forecast_quality_results),
                ("monthly_bill",            MonthlyBillCheckWidget,             self._monthly_bill_results),
            ):
                r = results_map.get(eid)
                if r is not None:
                    yield Static(f"  [dim][{label}][/dim]", markup=True)
                    yield widget_cls(r)

            yield Static("─" * 60)

        non_deep_total = sum(1 for _, rs in self._report for r in rs if r.category not in _DEEP_CATS)
        non_deep_passed = sum(
            1 for _, rs in self._report for r in rs
            if r.category not in _DEEP_CATS and r.ok
        )
        deep = self._all_deep()
        deep_total = sum(1 for r in deep if not r.skipped)
        deep_passed = sum(1 for r in deep if not r.skipped and r.overall_ok)
        all_total = non_deep_total + deep_total
        all_passed = non_deep_passed + deep_passed
        color = "green" if all_passed == all_total else "red"
        yield Static(
            f"[{color}]{all_passed}/{all_total} checks passed[/{color}]  [dim](q to quit)[/dim]",
            markup=True,
        )
        yield Footer()

    def on_mount(self) -> None:
        """Set exit code based on check results."""
        any_non_deep_failed = any(
            not r.ok for _, rs in self._report for r in rs if r.category not in _DEEP_CATS
        )
        any_deep_failed = any(
            not r.overall_ok and not r.skipped for r in self._all_deep()
        )
        self.exit_code = 1 if (any_non_deep_failed or any_deep_failed) else 0
