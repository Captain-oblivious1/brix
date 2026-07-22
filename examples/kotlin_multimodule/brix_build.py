#!/usr/bin/env python3
"""Two-module Kotlin build: the kotlin-abi lens, live.

A self-contained teaching example (sources live here; no clone needed). Module
`app` depends on module `core`. brix infers the order from data overlap — app's
classpath input is core's output jar — and builds core, then app.

The point it teaches: `core` exports a *normal* function (`Greeter.greet`) and
an *inline* function (`repeated`). kotlinc copies an inline function's body into
its call sites, so editing `repeated`'s body in `core` changes `app`'s compiled
bytecode, while editing `greet`'s body does not. Plain jvm-abi lensing (what
`brix.java` uses) is therefore unsound for a Kotlin dependency — it would skip
recompiling `app` after an inline change that altered its output, a
stale-result bug. `brix.kotlin` tracks classpath entries through the
**kotlin-abi** lens instead: classes with inline members count by bytes, the
rest by ABI. Edit `greet`'s body and rebuild — core recompiles, `app` replays
(the recovered cross-module skip). Edit `repeated`'s body — or even just add a
comment line above it, which shifts the line numbers its inlined copies carry —
and `app` recompiles. The automated proof of both directions is
`tests/test_kotlin_multimodule.py`; the lens itself is `brix/kotlinabi.py`.

Like the TypeScript example, this build uses the plain `IncrementalBuilder`
stack (outputs written in place), not the atomic builder: kotlinc resolves
`-classpath build/core.jar` from the real filesystem itself, and atomic staging
would hide `core`'s freshly built jar under a staging directory kotlinc can't
see. That is the parked "staged-overlay visibility for foreign resolvers"
limitation (docs/roadmap.md), which cross-module compilers trip.

It also compiles through a **warm kotlinc worker** (`kotlin.kotlinc_worker`): a
cold kotlinc reloads and re-JITs the entire Kotlin compiler on every call, so a
long-lived worker that compiles in-process is the Kotlin edit-loop win. The
worker is pure transport — it changes only speed, not what recompiles — so the
kotlin-abi skip above is unaffected. With the cross-build daemon (the default
under the Rust engine) the warm compiler even survives *between* `build`
invocations, so the second edit-rebuild pays no compiler-startup cost at all.

Setup: a `kotlinc` on PATH (or `BRIX_KOTLINC=/path/to/kotlinc`). kotlinc bundles
the Kotlin stdlib, so there are no external dependencies to resolve. The worker
finds `lib/kotlin-compiler.jar` beside the launcher (override with
`BRIX_KOTLIN_COMPILER`) and launches it with the `java` on PATH.

Run (from anywhere):
    python examples/kotlin_multimodule/brix_build.py build
    python examples/kotlin_multimodule/brix_build.py status
    # then edit core/src/lib.kt and rebuild to watch what re-runs.
"""

import os
import shutil
import sys
from pathlib import Path

# Point at this repository's source tree for the in-tree example.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, kotlin  # noqa: E402

KOTLINC = os.environ.get("BRIX_KOTLINC", "kotlinc")


def _kotlin_compiler() -> str:
    """The dist's lib/kotlin-compiler.jar, which the warm worker instantiates
    in-process. Derived from the kotlinc launcher (dist layout: bin/kotlinc ->
    lib/kotlin-compiler.jar); override with BRIX_KOTLIN_COMPILER."""
    override = os.environ.get("BRIX_KOTLIN_COMPILER")
    if override:
        return override
    launcher = shutil.which(KOTLINC) or KOTLINC
    return str(Path(launcher).resolve().parent.parent / "lib" / "kotlin-compiler.jar")


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    # A warm kotlinc worker: kotlinc reloads and re-JITs the whole compiler on
    # every cold call, so keeping it alive between compilations is the big
    # edit-loop win (the Kotlin analogue of java.javac_worker). Both modules
    # route through the one worker; it is pure transport, so the kotlin-abi
    # skip below is unchanged.
    worker = kotlin.kotlinc_worker(graph, kotlin_compiler=_kotlin_compiler())
    graph.add(
        kotlin.JarAction(
            source_files=data.glob("core/src/{name}.kt"),
            output_file=data.file("build/core.jar"),
            classes_dir="build/core-classes",
            jvm_target="17",
            kotlinc_worker=worker,
        ),
        kotlin.JarAction(
            source_files=data.glob("app/src/{name}.kt"),
            output_file=data.file("build/app.jar"),
            classes_dir="build/app-classes",
            jvm_target="17",
            classpath=[data.file("build/core.jar")],  # kotlin-abi-lensed; see module docstring
            entry_point="demo.app.MainKt",
            kotlinc_worker=worker,
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
