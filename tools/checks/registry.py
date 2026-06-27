"""Validator registry: CheckResult plus the @validator decorator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .snapshot import Snapshot


@dataclass
class CheckResult:
    """Outcome of a single validator run."""

    name: str
    category: str
    ok: bool
    detail: str = ""


Validator = Callable[[Snapshot], CheckResult]


_VALIDATORS: list[Validator] = []


def validator(name: str, category: str) -> Callable[[Callable[[Snapshot], tuple[bool, str]]], Validator]:
    """Decorator: register a ``(ok, detail)``-returning fn as a Snapshot validator.

    The decorated function may raise; the wrapper traps the exception and
    reports it as a failed CheckResult so a single broken validator can't
    abort the harness.

    Args:
        name: Display name used in the report.
        category: Logical grouping (e.g. pipeline module name) for filtering.

    Returns:
        The same callable but registered in the global ``_VALIDATORS`` list.
    """

    def wrap(fn: Callable[[Snapshot], tuple[bool, str]]) -> Validator:
        """Wrap ``fn`` into a CheckResult-producing validator and register it."""
        def run(snap: Snapshot) -> CheckResult:
            """Invoke the wrapped validator on a snapshot, trapping exceptions."""
            try:
                ok, detail = fn(snap)
            except Exception as exc:  # noqa: BLE001
                return CheckResult(name, category, False, f"raised {type(exc).__name__}: {exc}")
            return CheckResult(name, category, ok, detail)

        run.__name__ = fn.__name__
        _VALIDATORS.append(run)
        return run

    return wrap
