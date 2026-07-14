"""The supported top-level v1 Python API stays deliberate and small."""

import brix


EXPECTED = {
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
    "springboot",
    "ts",
}


def test_top_level_exports_are_the_supported_v1_contract():
    assert set(brix.__all__) == EXPECTED
    assert all(hasattr(brix, name) for name in EXPECTED)


def test_engine_internals_are_not_top_level_attributes():
    internal = {
        "AtomicLayer",
        "BuildMetadata",
        "CacheLayer",
        "DataFactory",
        "DataUnion",
        "DeleteFilesAction",
        "ExecutionEdge",
        "ExecutionNode",
        "ExecutionPlan",
        "IncrementalLayer",
        "NodeKind",
        "NodeState",
        "NodeStateTable",
        "TimeAndHashChangeDetector",
    }
    assert not internal.intersection(vars(brix))
