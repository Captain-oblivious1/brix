"""Brix's supported Python authoring and extension API.

Engine implementation types live in their defining modules and are not part of
the top-level v1 contract. See ``docs/python-api.md``.
"""

from .actions import ExpandAll, Expandable, ExternalProcessAction
from .builders import AtomicBuilder, BuildResult, CacheBuilder, CleanBuilder, IncrementalBuilder
from .cache import CacheStore, CacheStoreProtocol, TwoTierCacheStore
from .cache_http import HttpCacheStore
from .data import DataExpr
from .deps import DepSet
from .graph import Graph
from .layers import BuildLayer, NodeContext, NodeProposal
from .plan import EdgeTrigger
from .process import ProcessResult
from .report import ConsoleReporter, ErrorsAtEndReporter, Reporter
from .status import ActionStatus, StatusReport, compute_status
from .workers import WorkerSpec
from . import cli, cpp, identity, java, kotlin, springboot, ts

__all__ = [
    "ActionStatus",
    "AtomicBuilder",
    "BuildLayer",
    "BuildResult",
    "CacheBuilder",
    "CacheStore",
    "CacheStoreProtocol",
    "CleanBuilder",
    "ConsoleReporter",
    "DataExpr",
    "DepSet",
    "EdgeTrigger",
    "ErrorsAtEndReporter",
    "ExpandAll",
    "Expandable",
    "ExternalProcessAction",
    "Graph",
    "HttpCacheStore",
    "IncrementalBuilder",
    "NodeContext",
    "NodeProposal",
    "ProcessResult",
    "Reporter",
    "StatusReport",
    "TwoTierCacheStore",
    "WorkerSpec",
    "cli",
    "compute_status",
    "cpp",
    "identity",
    "java",
    "kotlin",
    "springboot",
    "ts",
]
