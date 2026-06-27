#!/usr/bin/env python3
"""sunSale integration validation harness (entry point).

Fetches a live snapshot from a running Home Assistant instance:
  - integration parameters (config) and per-module deliverables from
    /api/sun_sale/debug
  - raw HA state for every entity the integration consumes from
    /api/states/<entity_id>

Then runs a registry of validators against that snapshot and reports
pass/fail. Designed to be extended by adding more checks.

The implementation lives in the sibling ``checks`` package — one module per
pipeline domain — so the per-module deep-check convention stays sustainable.
This file stays as the documented entry point and re-exports the package's
public API for backwards compatibility (e.g. ``from tools.integration_check
import Snapshot, check_baked_observed`` used by the test suite).

Usage:
    HA_URL=http://host:port HA_TOKEN=... python tools/integration_check.py
    python tools/integration_check.py --json
    python tools/integration_check.py --filter pricing
    python tools/integration_check.py --values   # dump integration/consumed/exposed values
"""
from __future__ import annotations

import os
import sys

# Make the sibling ``checks`` package importable as a top-level package whether
# this file is executed directly (``python tools/integration_check.py`` — its
# own directory is already sys.path[0]) or imported as ``tools.integration_check``
# (the test suite does this, with only the repo root on sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checks import *  # noqa: E402,F401,F403  — re-export public API for back-compat
from checks import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
