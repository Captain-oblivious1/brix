"""Emit the Python lowering of a graph using an `asset` atom (a brix-shipped
helper referenced by logical name) — the frontend half of the asset mechanism.
The TS side (`src/test/asset.test.ts`) builds the same graph and asserts the
specs are deep-equal, so both frontends emit an identical asset reference."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import ExternalProcessAction, Graph  # noqa: E402
from brix.data import AssetData  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="run worker",
            command_line="{java} -jar {worker}",
            inputs={"java": d.tool("java"), "worker": AssetData("java/JavacWorker.java")},
            outputs={},
        )
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
