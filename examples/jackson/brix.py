#!/usr/bin/env python3
"""Build and test jackson-core with brix — the selective-testing dogfood.

Exercises the whole Java story on a famous real project:
  - a generated source (PackageVersion.java from a template) feeding the compile
  - resources packaged into the jar (META-INF/services)
  - JUnit 5 + AssertJ tests, run so a suite re-runs only when its inputs change

    examples/jackson/fetch-deps.sh                 # test-framework jars (once)
    python examples/jackson/brix.py build          # jar + compile + run tests
    python examples/jackson/brix.py status

Edit a main source and rebuild: jackson-core recompiles and the suite re-runs
(the run classpath depends on the jar's bytes). Edit only a test and rebuild:
the main jar replays, only the tests recompile and re-run.

Checkout defaults to ~/Projects/brix_test/jackson-core (BRIX_JACKSON_ROOT).
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, java  # noqa: E402
from brix.actions import ExternalProcessAction  # noqa: E402
from brix.data import FileData, ToolData  # noqa: E402

ROOT = Path(
    os.environ.get("BRIX_JACKSON_ROOT", os.path.expanduser("~/Projects/brix_test/jackson-core"))
).resolve()

VERSION = "2.19.4"
CONSOLE = "lib/junit-platform-console-standalone-1.11.4.jar"
# jackson-core's one compile dep (shaded into the distributed jar, but a
# plain classpath entry for us): the fast double parser it calls into.
FASTDOUBLE = "lib/fastdoubleparser-2.0.1.jar"
TEST_DEPS = ["lib/assertj-core-3.27.3.jar", "lib/byte-buddy-1.15.11.jar"]
GEN_VERSION = str(HERE / "gen_version.py")
PKG_TEMPLATE = "src/main/java/com/fasterxml/jackson/core/json/PackageVersion.java.in"
PKG_OUTPUT = "build/generated/com/fasterxml/jackson/core/json/PackageVersion.java"


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)

    # 1. Generate PackageVersion.java from the template (the maven-replacer
    #    step in jackson's pom). Its output feeds the compile below.
    graph.add(
        ExternalProcessAction(
            name="generate PackageVersion",
            command_line=(
                "{python} {gen} {template} {out}"
                " package=com.fasterxml.jackson.core.json"
                f" projectversion={VERSION}"
                " projectgroupid=com.fasterxml.jackson.core"
                " projectartifactid=jackson-core"
            ),
            inputs={
                "python": d.tool(sys.executable),
                "gen": FileData(GEN_VERSION),
                "template": d.file(PKG_TEMPLATE),
            },
            outputs={"out": d.file(PKG_OUTPUT)},
        )
    )

    # 2. Compile main sources + the generated one into jackson-core.jar,
    #    packaging src/main/resources (META-INF/services registrations).
    graph.add(
        java.JarAction(
            source_files=(
                d.glob("src/main/java/{name}.java") + d.glob("build/generated/{name}.java")
            ),
            output_file=d.file("build/libs/jackson-core.jar"),
            classes_dir="build/classes",
            resource_dir="src/main/resources",
            classpath=[d.file(FASTDOUBLE)],  # the one compile dep
            javac_worker=worker,
            options=["--release", "17", "-encoding", "UTF-8"],
        )
    )

    # 3. Compile + run the JUnit 5 suite. classpath = jackson-core.jar + the
    #    test deps (AssertJ); the console jar supplies Jupiter. Two lenses:
    #    jvm-abi to compile the tests, bytes to run them.
    graph.add(
        java.JUnitTestAction(
            name="jackson-core tests",
            test_sources=d.glob("src/test/java/{name}.java"),
            classpath=(
                [d.file("build/libs/jackson-core.jar"), d.file(FASTDOUBLE)]
                + [d.file(j) for j in TEST_DEPS]
            ),
            console_jar=CONSOLE,
            test_resource_dirs=["src/test/resources"],
            javac_worker=worker,
            options=["--release", "17", "-encoding", "UTF-8"],
        )
    )
    return graph


if __name__ == "__main__":
    from brix import CacheBuilder, IncrementalBuilder

    def builder():
        # No atomic layer: the test runner reads the built jar by its real
        # path, so outputs go straight to the workspace.
        return CacheBuilder(
            IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
            cache_dir="build/.brix-cache",
        )

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            builder_factory=builder,
            aliases={"jar": "build/libs/jackson-core.jar"},
        )
    )
