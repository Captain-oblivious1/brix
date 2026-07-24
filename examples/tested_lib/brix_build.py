#!/usr/bin/env python3
"""Running tests under brix, with per-test-class impact selection.

Two trivial classes (Adder, Greeter) in one library jar, and one JUnit suite
holding a test for each. The interesting part is what happens on a rebuild:

  * brix treats the suite as a **cacheable action** — an unchanged tree re-runs
    zero tests (the run replays from cache, like any output).
  * when something *does* change, brix runs only the **test classes the change
    could reach**. Editing Adder re-runs AdderTest; GreeterTest is provably
    unaffected (its only dependency, Greeter, did not change) and is skipped.

Selection is decided here, at build-file evaluation time — the same place the
monorepo lazy-loader decides which packages to load. It reads what changed since
the last build (from brix's own file-hash cache — no VCS needed), asks the Java
provider which test classes that reaches, and hands the JUnit runner a
`--select-class` list. The suite's *fingerprint* is untouched (still the whole
classpath), so any change that could matter still re-fires the suite — selection
only ever narrows which classes execute, never what is watched for staleness.

    python brix_build.py build      # first run: compiles and runs both tests
    python brix_build.py build      # CACHED — nothing runs
    # edit src/com/acme/Adder.java
    python brix_build.py build      # runs AdderTest only; GreeterTest skipped

Requires a `junit-platform-console-standalone` jar. Point
BRIX_JUNIT_CONSOLE_JAR at one, or drop it under ~/Projects/brix_test/*/lib/.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, java  # noqa: E402
from brix import testimpact_java as ti  # noqa: E402
from brix.metadata import BuildMetadata  # noqa: E402

ROOT = HERE
METADATA_DIR = "build/.brix"
LIB_JAR = "build/libs/lib.jar"
TEST_CLASSES = "build/test-classes/lib"
SUITE_INDEX = "build/.brix/testimpact/lib.json"

# Where source lives, and every source file, so the change detector can diff
# them against the last build. Two roots: main code and test code.
SOURCE_ROOTS = ["src", "test"]
SOURCES = [
    "src/com/acme/Adder.java",
    "src/com/acme/Greeter.java",
    "test/com/acme/AdderTest.java",
    "test/com/acme/GreeterTest.java",
]


def _console_jar() -> str:
    env = os.environ.get("BRIX_JUNIT_CONSOLE_JAR")
    if env and Path(env).is_file():
        return env
    found = sorted(
        Path.home().glob("Projects/brix_test/*/lib/junit-platform-console-standalone*.jar")
    )
    if found:
        return str(found[-1])
    sys.exit(
        "No junit-platform-console-standalone jar found.\n"
        "Set BRIX_JUNIT_CONSOLE_JAR=/path/to/junit-platform-console-standalone-*.jar"
    )


CONSOLE = _console_jar()
OPTIONS = ["--release", "17", "-encoding", "UTF-8"]


def _select() -> str:
    """Decide which test classes this build needs to run, as JUnit console
    flags. Reads the last build's compiled artifacts (all that exists yet) and
    what changed since, and fails open to a full scan whenever proof runs out."""
    meta = BuildMetadata(ROOT, METADATA_DIR)
    meta.load()
    provider = ti.JavaProvider(
        test_roots=[ROOT / TEST_CLASSES],
        classpath=[ROOT / LIB_JAR],
        harness_roots=[CONSOLE],
    )
    plan = ti.plan_suite(
        provider,
        index_path=ROOT / SUITE_INDEX,
        source_roots=SOURCE_ROOTS,
        changed_paths=ti.changed_since_last_build(meta, ROOT, SOURCES),
        prior_failed=meta.has_failed_actions(),
    )
    return " ".join(plan.select)


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)

    # One library jar holding both classes.
    graph.add(
        java.JarAction(
            source_files=d.glob("src/com/acme/{name}.java"),
            output_file=d.file(LIB_JAR),
            classes_dir="build/classes/lib",
            javac_worker=worker,
            options=OPTIONS,
        )
    )

    # One suite over both test classes. `select=` is the only new part: instead
    # of always scanning the whole classpath, run just the classes this build's
    # change set could reach.
    graph.add(
        java.JUnitTestAction(
            name="lib tests",
            test_sources=d.glob("test/com/acme/{name}.java"),
            classpath=[d.file(LIB_JAR)],
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
