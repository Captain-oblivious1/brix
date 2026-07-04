#!/usr/bin/env python3
"""A lifelike C++ project: typed actions, every layer we have, full CLI.

Needs a c++ compiler on PATH. Try:

    python3 examples/cpp_app/brix.py             # build (incremental + atomic)
    ./examples/cpp_app/build/bin/app             # run the result
    python3 examples/cpp_app/brix.py             # again: everything replays
    python3 examples/cpp_app/brix.py status      # exit 0, all fresh
    touch examples/cpp_app/src/greeting.cpp      # (or actually edit it)
    python3 examples/cpp_app/brix.py status      # explains what will rebuild
    python3 examples/cpp_app/brix.py errors      # after a broken edit: diagnostics
    python3 examples/cpp_app/brix.py clean

The graph uses the typed brix.cpp helpers, which lower to the same generic
model as ExternalProcessAction. The builder stacks every capability that
exists so far: incremental (fingerprint skip, deleted-source cleanup)
inside atomic (outputs stage and commit only if the whole build succeeds —
kill a build halfway and the workspace is untouched).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import AtomicBuilder, Graph, IncrementalBuilder, cli, cpp

HERE = Path(__file__).parent


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/obj/{name}.o"),
            compiler=data.tool("c++"),
            compile_options=["-O2", "-std=c++17", "-Wall"],
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/bin/app"),
            linker=data.tool("c++"),
        ),
    )

    return graph


def create_builder() -> AtomicBuilder:
    return AtomicBuilder(IncrementalBuilder(create_graph, root=HERE))


if __name__ == "__main__":
    sys.exit(cli.main(create_graph, root=HERE, builder_factory=create_builder))
