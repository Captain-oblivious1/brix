"""Brix: a build-system toolkit.

Define build graphs in ordinary Python; the engine plans and executes them.
This is the minimal prototype: data expressions, generic process actions,
runtime ExpandAll expansion, and a clean builder.
"""

from .actions import DeleteFilesAction, ExpandAll, Expandable, ExternalProcessAction
from .atomic import AtomicLayer
from .builders import AtomicBuilder, BuildResult, CacheBuilder, CleanBuilder, IncrementalBuilder
from .cache import CacheLayer, CacheStore, CacheStoreProtocol, TwoTierCacheStore
from .cache_http import HttpCacheStore
from .data import DataExpr, DataFactory, DataUnion
from .deps import DepSet
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
from .workers import WorkerSpec
from . import cli, cpp, identity, java, jvmabi, springboot, ts

__all__ = [
    "ActionStatus",
    "AtomicBuilder",
    "AtomicLayer",
    "BuildLayer",
    "BuildMetadata",
    "BuildResult",
    "CacheBuilder",
    "CacheLayer",
    "CacheStore",
    "CacheStoreProtocol",
    "HttpCacheStore",
    "CleanBuilder",
    "DepSet",
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
    "Expandable",
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
    "TwoTierCacheStore",
    "WorkerSpec",
    "cli",
    "compute_status",
    "cpp",
    "identity",
    "java",
    "jvmabi",
    "springboot",
    "ts",
]
