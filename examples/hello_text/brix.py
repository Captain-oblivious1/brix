#!/usr/bin/env python3
"""Text-processing demo: uppercase each src/*.txt, then combine them.

Run from anywhere:  python3 examples/hello_text/brix.py
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
            collection=data.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={
                    "dest": data.file("build/upper/{name}.txt"),
                },
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={
                "combined": data.file("build/all.txt"),
            },
        ),
    )

    return graph


if __name__ == "__main__":
    result = CleanBuilder(create_graph(), root=Path(__file__).parent).build(reporter=ConsoleReporter())
    sys.exit(0 if result.success else 1)
