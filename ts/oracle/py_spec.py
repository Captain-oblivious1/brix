"""Emit the Python lowering of the reference graph as JSON on stdout.

The TS oracle (`src/test/oracle.test.ts`) builds the *same* logical graph in
TypeScript, lowers it, and asserts the two specs are deep-equal — proving the TS
authoring surface produces byte-for-byte the same wire the Rust core already
runs from Python. The graph is hand-mirrored on both sides on purpose: two
independent authorings of one intent are the actual equivalence being tested.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import ExpandAll, ExternalProcessAction, Graph  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data

    inner = ExternalProcessAction(
        name="test {module}/{case}",
        command_line="{py} -m pytest {src} {opts}",
        inputs={
            "py": d.tool("python3"),
            "src": d.file("tests/{module}/{case}.py"),
            "opts": d.value("-q"),
            # a union: glob + single file
            "extra": d.glob("fixtures/{module}/*.json") + d.file("conftest.py"),
        },
        outputs={"report": d.file("build/{module}/{case}.xml")},
        env={"PYTHONPATH": "src"},
        depfile="build/{module}/{case}.d",
    )
    tests = ExpandAll(
        element_name="module",
        collection=d.glob("tests/{module:*}"),
        child=ExpandAll(
            element_name="case",
            collection=d.regex(r"tests/(?P<module>[^/]+)/(?P<case>[^/]+)\.py"),
            child=inner,
        ),
    )
    compile = ExternalProcessAction(
        name="compile",
        command_line="{cc} -c {src} -o {obj}",
        inputs={
            "cc": d.tool("cc"),
            # a glob with an exclusion narrowing
            "src": d.glob("src/*.c") - "*_test.c",
        },
        outputs={"obj": d.file("build/all.o")},
    )
    g.add(compile, tests)
    g.force_order(compile, tests)
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
