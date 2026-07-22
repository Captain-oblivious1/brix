#!/usr/bin/env python3
"""A project brix_build.py with the CLI section, per the spec's user-file shape.

Try, in order:

    python3 examples/cli_demo/brix_build.py status      # everything: never built
    python3 examples/cli_demo/brix_build.py             # build (incremental)
    python3 examples/cli_demo/brix_build.py status      # everything: fresh (exit 0)
    python3 examples/cli_demo/brix_build.py graph       # plan structure
    python3 examples/cli_demo/brix_build.py errors      # no recorded failures
    python3 examples/cli_demo/brix_build.py clean       # back to square one

Edit or delete a file under src/ between runs and watch status explain
exactly what would rebuild and why — without building anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import ExpandAll, ExternalProcessAction, Graph, cli

HERE = Path(__file__).parent


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
                outputs={"dest": data.file("build/upper/{name}.txt")},
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
            outputs={"combined": data.file("build/all.txt")},
        ),
    )

    return graph


if __name__ == "__main__":
    sys.exit(cli.main(create_graph, root=HERE))
