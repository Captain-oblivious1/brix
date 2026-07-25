#!/usr/bin/env python3
"""Running tests under brix, with per-test-class impact selection by discovery.

Two trivial classes (Adder, Greeter) in one library, and a JUnit test for each.
The interesting part is what happens on a rebuild:

  * Each test class is its **own** action. brix runs it, records which `.class`
    files the JVM actually loaded (via `-Xlog:class+load`, written as a depfile),
    and folds those into the run's fingerprint — exactly how a C++ compile
    records the headers it read.
  * So editing `Adder` re-runs only `AdderTest` (which loaded `Adder`);
    `GreeterTest` never loaded `Adder`, so it **replays**. Per-class selection,
    done by the incremental engine — no eval-time analysis, no `--select-class`
    planning, no static closure. The runtime tells the truth, reflection included.

Selection needs no VCS and no separate index; it is just the normal
replay-if-unchanged / re-run-if-failed machinery applied per test class.

    python brix_build.py build      # first run: compiles and runs both tests
    python brix_build.py build      # CACHED — nothing runs
    # edit src/com/acme/Adder.java
    python brix_build.py build      # runs AdderTest only; GreeterTest replays

Note the classpath points at the compiler's **exploded classes dir**
(`build/classes/lib`), not the packaged jar — so a single class's change is
visible on its own and selection can be per-class.

Requires a `junit-platform-console-standalone` jar. Point
BRIX_JUNIT_CONSOLE_JAR at one, or drop it under ~/Projects/brix_test/*/lib/.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, java  # noqa: E402

ROOT = HERE
METADATA_DIR = "build/.brix"
LIB_JAR = "build/libs/lib.jar"
LIB_CLASSES = "build/classes/lib"
TEST_CLASSES = "build/test-classes/lib"


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


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)

    # One library jar holding both classes; it also leaves the exploded classes
    # under build/classes/lib, which the tests run against for per-class selection.
    graph.add(
        java.JarAction(
            source_files=d.glob("src/com/acme/{name}.java"),
            output_file=d.file(LIB_JAR),
            classes_dir=LIB_CLASSES,
            javac_worker=worker,
            options=OPTIONS,
        )
    )

    # One JUnitTestAction, but it fans out to one run per test class. classpath
    # is the *exploded* code under test (not the jar), so editing one class is
    # visible on its own.
    graph.add(
        java.JUnitTestAction(
            name="lib tests",
            test_sources=d.glob("test/com/acme/{name}.java"),
            classpath=[d.file(LIB_CLASSES)],
            console_jar=CONSOLE,
            test_classes_dir=TEST_CLASSES,
            javac_worker=worker,
            options=OPTIONS,
        )
    )
    return graph


def builder():
    # The JUnit runner reads real classpath paths, so outputs are written in place.
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
