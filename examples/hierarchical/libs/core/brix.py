"""Package: libs/core — the leaf of the dependency chain.

A package build file exposes ``create_graph()`` and nothing else; it is loaded
and merged by the workspace loader, never run on its own. Paths stay
workspace-root-relative, exactly as in a monolith.
"""

import sys

from brix import ExternalProcessAction, Graph


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="build core",
            command_line="{python} {tool} core {src} {dest}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tools/step.py"),
                "src": d.file("libs/core/src.txt"),
            },
            outputs={"dest": d.file("build/core/core.txt")},
        )
    )
    return g
