#!/usr/bin/env python3
"""Two-module Kotlin build: why a Kotlin dependency is byte-tracked, not
ABI-tracked.

A self-contained teaching example (sources live here; no clone needed). Module
`app` depends on module `core`. brix infers the order from data overlap — app's
classpath input is core's output jar — and builds core, then app.

The point it teaches: `core` exports a *normal* function (`Greeter.greet`) and
an *inline* function (`repeated`). kotlinc copies an inline function's body into
its call sites, so editing `repeated`'s body in `core` changes `app`'s compiled
bytecode, while editing `greet`'s body does not. That is why `brix.kotlin`
byte-tracks Kotlin classpath entries instead of ABI-lensing them the way
`brix.java` safely can: an ABI lens would skip recompiling `app` after an
inline-body change that actually altered its output — a stale-result bug. The
automated proof of this soundness property is `tests/test_kotlin_multimodule.py`.

Whole-module kotlinc semantics mean any edit in a module recompiles that whole
module; a change in `core` then recompiles `app` too (byte-tracked). This is
conservative — `app` recompiles even after a normal-body change it did not need
— but always sound. A soundness-checked Kotlin ABI lens (treating inline bodies
as interface) that would recover the skip is future work; see docs/roadmap.md.

Like the TypeScript example, this build uses the plain `IncrementalBuilder`
stack (outputs written in place), not the atomic builder: kotlinc resolves
`-classpath build/core.jar` from the real filesystem itself, and atomic staging
would hide `core`'s freshly built jar under a staging directory kotlinc can't
see. That is the parked "staged-overlay visibility for foreign resolvers"
limitation (docs/roadmap.md), which cross-module compilers trip.

Setup: a `kotlinc` on PATH (or `BRIX_KOTLINC=/path/to/kotlinc`). kotlinc bundles
the Kotlin stdlib, so there are no external dependencies to resolve.

Run (from anywhere):
    python examples/kotlin_multimodule/brix.py build
    python examples/kotlin_multimodule/brix.py status
    # then edit core/src/lib.kt and rebuild to watch what re-runs.
"""

import os
import sys
from pathlib import Path

# This file is named brix.py, so its own directory would shadow the brix
# package on import — drop it, then point at this repo's src/.
HERE = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, kotlin  # noqa: E402

KOTLINC = os.environ.get("BRIX_KOTLINC", "kotlinc")


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        kotlin.JarAction(
            source_files=data.glob("core/src/{name}.kt"),
            output_file=data.file("build/core.jar"),
            kotlinc=data.tool(KOTLINC),
            classes_dir="build/core-classes",
            jvm_target="17",
        ),
        kotlin.JarAction(
            source_files=data.glob("app/src/{name}.kt"),
            output_file=data.file("build/app.jar"),
            kotlinc=data.tool(KOTLINC),
            classes_dir="build/app-classes",
            jvm_target="17",
            classpath=[data.file("build/core.jar")],  # byte-tracked; see module docstring
            entry_point="demo.app.MainKt",
        ),
    )
    return graph


if __name__ == "__main__":
    # Default builder: a plain IncrementalBuilder (outputs written in place, so
    # app's kotlinc resolves -classpath build/core.jar to core's real jar). See
    # the module docstring on why the atomic stack does not fit cross-module.
    sys.exit(
        cli.main(
            create_graph,
            root=HERE,
            metadata_dir="build/.brix",
            aliases={"core": "build/core.jar", "app": "build/app.jar"},
        )
    )
