#!/usr/bin/env python3
"""Build JUnit 5's pure-Java platform modules with brix.

A real-world acceptance test: re-implement a famous Gradle project's compile
graph in brix, so the multi-module ABI story (`identity="jvm-abi"`) and the
warm javac worker run against code we didn't write. Scope is the seven
pure-Java `junit-platform-*` modules — the `jupiter-*` side pulls in Kotlin
and code generation, `console`/`reporting` pull in extra jars and
multi-release/shadow packaging, so both are out of this first cut.

Each module compiles `src/main/java` to a jar through one shared warm javac
worker; its classpath is the transitive closure of the jars it depends on
(plus the two external annotation/assertion jars), every entry tagged
`jvm-abi` by `JarAction`. So an implementation-only edit in `commons`
replays its dependents (no recompile); an API edit cascades.

Setup (once):
    examples/junit5/fetch-deps.sh          # clones aren't committed; this
                                           # re-fetches the 2 external jars
Run (from anywhere):
    python examples/junit5/brix.py build           # all seven jars
    python examples/junit5/brix.py build engine    # one module + its deps
    python examples/junit5/brix.py status
    python examples/junit5/brix.py --no-daemon build

The JUnit checkout defaults to ~/Projects/brix_test/junit5 (override with
BRIX_JUNIT5_ROOT). This file uses the brix in this repo, not an installed
one — it puts ../../src on sys.path.
"""

import os
import sys
from pathlib import Path

# This file is named brix.py, so its own directory would shadow the brix
# package on import — drop it, then point at this repo's src/.
HERE = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, java  # noqa: E402

ROOT = Path(
    os.environ.get("BRIX_JUNIT5_ROOT", os.path.expanduser("~/Projects/brix_test/junit5"))
).resolve()

# Inter-module dependencies (the `api`/`implementation` project() edges from
# each module's *.gradle.kts). Order here is only for readability; brix infers
# build order from the jar/classpath data overlap.
DEPS: dict[str, list[str]] = {
    "junit-platform-commons": [],
    "junit-platform-engine": ["junit-platform-commons"],
    "junit-platform-suite-api": ["junit-platform-commons"],
    "junit-platform-launcher": ["junit-platform-engine"],
    "junit-platform-testkit": ["junit-platform-launcher"],
    "junit-platform-suite-commons": [
        "junit-platform-launcher",
        "junit-platform-engine",
        "junit-platform-suite-api",
    ],
    "junit-platform-suite-engine": [
        "junit-platform-engine",
        "junit-platform-suite-api",
        "junit-platform-suite-commons",
    ],
}

# External compile dependencies (Maven Central; see fetch-deps.sh). These two
# are needed everywhere and are tiny; putting both on every module's classpath
# is harmless (javac ignores unused entries) and spares tracking which needs
# which — the exercise is about the *inter-module* ABI edges.
EXTERNAL = [
    "lib/apiguardian-api-1.1.2.jar",
    "lib/opentest4j-1.3.0.jar",
]

# Per-module extra external jars. testkit exposes AssertJ in its public API
# (api(libs.assertj)), and assertj-core needs byte-buddy at compile scope.
# Kept off the shared list so only testkit content-hashes these — a change to
# assertj must not re-stale the other six modules.
EXTERNAL_EXTRA: dict[str, list[str]] = {
    "junit-platform-testkit": [
        "lib/assertj-core-3.27.3.jar",
        "lib/byte-buddy-1.15.11.jar",
    ],
}


def transitive(module: str, seen: set[str] | None = None) -> set[str]:
    """Every module `module` depends on, directly or transitively — javac's
    classpath is not transitive, so each must be listed explicitly."""
    seen = set() if seen is None else seen
    for dep in DEPS[module]:
        if dep not in seen:
            seen.add(dep)
            transitive(dep, seen)
    return seen


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    # One warm javac worker, bootstrapped once and shared by every module.
    worker = java.javac_worker(graph)

    for module in DEPS:
        classpath = [data.file(jar) for jar in EXTERNAL + EXTERNAL_EXTRA.get(module, [])]
        classpath += [
            data.file(f"build/libs/{dep}.jar") for dep in sorted(transitive(module))
        ]
        graph.add(
            java.JarAction(
                source_files=data.glob(f"{module}/src/main/java/{{name}}.java"),
                output_file=data.file(f"build/libs/{module}.jar"),
                classes_dir=f"build/classes/{module}",
                classpath=classpath,
                javac_worker=worker,
                options=["--release", "17", "-encoding", "UTF-8"],
            )
        )
    return graph


def aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for module in DEPS:
        jar = f"build/libs/{module}.jar"
        out[module] = jar
        out[module.removeprefix("junit-platform-")] = jar  # `build engine`
    return out


if __name__ == "__main__":
    from brix import AtomicBuilder, CacheBuilder, IncrementalBuilder

    def builder():
        return AtomicBuilder(
            CacheBuilder(
                IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
                cache_dir="build/.brix-cache",
            ),
            staging_dir="build/.brix-staging",
        )

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            staging_dir="build/.brix-staging",
            builder_factory=builder,
            aliases=aliases(),
        )
    )
