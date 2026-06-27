"""Run the validator registry and render text / JSON / values reports."""
from __future__ import annotations

import json
from typing import Any

from .registry import CheckResult, _VALIDATORS
from .snapshot import Snapshot


def run_checks(snapshots: list[Snapshot], category_filter: str | None) -> list[tuple[Snapshot, list[CheckResult]]]:
    """Run every registered validator against each snapshot.

    Args:
        snapshots: Coordinator snapshots collected from the debug endpoint.
        category_filter: When non-empty, only validators matching this category
            are executed; others are silently dropped.

    Returns:
        List of ``(snapshot, results)`` tuples preserving snapshot order.
    """
    out: list[tuple[Snapshot, list[CheckResult]]] = []
    for snap in snapshots:
        results = []
        for v in _VALIDATORS:
            res = v(snap)
            if category_filter and res.category != category_filter:
                continue
            results.append(res)
        out.append((snap, results))
    return out


def render_text(report: list[tuple[Snapshot, list[CheckResult]]]) -> str:
    """Render the check report as a plain-text PASS/FAIL listing with a summary.

    Args:
        report: Output of ``run_checks``.

    Returns:
        Multi-line string suitable for stdout.
    """
    lines: list[str] = []
    total = passed = 0
    for snap, results in report:
        lines.append(f"== entry {snap.entry_id} ==")
        cur_cat = None
        for r in results:
            if r.category != cur_cat:
                lines.append(f"  [{r.category}]")
                cur_cat = r.category
            mark = "PASS" if r.ok else "FAIL"
            lines.append(f"    {mark}  {r.name}: {r.detail}")
            total += 1
            if r.ok:
                passed += 1
        lines.append("")
    lines.append(f"{passed}/{total} checks passed")
    return "\n".join(lines)


def render_json(report: list[tuple[Snapshot, list[CheckResult]]]) -> str:
    """Render the check report as a JSON document keyed by coordinator entry.

    Args:
        report: Output of ``run_checks``.

    Returns:
        Pretty-printed JSON string.
    """
    out = []
    for snap, results in report:
        out.append({
            "entry_id": snap.entry_id,
            "checks": [
                {"name": r.name, "category": r.category, "ok": r.ok, "detail": r.detail}
                for r in results
            ],
        })
    return json.dumps(out, indent=2)


_INTEGRATION_INPUT_KEYS = frozenset({"tariff_config"})


def _section(title: str, payload: Any) -> str:
    """Format a ``[title]``-headed JSON block for the values dump.

    Args:
        title: Section header (no surrounding brackets).
        payload: Any JSON-serialisable structure.

    Returns:
        Two-line section: header plus indented JSON body.
    """
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return f"[{title}]\n{body}"


def render_values(snapshots: list[Snapshot]) -> str:
    """Dump every snapshot's integration config, consumed inputs, and exposed outputs.

    Args:
        snapshots: Coordinator snapshots collected from the debug endpoint.

    Returns:
        Multi-section plain-text dump.
    """
    parts: list[str] = []
    for snap in snapshots:
        debug = snap.debug
        inputs = snap.inputs
        consumed_primary = {
            k: v for k, v in inputs.items() if k not in _INTEGRATION_INPUT_KEYS
        }
        integration = {
            "entry_id": snap.entry_id,
            "automation_enabled": debug.get("automation_enabled"),
            "config": snap.config,
            **{k: inputs.get(k) for k in _INTEGRATION_INPUT_KEYS if k in inputs},
        }
        consumed = {
            "raw_entities": snap.raw_entities,
            "primary": consumed_primary,
        }
        exposed = {
            "pipeline": snap.pipeline,
            "outputs": snap.outputs,
            "last_dispatched_action": debug.get("last_dispatched_action"),
            "last_dispatched_at": debug.get("last_dispatched_at"),
            "timestamp": debug.get("timestamp"),
        }
        parts.append(f"== entry {snap.entry_id} ==\n")
        parts.append(_section("INTEGRATION", integration))
        parts.append("")
        parts.append(_section("CONSUMED", consumed))
        parts.append("")
        parts.append(_section("EXPOSED", exposed))
        parts.append("")
    return "\n".join(parts)
