"""Package: services/api — depends on libs/core.

No cross-package dependency is *declared*: this action simply consumes
``build/core/core.txt``, the output libs/core produces. brix derives the
core -> api edge from that overlap, and the persisted index records it so a
later build can tell that editing core implicates api without loading core.
"""

import sys

from brix import ExternalProcessAction, Graph


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="build api",
            command_line="{python} {tool} api {src} {dest}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tools/step.py"),
                "src": d.file("build/core/core.txt"),
            },
            outputs={"dest": d.file("build/api/api.txt")},
        )
    )
    return g
