#!/usr/bin/env python3
"""The raw C++ graph from the design doc: compile each src/*.cpp, link an app.

Run from anywhere:  python3 examples/raw_cpp/brix.py
Requires a C++ compiler on PATH as `c++`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import CleanBuilder, ConsoleReporter, ExpandAll, ExternalProcessAction, Graph


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.cpp"),
            child=ExternalProcessAction(
                name="compile {name}",
                command_line="{compiler} {flags} -c {source} -o {object}",
                inputs={
                    "compiler": data.tool("c++"),
                    "flags": data.value("-O2 -std=c++20"),
                    "source": data.file("src/{name}.cpp"),
                },
                outputs={
                    "object": data.file("build/obj/{name}.o"),
                },
            ),
        ),
        ExternalProcessAction(
            name="link app",
            command_line="{compiler} {objects} -o {app}",
            inputs={
                "compiler": data.tool("c++"),
                "objects": data.glob("build/obj/{name}.o"),
            },
            outputs={
                "app": data.file("build/bin/app"),
            },
        ),
    )

    return graph


if __name__ == "__main__":
    result = CleanBuilder(create_graph(), root=Path(__file__).parent).build(reporter=ConsoleReporter())
    sys.exit(0 if result.success else 1)
