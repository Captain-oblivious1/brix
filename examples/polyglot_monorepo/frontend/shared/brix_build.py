"""Package: frontend/shared — a TypeScript library.

Emits ``.js`` and, because it is a dependency of ``app``, a ``.d.ts`` beside
each. ``app`` tracks that declaration through the **ts-dts** lens, so an
implementation-only edit here lets ``app`` replay.

``tsc`` is rarely global: set ``BRIX_TSC`` or install a local one. This package
compiles independently of the backend — a frontend edit never loads a Java
package, which is the cross-language point of this example.
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
            sources=d.glob("frontend/shared/src/{name}.ts"),
            out_dir="build/frontend/shared",
            tsc=d.tool(TSC),
            options=["--target", "es2020", "--module", "commonjs",
                     "--rootDir", "frontend/shared/src", "--strict"],
            # Import tracking is dogfooded in examples/typescript; keep this
            # example focused on the cross-language workspace behavior.
            track_imports=False,
        )
    )
    return g
