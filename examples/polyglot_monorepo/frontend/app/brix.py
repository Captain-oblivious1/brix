"""Package: frontend/app — a TypeScript app that depends on frontend/shared.

It declares ``shared``'s emitted ``.d.ts`` as a `ts.CompileAction` dependency
(ts-dts-lensed). No cross-package edge is *declared* to the workspace: brix
derives the ``shared -> app`` edge from that input/output overlap and persists
it, so a later build knows an edit to ``shared`` implicates ``app`` — while a
backend edit never does.
"""

import os
import shutil

from brix import Graph, ts

TSC = os.environ.get("BRIX_TSC") or shutil.which("tsc") or "tsc"


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ts.CompileAction(
            sources=d.glob("frontend/app/src/{name}.ts"),
            out_dir="build/frontend/app",
            tsc=d.tool(TSC),
            options=["--target", "es2020", "--module", "commonjs",
                     "--rootDir", "frontend/app/src", "--strict"],
            # shared's built declaration — the ts-dts-lensed dependency that
            # creates the shared -> app edge the selector reads.
            dependencies=[d.glob("build/frontend/shared/{name}.d.ts")],
            track_imports=False,
        )
    )
    return g
