"""Emit the Python lowering of a `ts` skin graph as JSON.

Covers the byte-equal-provable surface of the TS ts skin: CompileAction with a
ts-dts-lensed dependency (proving the content-key crossing), track_imports off
(the import-tracking wrapper's invocation is a pending decision — see the TS
skin), plus RunTestAction and TypedocAction.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, ts  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ts.CompileAction(
            sources=d.glob("src/{name}.ts"),
            out_dir="dist",
            options=["--target", "es2022", "--strict"],
            dependencies=[d.glob("../shared/dist/{name}.d.ts")],
            track_imports=False,
        ),
        ts.RunTestAction(
            test_file="tests/foo.test.ts",
            sources=["src/foo.ts", d.glob("src/util/{name}.ts")],
            args=["--reporter", "dot"],
        ),
        ts.TypedocAction(
            entry_points=d.glob("src/{name}.ts"),
            output_dir="build/docs",
        ),
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
