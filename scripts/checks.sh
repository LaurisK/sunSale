#!/usr/bin/env bash
#
# Run the same gates CI runs, with the same arguments.
#
# This script is the single source of truth for "does this pass CI". The
# commands below are mirrored verbatim in .github/workflows/ci.yml; if one
# changes, change both. Local invocations that differ from CI are worse than
# useless -- they hand out false confidence, which is how a failing commit gets
# pushed in the first place.
#
# Covers the three CI jobs that are reproducible without a real Home Assistant
# install: ruff, mypy, pytest. It does NOT cover import-smoke (installs real
# HA), hassfest, HACS, or the solis_modbus contract guard (clones upstream) --
# those still run only in CI, so a green run here is necessary, not sufficient.
#
# Usage:
#   scripts/checks.sh            # ruff + mypy + pytest
#   scripts/checks.sh --fast     # ruff + mypy only (skips the suite)
#
# Wired to pre-push by scripts/install-hooks.sh; see CONTRIBUTING.md.

set -uo pipefail

cd "$(dirname "$0")/.."

# Prefer the repo venv so local runs use the pinned tool versions rather than
# whatever happens to be on PATH -- the drift between them is exactly what this
# script exists to catch.
if [[ -x "venv/bin/python" ]]; then
    PY="venv/bin/python"
else
    PY="python3"
fi

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

failed=()

run_gate() {
    local name="$1"; shift
    printf '\n\033[1m>>> %s\033[0m\n' "$name"
    if "$@"; then
        printf '\033[32m    %s OK\033[0m\n' "$name"
    else
        printf '\033[31m    %s FAILED\033[0m\n' "$name"
        failed+=("$name")
    fi
}

run_gate "ruff"  "$PY" -m ruff check custom_components tools tests
run_gate "mypy"  "$PY" -m mypy custom_components/sun_sale
if [[ "$FAST" -eq 0 ]]; then
    run_gate "pytest" "$PY" -m pytest -q
fi

echo
if [[ ${#failed[@]} -eq 0 ]]; then
    printf '\033[32mAll gates passed.\033[0m\n'
    exit 0
fi
printf '\033[31mFailed: %s\033[0m\n' "${failed[*]}"
printf 'Fix these before pushing (ruff issues are often `ruff check --fix`-able).\n'
exit 1
