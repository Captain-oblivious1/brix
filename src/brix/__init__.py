"""Brix: a build-system toolkit.

Define build graphs in ordinary Python; the engine plans and executes them.
This is the minimal prototype: data expressions, generic process actions,
runtime ExpandAll expansion, and a clean builder.
"""

from .actions import ExpandAll, ExternalProcessAction
from .builders import BuildResult, CleanBuilder
from .data import DataExpr, DataFactory, DataUnion
from .graph import Graph
from .layers import BuildLayer, NodeContext
from .plan import (
    ExecutionEdge,
    ExecutionNode,
    ExecutionPlan,
    NodeKind,
    NodeState,
    NodeStateTable,
)
from .process import ProcessResult

__all__ = [
    "BuildLayer",
    "BuildResult",
    "CleanBuilder",
    "DataExpr",
    "DataFactory",
    "DataUnion",
    "ExecutionEdge",
    "ExecutionNode",
    "ExecutionPlan",
    "ExpandAll",
    "ExternalProcessAction",
    "Graph",
    "NodeContext",
    "NodeKind",
    "NodeState",
    "NodeStateTable",
    "ProcessResult",
]
