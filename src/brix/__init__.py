"""Brix's supported Python authoring and extension API.

Engine implementation types live in their defining modules and are not part of
the top-level v1 contract. See ``docs/python-api.md``.
"""

from .actions import ExpandAll, Expandable, ExternalProcessAction
from .builders import (
    AtomicBuilder,
    BuildResult,
    CacheBuilder,
    CleanBuilder,
    IncrementalBuilder,
    NestableBuilder,
    build_stack,
)
from .cache import CacheStore, CacheStoreProtocol, TwoTierCacheStore
from .cache_http import HttpCacheStore
from .data import DataExpr
from .deps import DepSet
from .gitdetect import GitChangeDetector
from .graph import Graph
from .layers import BuildLayer, NodeContext, NodeProposal
from .plan import EdgeTrigger
from .affected import load_affected
from .workspace import Workspace, load_workspace
from .process import ProcessResult
from .report import ConsoleReporter, ErrorsAtEndReporter, Reporter
from .status import ActionStatus, StatusReport, compute_status
from .workers import WorkerSpec
from .env import SYSTEM, UNSET
from . import cli, cpp, identity, java, kotlin, rust, springboot, ts

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
    "GitChangeDetector",
    "Graph",
    "HttpCacheStore",
    "IncrementalBuilder",
    "NestableBuilder",
    "NodeContext",
    "NodeProposal",
    "ProcessResult",
    "Reporter",
    "StatusReport",
    "SYSTEM",
    "TwoTierCacheStore",
    "UNSET",
    "WorkerSpec",
    "Workspace",
    "build_stack",
    "load_affected",
    "load_workspace",
    "cli",
    "compute_status",
    "cpp",
    "identity",
    "java",
    "kotlin",
    "rust",
    "springboot",
    "ts",
]
