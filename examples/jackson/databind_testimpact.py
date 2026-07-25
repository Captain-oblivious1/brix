#!/usr/bin/env python3
"""The real jackson-databind test suite under brix, with per-class impact
selection **by runtime discovery**.

The large-scale companion to ``examples/tested_lib``. The suite is Jackson's
*actual* ``src/test/java`` tree — ~700 test files, ~3,400 executed tests — and
each test class runs as its own action against databind's exploded classes.
brix records which ``.class`` files each test actually loads (``-Xlog:class+load``
→ a depfile) and re-runs a test only when a class it loaded changes.

This is the same mechanism as ``tested_lib``, and it is the headline result of
the whole test-impact feature: **databind is the case where static class-level
selection skipped ~0%** (everything is statically reachable through a reflective
``ObjectMapper``), yet runtime discovery selects **~1.5%** — editing one leaf
class re-runs ~11 of ~700 tests in seconds, because discovery sees the *narrow
set a test really loads*, not the conservative static closure.

    python databind_testimpact.py build   # cold: ~700 test classes, ~1 min (24-wide parallel)
    python databind_testimpact.py build   # CACHED: nothing runs
    # edit one databind main source (a real bytecode change)
    python databind_testimpact.py build   # re-runs only the tests that loaded it

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

ROOT = jackson.ROOT
DATABIND = jackson.DATABIND
CONSOLE = jackson.CONSOLE
FASTDOUBLE = jackson.FASTDOUBLE
DATABIND_CLASSES = "build/classes/databind"
OPTIONS = ["--release", "17", "-encoding", "UTF-8"]
METADATA_DIR = "build/.brix"

MAIN_ROOT = f"{DATABIND}/src/main/java"
TEST_ROOT = f"{DATABIND}/src/test/java"
TEST_GLOB = f"{TEST_ROOT}/{{name}}.java"
TEST_CLASSES = "build/test-classes/databind-real"

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


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)

    jars = jackson.build_module_jars(graph, worker)
    # The code under test: databind's own classes as an EXPLODED dir (so a
    # single class's change is visible on its own → per-class selection), plus
    # core/annotations/fastdouble as jars (third-party to these tests → whole-jar,
    # coarse but sound).
    classpath = [
        d.file(DATABIND_CLASSES),
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
