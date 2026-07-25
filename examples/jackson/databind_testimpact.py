#!/usr/bin/env python3
"""The real jackson-databind test suite under brix, with per-class impact selection.

This is the large-scale companion to ``examples/tested_lib`` (which shows the
same mechanism on two toy classes). Here the suite is Jackson's *actual*
``src/test/java`` tree — 708 test files, ~3,400 executed tests — run against the
three real module jars built by the sibling ``brix_build.py``.

The move is identical to ``tested_lib``: one ``java.JUnitTestAction`` over the
whole test tree, but with ``select=`` decided at build-file eval time by
``testimpact_java.plan_suite``. It reads the last build's compiled test classes
(their real bytecode reference graph) plus what changed since (from brix's own
file-hash cache, no VCS), and hands the JUnit console a ``--select-class`` list
of just the test classes the change can reach. The suite's *fingerprint* stays
the whole classpath, so any change that could matter still re-fires the suite —
selection only narrows which classes execute, and fails open to a full scan
whenever proof runs out.

    python databind_testimpact.py build   # cold: compiles + runs the whole suite
    python databind_testimpact.py build   # CACHED: nothing runs
    # edit a databind main source
    python databind_testimpact.py build   # runs only the reachable test classes

``verify_databind_selection.py`` is the proof: it edits one databind class and
asserts the selected subset is small, contains exactly the reachable test
classes, and reaches the same verdict the full run gave those classes.

Requires the three sibling checkouts (see README) and the
``junit-platform-console-standalone`` jar shipped in ``jackson-core/lib``.
"""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

# Reuse the sibling build's real-module jar graph (annotations/core/databind)
# and its constants, so an edit to a databind source flows jar -> suite here.
_spec = importlib.util.spec_from_file_location("brix_jackson_example", HERE / "brix_build.py")
assert _spec and _spec.loader
jackson = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = jackson
_spec.loader.exec_module(jackson)

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, java  # noqa: E402
from brix import testimpact_java as ti  # noqa: E402
from brix.metadata import BuildMetadata  # noqa: E402

ROOT = jackson.ROOT
DATABIND = jackson.DATABIND
CONSOLE = jackson.CONSOLE
FASTDOUBLE = jackson.FASTDOUBLE
OPTIONS = ["--release", "17", "-encoding", "UTF-8"]
METADATA_DIR = "build/.brix"

MAIN_ROOT = f"{DATABIND}/src/main/java"
TEST_ROOT = f"{DATABIND}/src/test/java"
TEST_GLOB = f"{TEST_ROOT}/{{name}}.java"
TEST_CLASSES = "build/test-classes/databind-real"
SUITE_INDEX = "build/.brix/testimpact/databind-real.json"
SOURCE_ROOTS = [MAIN_ROOT, TEST_ROOT]

# The 14 test files that need test-only libraries we deliberately do not stage
# (assertj / guava / mockito / jol / junit4-vintage / javax.measure). Excluding
# them via the glob `-` operator keeps this dogfood focused on selection at real
# scale rather than recreating Jackson's whole Maven test-dependency closure.
# Basenames are unique across the tree, so a trailing-name pattern is exact.
EXCLUDE = [
    "*DelegatingCreatorImplicitNamesTest.java",
    "*TwoCreators4602Test.java",
    "*CustomDeserializers4225NullCacheTest.java",
    "*LazyIgnoralForNumbers3730Test.java",
    "*Java8Datetime4533Test.java",
    "*OptionalJava8Fallbacks4082Test.java",
    "*ExistingPropertyTest.java",
    "*MapperFootprintTest.java",
    "*AnyGetterNameConflictSerialization5342Test.java",
    "*ReaderForUpdating5281Test.java",
    "*TypeFactoryWithClassLoaderTest.java",
    "*CLHMTestlibTests.java",
    "*MapTestFactory.java",
    "*NoClassDefFoundWorkaroundTest.java",
]

# Six more classes whose tests fail against *this* JDK independent of brix
# (verified with a plain `javac` + `java -jar console` run of the same tree):
# JDK-version locale/timezone/fuzz behavior Jackson 2.19 predates. A suite with
# a permanently-failing test is pinned to `prior_failed`, which forces a full
# run every build and never lets selection engage — so a green baseline is a
# precondition for demonstrating selection, not a way to hide a brix bug.
EXCLUDE += [
    "*BiggerDataTest.java",
    "*ArrayDelegatorCreatorForCollectionTest.java",
    "*FloatDeserTest.java",
    "*LocaleDeserTest.java",
    "*MixinForCreators2795Test.java",
    "*ClassUtilTest.java",
]


def _all_sources() -> list[str]:
    """Every databind source (main + kept test), workspace-relative to ROOT, so
    the change detector can diff each against the last build's hash."""
    excluded = {p.lstrip("*") for p in EXCLUDE}
    out: list[str] = []
    for root in (MAIN_ROOT, TEST_ROOT):
        for path in sorted((ROOT / root).rglob("*.java")):
            if any(path.name == name for name in excluded):
                continue
            out.append(str(path.relative_to(ROOT)))
    return out


def make_provider() -> "ti.JavaProvider":
    """The Java provider over the last build's compiled test classes and the
    real module jars — the reference graph selection reads. Shared with
    ``characterize_databind_selection.py``."""
    jars = ["jackson-databind", "jackson-core", "jackson-annotations"]
    return ti.JavaProvider(
        test_roots=[ROOT / TEST_CLASSES],
        classpath=[ROOT / f"build/libs/{j}.jar" for j in jars] + [ROOT / FASTDOUBLE],
        harness_roots=[ROOT / CONSOLE],
    )


def _select() -> str:
    """Which test classes this build needs, as JUnit console flags. Reads the
    last build's compiled test classes + real module jars (the reference graph)
    and what changed since, and fails open to a full scan when proof runs out."""
    meta = BuildMetadata(ROOT, METADATA_DIR)
    meta.load()
    provider = make_provider()
    plan = ti.plan_suite(
        provider,
        index_path=ROOT / SUITE_INDEX,
        source_roots=SOURCE_ROOTS,
        changed_paths=ti.changed_since_last_build(meta, ROOT, _all_sources()),
        prior_failed=meta.has_failed_actions(),
    )
    return " ".join(plan.select)


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)

    jars = jackson.build_module_jars(graph, worker)
    classpath = [
        d.file(jars["databind"]),
        d.file(jars["core"]),
        d.file(jars["annotations"]),
        d.file(FASTDOUBLE),
    ]

    graph.add(
        java.JUnitTestAction(
            name="databind real tests",
            test_sources=d.glob(TEST_GLOB) - EXCLUDE,
            classpath=classpath,
            console_jar=CONSOLE,
            test_classes_dir=TEST_CLASSES,
            javac_worker=worker,
            options=OPTIONS,
            select=_select(),
        )
    )
    return graph


def builder():
    # The JUnit runner reads real jar paths, so outputs are written in place.
    return CacheBuilder(
        IncrementalBuilder(create_graph, root=ROOT, metadata_dir=METADATA_DIR),
        cache_dir="build/.brix-cache",
    )


if __name__ == "__main__":
    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir=METADATA_DIR,
            builder_factory=builder,
        )
    )
