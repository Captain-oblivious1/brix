"""Emit the Python lowering of a `python.PytestAction` graph as JSON.

The TS skin oracle (`src/test/skin_python.test.ts`) builds the same usage with
the TS `python.PytestAction`, lowers it, and asserts the specs are deep-equal —
proving the TS skin lowers to exactly the core actions the Python skin does.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, python  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        python.PytestAction(
            test_sources=d.glob("tests/test_{name}.py"),
            python=d.tool("python3"),
            manifest_dir="build/deps",
            options=["-q", "-x"],
            env={"PYTHONPATH": "src"},
        )
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
