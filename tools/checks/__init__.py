"""sunSale integration validation harness.

This package was split out of the former ~5k-line ``tools/integration_check.py``
monolith so the per-pipeline-module deep-check convention (one ``check_<module>``
plus one ``<Module>CheckWidget`` per pipeline module, mandated by CLAUDE.md)
stays sustainable: each domain lives in its own module here.

Layout:
  - ``client``   — read-only HA REST client and connection constants.
  - ``snapshot`` — the per-coordinator Snapshot model and ``collect()``.
  - ``registry`` — ``CheckResult`` plus the ``@validator`` decorator/registry.
  - ``validators`` — the lightweight built-in validators (auto-registered).
  - one module per domain (``forecast``, ``pricing``, ``observed`` …), each
    owning its result dataclasses, ``check_*`` function(s), and TUI widget(s).
  - ``app`` — the Textual TUI shell (``IntegrationCheckApp``).
  - ``report`` — text/JSON/values rendering and ``run_checks``.
  - ``cli`` — the ``main()`` argument-parsing entry point.

Importing this package imports every submodule, which has the side effect of
registering all built-in validators into the global registry.
"""
from __future__ import annotations

from .client import (
    DEBUG_PATH,
    STATE_PATH,
    HAClient,
)
from .credentials import CredentialsError, resolve_credentials
from .snapshot import Snapshot, collect
from .registry import CheckResult, Validator, validator
from . import validators  # noqa: F401  — registers built-in validators on import

from .forecast import (
    ForecastAccuracyCheckResult,
    ForecastCheckResult,
    ForecastQualityCheckResult,
    check_forecast,
    check_forecast_accuracy,
    check_forecast_quality,
)
from .pricing import PricingCheckResult, check_pricing
from .calculation import CalculationCheckResult, check_calculation
from .schedule import ScheduleCheckResult, check_schedule
from .inverter import InverterModeCheckResult, check_inverter_mode
from .battery import (
    BatteryCheckResult,
    BatteryRuntimeCheckResult,
    check_battery,
    check_battery_runtime,
)
from .observed import (
    BakedObservedCheckResult,
    BakedObservedCheckRow,
    ObservedGenerationCheckResult,
    ObservedGridCheckResult,
    check_baked_observed,
    check_observed_generation,
    check_observed_grid,
)
from .consumption import (
    BaseLoadCheckResult,
    HouseholdConsumptionCheckResult,
    check_base_load,
    check_household_consumption,
)
from .profitability import ProfitabilityCheckResult, check_profitability
from .billing import MonthlyBillCheckResult, check_monthly_bill
from .derived import (
    ObservedConsumptionCheckResult,
    ObservedLossesCheckResult,
    check_observed_consumption,
    check_observed_losses,
)
from .report import render_json, render_text, render_values, run_checks
from .app import IntegrationCheckApp
from .cli import main

__all__ = [
    # infra
    "HAClient",
    "CredentialsError",
    "resolve_credentials",
    "DEBUG_PATH",
    "STATE_PATH",
    "Snapshot",
    "collect",
    "CheckResult",
    "Validator",
    "validator",
    # results
    "ForecastCheckResult",
    "ForecastAccuracyCheckResult",
    "ForecastQualityCheckResult",
    "PricingCheckResult",
    "CalculationCheckResult",
    "ScheduleCheckResult",
    "InverterModeCheckResult",
    "BatteryCheckResult",
    "BatteryRuntimeCheckResult",
    "ObservedGenerationCheckResult",
    "ObservedGridCheckResult",
    "BakedObservedCheckResult",
    "BakedObservedCheckRow",
    "BaseLoadCheckResult",
    "HouseholdConsumptionCheckResult",
    "ProfitabilityCheckResult",
    "MonthlyBillCheckResult",
    "ObservedConsumptionCheckResult",
    "ObservedLossesCheckResult",
    # checks
    "check_forecast",
    "check_forecast_accuracy",
    "check_forecast_quality",
    "check_pricing",
    "check_calculation",
    "check_schedule",
    "check_inverter_mode",
    "check_battery",
    "check_battery_runtime",
    "check_observed_generation",
    "check_observed_grid",
    "check_baked_observed",
    "check_base_load",
    "check_household_consumption",
    "check_profitability",
    "check_monthly_bill",
    "check_observed_consumption",
    "check_observed_losses",
    # reporting / TUI / entry point
    "run_checks",
    "render_text",
    "render_json",
    "render_values",
    "IntegrationCheckApp",
    "main",
]
