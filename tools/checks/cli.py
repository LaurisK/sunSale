"""Command-line entry point for the integration validation harness."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error

from .app import IntegrationCheckApp
from .battery import check_battery, check_battery_runtime
from .billing import check_monthly_bill
from .calculation import check_calculation
from .capability import check_capability
from .client import HAClient
from .consumption import check_base_load, check_household_consumption
from .credentials import CredentialsError, resolve_credentials
from .derived import check_observed_consumption, check_observed_losses
from .forecast import (
    check_forecast,
    check_forecast_accuracy,
    check_array_calibration,
    check_forecast_quality,
)
from .inverter import check_inverter_mode
from .observed import (
    check_baked_observed,
    check_observed_generation,
    check_observed_grid,
)
from .price_forecast import check_price_forecast
from .pricing import check_pricing
from .profitability import check_profitability
from .report import render_json, render_values, run_checks
from .schedule import check_schedule
from .snapshot import collect


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args, collect snapshots, and dispatch to the requested output mode.

    Args:
        argv: Optional CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (0 on success, non-zero on validator failure or
        infrastructure errors).
    """
    p = argparse.ArgumentParser(description="sunSale integration validation harness")
    p.add_argument("--url", default=None, help="HA base URL (else HA_URL env / secrets.json)")
    p.add_argument("--token", default=None, help="HA token (else HA_TOKEN env / secrets.json)")
    p.add_argument("--json", action="store_true", help="emit JSON report (no TUI)")
    p.add_argument("--filter", help="only run checks in this category")
    p.add_argument("--dump-snapshot", action="store_true", help="print raw snapshot then exit")
    p.add_argument(
        "--values",
        action="store_true",
        help="print all integration/consumed/exposed values then exit",
    )
    args = p.parse_args(argv)

    try:
        url, token = resolve_credentials(args.url, args.token)
    except CredentialsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    client = HAClient(url, token)
    try:
        snapshots = collect(client)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"ERROR: could not reach HA at {url}: {e}", file=sys.stderr)
        return 2

    if not snapshots:
        print("ERROR: HA returned 0 coordinator entries — integration not loaded?", file=sys.stderr)
        return 2

    if args.dump_snapshot:
        print(json.dumps(
            [{"entry_id": s.entry_id, "debug": s.debug, "raw_entities": s.raw_entities} for s in snapshots],
            indent=2,
        ))
        return 0

    if args.values:
        print(render_values(snapshots))
        return 0

    report = run_checks(snapshots, args.filter)

    if args.json:
        print(render_json(report))
        any_failed = any(not r.ok for _, results in report for r in results)
        return 1 if any_failed else 0

    forecast_results               = {s.entry_id: check_forecast(s)                  for s in snapshots}
    pricing_results                = {s.entry_id: check_pricing(s)                   for s in snapshots}
    calculation_results            = {s.entry_id: check_calculation(s)               for s in snapshots}
    schedule_results               = {s.entry_id: check_schedule(s)                  for s in snapshots}
    battery_results                = {s.entry_id: check_battery(s)                   for s in snapshots}
    observed_gen_results           = {s.entry_id: check_observed_generation(s)       for s in snapshots}
    observed_grid_results          = {s.entry_id: check_observed_grid(s)             for s in snapshots}
    baked_observed_results         = {s.entry_id: check_baked_observed(s)            for s in snapshots}
    forecast_acc_results           = {s.entry_id: check_forecast_accuracy(s)         for s in snapshots}
    base_load_results              = {s.entry_id: check_base_load(s)                 for s in snapshots}
    battery_runtime_results        = {s.entry_id: check_battery_runtime(s)           for s in snapshots}
    household_consumption_results  = {s.entry_id: check_household_consumption(s)     for s in snapshots}
    profitability_results          = {s.entry_id: check_profitability(s)             for s in snapshots}
    forecast_quality_results       = {s.entry_id: check_forecast_quality(s)          for s in snapshots}
    array_calibration_results      = {s.entry_id: check_array_calibration(s)         for s in snapshots}
    monthly_bill_results           = {s.entry_id: check_monthly_bill(s)              for s in snapshots}
    observed_consumption_results   = {s.entry_id: check_observed_consumption(s)      for s in snapshots}
    observed_losses_results        = {s.entry_id: check_observed_losses(s)           for s in snapshots}
    inverter_mode_results          = {s.entry_id: check_inverter_mode(s)             for s in snapshots}
    capability_results             = {s.entry_id: check_capability(s)                for s in snapshots}
    price_forecast_results         = {s.entry_id: check_price_forecast(s)            for s in snapshots}
    app = IntegrationCheckApp(
        report,
        forecast_results, pricing_results, calculation_results,
        schedule_results, battery_results,
        observed_gen_results, observed_grid_results,
        baked_observed_results,
        forecast_acc_results,
        base_load_results, battery_runtime_results,
        household_consumption_results, profitability_results,
        forecast_quality_results,
        array_calibration_results,
        monthly_bill_results,
        observed_consumption_results,
        observed_losses_results,
        inverter_mode_results,
        capability_results,
        price_forecast_results,
    )
    app.run(inline=True)
    return app.exit_code
