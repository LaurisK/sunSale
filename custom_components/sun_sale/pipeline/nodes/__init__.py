"""DAG nodes — the computation tier of the sunSale pipeline.

Pure Python — no Home Assistant imports.
Each node declares only its output_type and consumed types. Execution tiers are
derived by DagEngine from the consumes/output_type graph (longest-path layering),
so nodes never hand-assign a tier — adding a node is a non-decision.

The ``tierN.py`` filenames are an editing convenience that happens to mirror the
derived layering today (T1 consumes only primary data, T2 consumes T1 outputs,
and so on); they carry no semantic weight, and a node placed in the "wrong" file
would still run at its correct derived tier.
"""
from .tier1 import (
    BaseLoadProfileNode,
    BatteryStateNode,
    BatteryStatusNode,
    PricingNode,
)
from .tier2 import (
    BatteryRuntimeNode,
    DegradationNode,
    GenerationNode,
    ObservedConsumptionNode,
    ObservedGenerationNode,
    ObservedGridNode,
    ObservedLossesNode,
    ProfitabilityNode,
)
from .tier3 import (
    ForecastAccuracyNode,
    LockoutNode,
    MonthlyBillNode,
)
from .tier4 import ScheduleNode

__all__ = [
    "BaseLoadProfileNode",
    "BatteryRuntimeNode",
    "BatteryStateNode",
    "BatteryStatusNode",
    "DegradationNode",
    "ForecastAccuracyNode",
    "GenerationNode",
    "LockoutNode",
    "MonthlyBillNode",
    "ObservedConsumptionNode",
    "ObservedGenerationNode",
    "ObservedGridNode",
    "ObservedLossesNode",
    "PricingNode",
    "ProfitabilityNode",
    "ScheduleNode",
]
