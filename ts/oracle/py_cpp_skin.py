"""Emit the Python lowering of a `cpp` skin graph as JSON.

Exercises CompileSourceAction (per-TU compile with -MMD + the toolchain env-used
snapshot), LinkApplicationAction, ArchiveAction, both RunTestAction binary-ref
branches (a path vs a bare name), and DoxygenAction.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, cpp  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        cpp.CompileSourceAction(
            source_file=d.glob("src/{name}.cpp"),
            object_file=d.glob("build/obj/{name}.o"),
            compiler=d.tool("clang++"),
            compile_options=["-O2", "-std=c++20"],
        ),
        cpp.LinkApplicationAction(
            object_files=d.glob("build/obj/{name}.o"),
            output_file=d.file("build/bin/app"),
            linker=d.tool("clang++"),
            link_options=["-flto"],
        ),
        cpp.ArchiveAction(
            object_files=d.glob("build/obj/{name}.o"),
            output_file=d.file("build/lib/libfoo.a"),
        ),
        cpp.RunTestAction(test_binary="build/bin/test_foo", args=["--gtest_color=no"]),
        cpp.RunTestAction(test_binary="bare_test", name="run bare"),
        # runner= branch ("{runner} {binary} {args}") + runtime_inputs (a string
        # and a DataExpr), exercising both otherwise-uncovered code paths.
        cpp.RunTestAction(
            test_binary="build/bin/plugin_test",
            runner=d.tool("valgrind"),
            runtime_inputs=["assets/data.bin", d.glob("plugins/{name}.so")],
            args=["-v"],
        ),
        cpp.DoxygenAction(
            config="Doxyfile",
            source_files=d.glob("src/{name}.h"),
            output_dir="build/docs/html",
        ),
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
