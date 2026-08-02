"""Import every integration module against a real Home Assistant install.

The unit-test suite stubs ``homeassistant`` wholesale (``tests/conftest.py``
injects auto-mock modules into ``sys.modules``), so a renamed or removed HA API
is invisible to it — every attribute access simply yields a ``MagicMock``. This
script closes that gap: it walks ``custom_components/sun_sale`` and imports each
module against the *real* HA package (installed in the CI ``import-smoke`` job).

Importing alone catches the drift that matters here — removed symbols, moved
modules, and class-definition-time signature breakage — without needing a
running HA event loop. Modules are expected to be side-effect-free at import
time; if one needs a live loop to import, that is a finding, not a script bug.

Run: ``python tools/ci_import_check.py`` (exits non-zero if any import fails).
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

# Repo root = parent of this ``tools/`` directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _REPO_ROOT / "custom_components" / "sun_sale"


def _iter_module_names() -> list[str]:
    """Return the dotted module name of every importable source file.

    Walks ``custom_components/sun_sale`` for ``*.py`` files, skips
    ``__pycache__``, and converts each path to a dotted module name relative to
    the repo root (so ``custom_components/sun_sale/orchestration/coordinator.py``
    becomes ``custom_components.sun_sale.orchestration.coordinator``). Package
    ``__init__.py`` files map to their package name.

    Returns:
        Sorted list of dotted module names.
    """
    names: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_REPO_ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def main() -> int:
    """Import every integration module and report failures.

    Returns:
        Process exit code: 0 if all modules import cleanly, 1 otherwise.
    """
    # Ensure the repo root is importable so ``custom_components.*`` resolves.
    sys.path.insert(0, str(_REPO_ROOT))

    failures: list[tuple[str, str]] = []
    module_names = _iter_module_names()
    for name in module_names:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - report every failure, keep going
            failures.append((name, traceback.format_exc()))

    print(f"Checked {len(module_names)} modules.")
    if failures:
        print(f"\n{len(failures)} module(s) failed to import:\n")
        for name, tb in failures:
            print(f"--- {name} ---")
            print(tb)
        return 1

    print("All modules imported cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
