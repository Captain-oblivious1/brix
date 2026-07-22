#!/usr/bin/env python3
"""Build a two-module TypeScript project with brix — the dogfood for `brix.ts`
and the **ts-dts** lens (the TypeScript half of a Java+TS monorepo).

`greeter` is a library; `app` imports its built declaration. Each module is one
`tsc` spawn that brix owns the incrementality of (no `tsc --build`): brix re-runs
a module only when a source or a tracked dependency actually changes. The app
declares the greeter's emitted `greet.d.ts` as a `ts.CompileAction` dependency,
so it is tracked through the ts-dts lens — an implementation-only edit to
`greet()` re-emits a byte-identical `.d.ts` and the app **replays**; changing an
exported signature recompiles it.

Plain incremental stack (no atomic staging), on purpose: `tsc` resolves the
`import "../../greeter/dist/greet"` by walking the real filesystem itself, so the
greeter's built `.d.ts` must live at its actual path when the app compiles.
Java sidesteps this because brix passes the overlay-resolved classpath into
`{classpath}`, but a tool doing its own module resolution can't be redirected —
so cross-module TS wants outputs in place, not in an atomic overlay.

Setup (once) — a local TypeScript, since tsc is rarely global:
    cd examples/typescript && npm install typescript@5

Run (from anywhere):
    python examples/typescript/brix_build.py build      # -> greeter/dist, app/dist
    python examples/typescript/brix_build.py status
    node examples/typescript/app/dist/main.js     # -> Hello, brix!

    # See the lens skip: edit greeter/src/greet.ts's *body* only, rebuild, and
    # the app step reports (replayed); edit its exported signature and it rebuilds.

tsc is found via BRIX_TSC, else ./node_modules/.bin/tsc, else a global `tsc`.
The path setup below points this in-tree example at this repository's source.
"""

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, ts  # noqa: E402

ROOT = HERE

# tsc is usually a local devDependency; prefer a pointed-at one, then the local
# install, then a global tsc.
_local = HERE / "node_modules" / ".bin" / "tsc"
TSC = (
    os.environ.get("BRIX_TSC")
    or (str(_local) if _local.exists() else None)
    or shutil.which("tsc")
    or "tsc"
)

# Deterministic, predictable emit; explicit rootDir per module so output paths
# don't depend on the inputs' common ancestor.
_COMMON = ["--module", "commonjs", "--moduleResolution", "node", "--target", "es2020"]


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    tsc = data.tool(TSC)
    graph.add(
        ts.CompileAction(
            sources=data.glob("greeter/src/{name}.ts"),
            out_dir="greeter/dist",
            tsc=tsc,
            options=[*_COMMON, "--rootDir", "greeter/src"],
        ),
        ts.CompileAction(
            sources=data.glob("app/src/{name}.ts"),
            out_dir="app/dist",
            tsc=tsc,
            options=[*_COMMON, "--rootDir", "app/src"],
            # Track the greeter's public API through ts-dts.
            dependencies=[data.file("greeter/dist/greet.d.ts")],
        ),
    )
    return graph


if __name__ == "__main__":
    # Default builder: a plain IncrementalBuilder (outputs written in place, so
    # tsc's own import resolution finds the greeter's built .d.ts).
    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            aliases={"app": "app/dist/main.js"},
        )
    )
