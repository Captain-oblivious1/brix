"""Brix: a build-system toolkit.

Define build graphs in ordinary Python; the engine plans and executes them.
This is the minimal prototype: data expressions, generic process actions,
runtime ExpandAll expansion, and a clean builder.
"""

from .actions import DeleteFilesAction, ExpandAll, ExternalProcessAction
from .builders import BuildResult, CleanBuilder, IncrementalBuilder
from .data import DataExpr, DataFactory, DataUnion
from .graph import Graph
from .incremental import IncrementalLayer
from .layers import BuildLayer, NodeContext, NodeProposal
from .metadata import BuildMetadata, TimeAndHashChangeDetector
from .plan import (
    EdgeTrigger,
    ExecutionEdge,
    ExecutionNode,
    ExecutionPlan,
    NodeKind,
    NodeState,
    NodeStateTable,
)
from .process import ProcessResult
from .report import ConsoleReporter, ErrorsAtEndReporter, Reporter
from .status import ActionStatus, StatusReport, compute_status
from . import cli

__all__ = [
    "ActionStatus",
    "BuildLayer",
    "BuildMetadata",
    "BuildResult",
    "CleanBuilder",
    "ConsoleReporter",
    "DataExpr",
    "DataFactory",
    "DataUnion",
    "DeleteFilesAction",
    "EdgeTrigger",
    "ErrorsAtEndReporter",
    "ExecutionEdge",
    "ExecutionNode",
    "ExecutionPlan",
    "ExpandAll",
    "ExternalProcessAction",
    "Graph",
    "IncrementalBuilder",
    "IncrementalLayer",
    "NodeContext",
    "NodeKind",
    "NodeProposal",
    "NodeState",
    "NodeStateTable",
    "ProcessResult",
    "Reporter",
    "StatusReport",
    "TimeAndHashChangeDetector",
    "cli",
    "compute_status",
]
