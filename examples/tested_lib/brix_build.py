#!/usr/bin/env python3
"""A minimal, self-contained example of running tests under brix.

The point of this example is *not* the code — it is two trivial classes and
two tiny JUnit suites. The point is how brix treats a test suite: as a
**cacheable action**. Unlike Maven or Gradle, brix does not re-run a suite
just because you typed `build`. It runs a suite only when something the suite
actually reads has changed, and otherwise restores the previous *passing*
result from cache — the same way it restores a compiled jar.

Two lenses on the code under test make this sound (see JUnitTestAction):

  * the test *compile* depends on the code through its **ABI** — so an
    implementation-only edit does not recompile the tests;
  * the test *run* depends on the code through its **bytes** — so that same
    edit does re-run the suite, because behavior may have changed.

The two suites here are wired to two *separate* jars (adder.jar, greeter.jar).
That independence is the whole demonstration: editing Adder rebuilds adder.jar,
whose bytes are an input to the Adder suite, so that suite re-runs — while
greeter.jar is untouched, so the Greeter suite stays cached and runs zero tests.

    python brix_build.py build      # first run: compiles and runs both suites
    python brix_build.py build      # both suites CACHED — nothing runs
    # edit src/com/acme/Adder.java
    python brix_build.py build      # only the Adder suite re-runs

Requires a `junit-platform-console-standalone` jar (the runner). Point
BRIX_JUNIT_CONSOLE_JAR at one, or drop it under ~/Projects/brix_test/*/lib/.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, java  # noqa: E402

ROOT = HERE


def _console_jar() -> str:
    """The one external input: the JUnit console launcher jar."""
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

    # Two independent library jars. Each test suite depends on exactly one.
    adder_jar = "build/libs/adder.jar"
    greeter_jar = "build/libs/greeter.jar"

    graph.add(
        java.JarAction(
            source_files=d.file("src/com/acme/Adder.java"),
            output_file=d.file(adder_jar),
            classes_dir="build/classes/adder",
            javac_worker=worker,
            options=OPTIONS,
        )
    )
    graph.add(
        java.JarAction(
            source_files=d.file("src/com/acme/Greeter.java"),
            output_file=d.file(greeter_jar),
            classes_dir="build/classes/greeter",
            javac_worker=worker,
            options=OPTIONS,
        )
    )

    # One suite per jar. This is the whole authoring surface a user needs:
    #   - test_sources: the .java test files
    #   - classpath:    the code under test (byte lens at run time)
    #   - console_jar:  the runner (and the Jupiter API the tests compile against)
    suites = (
        ("adder tests", "test/com/acme/AdderTest.java", adder_jar),
        ("greeter tests", "test/com/acme/GreeterTest.java", greeter_jar),
    )
    for name, source, jar in suites:
        graph.add(
            java.JUnitTestAction(
                name=name,
                test_sources=d.file(source),
                classpath=[d.file(jar)],
                console_jar=CONSOLE,
                test_classes_dir=f"build/test-classes/{name.split()[0]}",
                javac_worker=worker,
                options=OPTIONS,
            )
        )
    return graph


def builder():
    # The JUnit runner reads real jar paths, so outputs are written in place.
    return CacheBuilder(
        IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
        cache_dir="build/.brix-cache",
    )


if __name__ == "__main__":
    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            builder_factory=builder,
        )
    )
