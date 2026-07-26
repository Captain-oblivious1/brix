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
    "cli",
    "load_affected",
    "load_workspace",
    "compute_status",
    "cpp",
    "identity",
    "java",
    "kotlin",
    "python",
    "rust",
    "springboot",
    "ts",
}


def test_top_level_exports_are_the_supported_v1_contract():
    assert set(brix.__all__) == EXPECTED
    assert all(hasattr(brix, name) for name in EXPECTED)


# Each public authoring submodule declares a curated `__all__` — the documented
# surface (autogen renders exactly these) and a reviewed contract. Internal
# helpers stay out; adding a genuinely public name means updating this set.
MODULE_EXPORTS = {
    "cpp": {"CompileSourceAction", "LinkApplicationAction", "ArchiveAction",
            "RunTestAction", "DoxygenAction", "pkg_config"},
    "java": {"JarAction", "JUnitTestAction", "JavadocAction", "javac_worker",
             "abi", "AbiFile", "AbiGlob", "pom", "maven", "gradle"},
    "kotlin": {"JarAction", "kotlinc_worker", "abi", "KotlinAbiFile", "KotlinAbiGlob"},
    "python": {"PytestAction"},
    "rust": {"CargoBuildAction", "cargo"},
    "ts": {"CompileAction", "RunTestAction", "TypedocAction", "npm", "dts", "DtsFile", "DtsGlob"},
    "springboot": {"SpringBootJar", "boot_repackager", "RepackagerSpec"},
    "cli": {"main", "Alias"},
    "data": {"DataExpr", "DataAtom", "FileData", "GlobData", "RegexData",
             "ToolData", "ValueData", "DataUnion", "DataFactory"},
    "identity": {"ContentIdentity", "register"},
}


def test_public_submodules_expose_their_curated_surface():
    import importlib

    for name, expected in MODULE_EXPORTS.items():
        module = importlib.import_module(f"brix.{name}")
        assert set(module.__all__) == expected, name
        # No private/underscore names leak into the documented surface, and
        # every listed name actually resolves.
        assert all(not n.startswith("_") for n in module.__all__), name
        assert all(hasattr(module, n) for n in module.__all__), name


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
