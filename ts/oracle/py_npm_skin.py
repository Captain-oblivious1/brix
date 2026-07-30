"""Emit the Python lowering of a ts.npm-resolved graph as JSON.

Resolves a fixture node_modules tree with ts.npm — exercising a package's own
types (foo), the nested-node_modules skip (foo/node_modules/bar), sorting
(foo/index vs foo/lib/util), the @types fallback (bar), scoped-name mangling
(@scope/thing -> @types/scope__thing), and first-wins dedup (foo listed twice) —
then threads the resulting DepSet through ts.CompileAction(deps=...) alongside an
explicit upstream dependency, proving the dependencies/types fold order.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, ts  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "npm")


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    deps = ts.npm(["foo", "bar", "@scope/thing", "foo"], root=FIXTURE)
    g.add(
        ts.CompileAction(
            sources=d.glob("src/{name}.ts"),
            out_dir="dist",
            dependencies=[d.file("build/upstream/mod.d.ts")],
            deps=deps,
            track_imports=False,
        )
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
