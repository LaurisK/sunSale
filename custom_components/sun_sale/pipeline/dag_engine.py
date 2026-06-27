"""DAG Task Engine — tiered directed acyclic graph executor.

Architecture:
  - Each DagNode declares its output_type, the types it consumes, and
    (optionally) the types it consumes_optional. Execution tiers are *derived*,
    not hand-assigned: DagEngine computes each node's tier by longest-path
    layering over the consumes + consumes_optional / output_type graph, so a
    node always lands strictly above every node whose output it consumes —
    required or optional. Adding a node is a non-decision — its tier falls out
    of what it consumes.
  - consumes vs consumes_optional: both edges raise a consumer's tier above its
    producer (so ordering is guaranteed either way), but only consumes gates
    readiness. An optional dependency that is absent this cycle (its producer
    failed or was skipped) does NOT stop the consumer from running — it reads
    None and degrades. This lets a node order itself after an optional input it
    reads via ctx.get without making that input mandatory.
  - Constructing the engine raises DependencyCycleError if the graph contains a
    cycle (a node that transitively consumes its own output); an acyclic graph
    always yields a finite layering.
  - On each run cycle the engine executes tiers in ascending order; within a tier
    all eligible nodes run in parallel (asyncio.gather). Each tier runs to
    completion before the next begins, so every lower-tier output is present in
    NodeContext.secondary by the time a node's tier is evaluated.
  - Node readiness is therefore derived directly from ctx.secondary. There is no
    cross-node notification or per-node "satisfied" state, so nothing can leak
    between cycles. When a node completes it deposits its result in
    NodeContext.secondary.

No Home Assistant imports — pure Python.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from ..contract.models import SunSaleConfig

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class DependencyCycleError(Exception):
    """Raised when the consumes/output_type graph contains a dependency cycle.

    A cycle means a node transitively consumes its own output, so no finite
    tier layering exists. Detected once, at engine construction.
    """


class MissingDependencyError(KeyError):
    """Raised when NodeContext.require() cannot find the requested type."""


@dataclass
class NodeContext:
    """Shared mutable context for one DAG run cycle.

    primary   — translation-layer outputs (HA state → domain types).
                Pre-populated before the DAG starts; never modified during the run.
    secondary — node outputs, keyed by output_type.
                Grows as nodes complete during the run.
    config    — structured user configuration, read-only.
    now       — cycle timestamp.
    """
    primary: dict[type, Any]
    secondary: dict[type, Any]
    config: SunSaleConfig
    now: datetime

    def get(self, t: type[T]) -> T | None:
        """Look up type t in primary then secondary context; return None if absent.

        Primary takes precedence whenever it holds the key, even if the stored
        value is falsy (None, empty collection, a NamedTuple). Membership — not
        truthiness — decides the fallthrough, so a deliberately-stored primary
        value is never shadowed by a stale secondary one.

        Args:
            t: The type key to look up.

        Returns:
            The stored value, or None.
        """
        if t in self.primary:
            return self.primary[t]
        return self.secondary.get(t)

    def require(self, t: type[T]) -> T:
        """Return the value for type t; raise MissingDependencyError if absent.

        Args:
            t: The type key to look up.

        Returns:
            The stored value (never None).

        Raises:
            MissingDependencyError: When t is not found in primary or secondary.
        """
        v = self.get(t)
        if v is None:
            raise MissingDependencyError(t)
        return v


class DagNode(ABC):
    """Abstract base for all DAG computation nodes.

    Subclass contract (class-level attributes):
      output_type:       type|None — type deposited into ctx.secondary
                                     (None = sink node).
      consumes:          list[type] — required types this node reads from
                                     NodeContext (both primary and secondary).
                                     Each gates readiness AND raises the tier.
      consumes_optional: list[type] — types the node reads via ctx.get but can
                                     run without. Each raises the tier (so the
                                     node still orders after its producer) but
                                     does NOT gate readiness. Defaults to [].

    A node's execution tier is not declared — DagEngine derives it from the
    consumes + consumes_optional / output_type graph so a node always runs
    strictly after every node whose output it consumes, required or optional.
    """

    output_type: type | None
    consumes: list[type]
    consumes_optional: list[type] = []

    def all_secondary_deps_satisfied(self, ctx: NodeContext) -> bool:
        """Return True when every secondary dependency declared in consumes is available.

        Readiness is derived purely from ctx.secondary: because a node may only
        consume strictly-lower-tier outputs and each tier runs to completion
        before the next begins, every secondary dependency it declares is
        already present in ctx.secondary by the time its own tier is evaluated.
        There is no per-node "satisfied" state, so nothing can leak across cycles.

        Args:
            ctx: Current DAG run context.

        Returns:
            True if every non-primary consumed type is present in ctx.secondary.
        """
        return all(
            t in ctx.secondary
            for t in self.consumes
            if t not in ctx.primary
        )

    # ── Execution ────────────────────────────────────────────────────────────

    async def run(self, ctx: NodeContext) -> None:
        """Execute this node: compute and deposit the result into ctx.secondary.

        Args:
            ctx: Shared run context; result is deposited into ctx.secondary.

        Raises:
            Exception: Anything raised by _compute propagates so the engine can
                isolate the failure. The node simply deposits nothing, so its
                downstream consumers stay unready (absent from ctx.secondary)
                and degrade gracefully — there is no cross-cycle state to reset.
        """
        result = await self._compute(ctx)
        if result is not None and self.output_type is not None:
            ctx.secondary[self.output_type] = result

    @abstractmethod
    async def _compute(self, ctx: NodeContext) -> Any | None:
        """Implement node-specific computation logic.

        Args:
            ctx: Shared run context with all upstream outputs available.

        Returns:
            The node's result, deposited as output_type in ctx.secondary
            (ignored when output_type is None).
        """


class DagEngine:
    """Tiered DAG executor with tiers derived from the dependency graph.

    Construction:
      DagEngine(nodes) — each node's tier is derived from its consumes/output_type
      relationships and the nodes are grouped by tier.

    Execution:
      await engine.run(primary, config, now) → secondary

    Tiers execute in ascending order; within a tier all ready nodes run in parallel.
    """

    def __init__(self, nodes: list[DagNode]) -> None:
        """Derive each node's execution tier and group the nodes by tier.

        Args:
            nodes: All DAG nodes; each must set output_type (or None) and consumes.

        Raises:
            DependencyCycleError: When the consumes/output_type graph has a cycle.
        """
        tier_of = self._assign_tiers(nodes)
        self._nodes_by_tier: dict[int, list[DagNode]] = {}
        for node in nodes:
            self._nodes_by_tier.setdefault(tier_of[node], []).append(node)

    @staticmethod
    def _assign_tiers(nodes: list[DagNode]) -> dict[DagNode, int]:
        """Derive each node's execution tier by longest-path layering over the graph.

        A node's tier is one more than the deepest tier among the nodes that
        produce the secondary types it consumes — required (consumes) or
        optional (consumes_optional) alike (0 when it consumes only primary
        inputs). Optional edges raise the tier exactly like required ones, so a
        node that reads an optional input via ctx.get still lands strictly above
        its producer; the optional/required distinction only matters later, for
        readiness (see DagNode.all_secondary_deps_satisfied). Because every
        producer→consumer edge raises the tier by at least one, same-tier nodes
        are guaranteed independent and every dependency is resolved before its
        consumer's tier runs.

        Args:
            nodes: All nodes; each node's output_type, consumes, and
                consumes_optional are inspected.

        Returns:
            Mapping of node → derived tier (0-based).

        Raises:
            DependencyCycleError: When a node transitively consumes its own output
                (through a required or optional edge), so no finite layering exists.
        """
        producer: dict[type, DagNode] = {
            n.output_type: n for n in nodes if n.output_type is not None
        }
        tier: dict[DagNode, int] = {}
        resolving: set[DagNode] = set()

        def resolve(node: DagNode, trail: tuple[DagNode, ...]) -> int:
            """Return node's tier, recursing into its producers; trail detects cycles."""
            if node in tier:
                return tier[node]
            if node in resolving:
                chain = " → ".join(type(n).__name__ for n in (*trail, node))
                raise DependencyCycleError(f"Dependency cycle: {chain}")
            resolving.add(node)
            dep_producers = [
                producer[t]
                for t in (*node.consumes, *node.consumes_optional)
                if t in producer
            ]
            node_tier = 1 + max(
                (resolve(p, (*trail, node)) for p in dep_producers), default=-1
            )
            resolving.discard(node)
            tier[node] = node_tier
            return node_tier

        for n in nodes:
            resolve(n, ())
        return tier

    async def run(
        self,
        primary: dict[type, Any],
        config: SunSaleConfig,
        now: datetime,
    ) -> dict[type, Any]:
        """Execute all tiers in ascending order; within each tier run ready nodes in parallel.

        Args:
            primary: Translation-layer outputs (HA state → domain types).
            config: Structured user configuration, passed through to all nodes.
            now: Cycle timestamp.

        Returns:
            The secondary-outputs dict (one entry per node that produced a
            result). A node that raises is isolated: its output is omitted and
            downstream consumers that depend on it simply never become ready,
            degrading the same way they would for a missing primary input.
        """
        ctx = NodeContext(primary=primary, secondary={}, config=config, now=now)
        skipped: list[tuple[DagNode, list[type]]] = []

        for tier_num in sorted(self._nodes_by_tier):
            ready: list[DagNode] = []
            for node in self._nodes_by_tier[tier_num]:
                if node.all_secondary_deps_satisfied(ctx):
                    ready.append(node)
                else:
                    skipped.append((node, self._missing_deps(node, ctx)))
            if not ready:
                continue
            # return_exceptions so one flaky node can't abort the whole tier
            # (and with it the cycle). A failed node deposits nothing, so its
            # consumers find the dependency absent in ctx.secondary and skip.
            results = await asyncio.gather(
                *[n.run(ctx) for n in ready], return_exceptions=True
            )
            for node, result in zip(ready, results):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    _LOGGER.warning(
                        "DAG node %s (tier %d) failed; output omitted, "
                        "downstream consumers degrade this cycle: %s",
                        type(node).__name__, tier_num, result,
                    )

        if skipped and _LOGGER.isEnabledFor(logging.DEBUG):
            # One line per cycle naming every node that never ran and the
            # consumed types it was waiting on — so an empty sensor traces back
            # to its missing upstream from the log alone, without the debug
            # endpoint. A skip is routine (an optional source absent this
            # cycle), hence DEBUG not WARNING.
            detail = "; ".join(
                f"{type(node).__name__}(missing "
                f"{', '.join(getattr(t, '__name__', str(t)) for t in missing)})"
                for node, missing in skipped
            )
            _LOGGER.debug("DAG skipped %d node(s) this cycle: %s", len(skipped), detail)

        return ctx.secondary

    @staticmethod
    def _missing_deps(node: DagNode, ctx: NodeContext) -> list[type]:
        """Return the consumed types absent from both primary and secondary context.

        These are exactly the dependencies that kept ``node`` from running this
        cycle — a missing primary input (translator returned None) or an
        upstream secondary output that was itself skipped or failed.

        Args:
            node: The skipped node.
            ctx: Current run context.

        Returns:
            The unsatisfied consumed types, in the node's declared order.
        """
        return [
            t for t in node.consumes
            if t not in ctx.primary and t not in ctx.secondary
        ]


async def run_translators(
    translators: list,
    hass: Any,
    config: SunSaleConfig,
    raw_config: dict,
    now: datetime,
) -> dict[type, Any]:
    """Run all translators concurrently and collect their outputs into a primary dict.

    Translators that return None (optional / unavailable sources) are silently
    skipped. A translator that *raises* is isolated: the failure is logged and
    its output is omitted from the primary dict, so one flaky source can't abort
    the whole cycle. The DAG degrades gracefully for the missing input, and the
    control-module dispatch downstream still runs.

    Args:
        translators: List of translator objects exposing output_type and translate().
        hass: Home Assistant instance passed through to each translator.
        config: Structured SunSale configuration.
        raw_config: Raw config-entry dict forwarded to translators that need it.
        now: Cycle timestamp.

    Returns:
        Dict mapping each surviving translator's output_type to its result.

    Raises:
        asyncio.CancelledError: Re-raised if a translator task was cancelled
            (genuine cancellation must not be swallowed as a per-source failure).
    """
    results = await asyncio.gather(
        *[t.translate(hass, config, raw_config, now) for t in translators],
        return_exceptions=True,
    )
    primary: dict[type, Any] = {}
    for t, result in zip(translators, results):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            _LOGGER.warning(
                "Translator %s failed; omitting %s from primary inputs this "
                "cycle: %s",
                type(t).__name__,
                getattr(t.output_type, "__name__", t.output_type),
                result,
            )
            continue
        if result is not None:
            primary[t.output_type] = result
    return primary
