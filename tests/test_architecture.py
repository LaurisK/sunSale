"""Architecture guard — enforces the layering invariants the design rests on.

`docs/ARCHITECTURE.md` §9 describes a layered package:

    contract  →  (imports nothing internal, no Home Assistant)
    inbound / pipeline / outbound  →  the pure/edge middle layers
    orchestration  →  the glue that may import all of the above

Those directions are otherwise "enforced by convention" (a violation only
surfaces at runtime as a circular import). This module turns the crisp,
currently-true invariants into a build-time tripwire by statically parsing every
module's imports:

  1. `contract/` is foundational — it imports no other integration layer and no
     `homeassistant`.
  2. Nothing below `orchestration/` imports `orchestration/` — the glue layer is
     a sink, so the dependency graph across layers stays acyclic in that
     direction.
  3. The pure/edge layers reach `homeassistant` only from an explicit,
     reviewed allow-list of modules. Adding a new HA import to a pure layer must
     be a conscious edit here — which is exactly the boundary erosion the
     architecture warns about.
  4. The pure/edge layers import HA-platform *entry* modules (sensor, switch, …)
     via nothing; only the shared pure helpers `ha_state` / `currency` are
     allowed as root-level dependencies.

The messy peer edges between inbound/pipeline/outbound (documented back-edges
such as inbound→outbound, pipeline→inbound normalisers, outbound→inbound
telemetry seam) are intentionally *not* constrained here — they are real and
acyclic, and pinning them would only add churn. This guard covers the invariants
that are both load-bearing and unambiguous.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# --- Package geography -------------------------------------------------------

PKG_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "sun_sale"

# Top-level sub-packages that form the architectural layers.
LAYER_PACKAGES = {"contract", "inbound", "pipeline", "outbound", "orchestration"}

# Root-level `.py` modules that are HA entry points / shared helpers rather than
# a layer. The pure/edge layers may depend on the shared *helper* roots only.
ROOT_HELPER_MODULES = {"ha_state", "currency"}
ROOT_ENTRY_MODULES = {
    "__init__", "config_flow", "sensor", "switch", "select", "number",
}

# Modules below `orchestration/` that are permitted to import `homeassistant`.
# This is the reviewed HA-boundary surface: inbound resolvers (read the entity
# registry at setup), the recorder resampler, and the outbound writers. Keep it
# minimal — a new entry means a pure layer just grew a Home Assistant dependency.
HA_IMPORT_ALLOWLIST = {
    "inbound/forecast_resolver.py",
    "inbound/inverter_discovery.py",
    "inbound/inverter_entity_resolver.py",
    "inbound/observer/recorder_resample.py",
    "inbound/platform_profiles.py",
    "inbound/solis_entity_resolver.py",
    "outbound/entity_control.py",
    "outbound/heartbeat.py",
    "outbound/inverter_control_module.py",
    "outbound/inverter.py",
    "outbound/solis_dispatch_driver.py",
    "outbound/solis_driver.py",
}


def _iter_modules():
    """Yield every integration `.py` file paired with its parsed AST.

    Yields:
        Tuples of (relative_path_str, module_layer_or_None, ast.Module). The
        layer is the top-level sub-package name for files under a layer package,
        or None for root-level modules.
    """
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PKG_ROOT)
        layer = rel.parts[0] if rel.parts[0] in LAYER_PACKAGES else None
        tree = ast.parse(path.read_text(), filename=str(path))
        yield rel.as_posix(), layer, tree


def _resolve_target_layer(rel_path: str, node: ast.ImportFrom) -> str | None:
    """Resolve a relative ImportFrom to the integration layer it targets.

    Args:
        rel_path: The importing module's path relative to the package root.
        node: The `from ... import ...` node (only relative imports, level > 0).

    Returns:
        The top-level layer/root module name the import resolves to (e.g.
        "contract", "ha_state"), or None if it cannot be attributed (e.g. an
        intra-directory `from . import x`).
    """
    # Containing package parts, e.g. "pipeline/nodes/tier1.py" -> ["pipeline",
    # "nodes"]. Strip (level - 1) trailing parts to honour the leading dots.
    pkg_parts = rel_path.split("/")[:-1]
    anchor = pkg_parts[: len(pkg_parts) - (node.level - 1)]
    head = node.module.split(".")[0] if node.module else None
    parts = [*anchor, head] if head else anchor
    return parts[0] if parts else None


def _imports_homeassistant(tree: ast.Module) -> bool:
    """Return True if the module imports the `homeassistant` package in any form."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "homeassistant" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == "homeassistant":
                return True
    return False


def _internal_targets(rel_path: str, tree: ast.Module) -> set[str]:
    """Return the set of integration layer/root names a module imports.

    Args:
        rel_path: The importing module's path relative to the package root.
        tree: Its parsed AST.

    Returns:
        Layer or root-module names reachable via relative imports, excluding the
        module's own layer (intra-layer imports are not cross-layer edges).
    """
    own_layer = rel_path.split("/")[0]
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            target = _resolve_target_layer(rel_path, node)
            if target and target != own_layer:
                targets.add(target)
    return targets


# --- Invariant 1: contract is foundational ----------------------------------

def test_contract_imports_no_other_layer():
    """`contract/` must not import any other integration layer or root module."""
    forbidden = LAYER_PACKAGES | ROOT_HELPER_MODULES | ROOT_ENTRY_MODULES
    violations = []
    for rel, layer, tree in _iter_modules():
        if layer != "contract":
            continue
        bad = _internal_targets(rel, tree) & forbidden
        if bad:
            violations.append(f"{rel} imports {sorted(bad)}")
    assert not violations, (
        "contract/ must depend on nothing internal (it is the foundation):\n  "
        + "\n  ".join(violations)
    )


def test_contract_does_not_import_homeassistant():
    """`contract/` holds pure data types and must never touch Home Assistant."""
    violations = [
        rel
        for rel, layer, tree in _iter_modules()
        if layer == "contract" and _imports_homeassistant(tree)
    ]
    assert not violations, (
        "contract/ must not import homeassistant:\n  " + "\n  ".join(violations)
    )


# --- Invariant 2: orchestration is a sink -----------------------------------

def test_nothing_below_orchestration_imports_orchestration():
    """Only `orchestration/` glues layers; nothing beneath it may import it."""
    violations = []
    for rel, layer, tree in _iter_modules():
        if layer == "orchestration" or layer is None:
            continue
        if "orchestration" in _internal_targets(rel, tree):
            violations.append(rel)
    assert not violations, (
        "orchestration/ is the glue layer and must not be imported upward from "
        "contract/inbound/pipeline/outbound:\n  " + "\n  ".join(violations)
    )


# --- Invariant 3: the Home Assistant boundary is an explicit allow-list ------

def test_ha_imports_confined_to_allowlist():
    """Pure/edge layers import `homeassistant` only from reviewed modules.

    A failure here means either a new module legitimately grew an HA dependency
    (add it to HA_IMPORT_ALLOWLIST as a conscious decision) or a pure module
    reached for `hass` where it should have used the inbound/outbound seam.
    """
    offenders = set()
    for rel, layer, tree in _iter_modules():
        if layer not in {"inbound", "pipeline", "outbound"}:
            continue
        if _imports_homeassistant(tree):
            offenders.add(rel)
    unexpected = offenders - HA_IMPORT_ALLOWLIST
    stale = HA_IMPORT_ALLOWLIST - offenders
    assert not unexpected, (
        "Unexpected homeassistant import in a pure/edge layer. If intentional, "
        "add it to HA_IMPORT_ALLOWLIST:\n  " + "\n  ".join(sorted(unexpected))
    )
    assert not stale, (
        "HA_IMPORT_ALLOWLIST lists modules that no longer import homeassistant; "
        "remove them to keep the boundary tight:\n  " + "\n  ".join(sorted(stale))
    )


def test_pipeline_never_imports_homeassistant():
    """`pipeline/` is the pure computation core — it must stay HA-free.

    (Stricter than the allow-list: no pipeline module is on it, and none should
    ever be — the DAG nodes compute over already-translated domain types.)
    """
    violations = [
        rel
        for rel, layer, tree in _iter_modules()
        if layer == "pipeline" and _imports_homeassistant(tree)
    ]
    assert not violations, (
        "pipeline/ must remain pure Python with no homeassistant imports:\n  "
        + "\n  ".join(violations)
    )


# --- Invariant 4: pure layers don't reach into HA entry modules --------------

def test_pure_layers_do_not_import_ha_entry_modules():
    """contract/inbound/pipeline/outbound may use root helpers, not HA platforms.

    The shared pure helpers `ha_state` / `currency` are allowed; the HA-platform
    entry modules (`sensor`, `switch`, `config_flow`, …) are not — those belong
    to the HA edge and orchestration only.
    """
    violations = []
    for rel, layer, tree in _iter_modules():
        if layer not in {"contract", "inbound", "pipeline", "outbound"}:
            continue
        bad = _internal_targets(rel, tree) & ROOT_ENTRY_MODULES
        if bad:
            violations.append(f"{rel} imports {sorted(bad)}")
    assert not violations, (
        "Pure/edge layers must not import HA-platform entry modules:\n  "
        + "\n  ".join(violations)
    )


def test_guard_actually_scanned_the_package():
    """Sanity: the walker found the expected layers (guards against a bad path)."""
    seen_layers = {layer for _, layer, _ in _iter_modules() if layer}
    assert LAYER_PACKAGES <= seen_layers, (
        f"Expected all layers scanned; missing {LAYER_PACKAGES - seen_layers}"
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
