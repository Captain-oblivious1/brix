"""Package: docs — independent of the core -> api -> web chain.

It reads its own source and shares nothing with the other packages, so editing
the chain must NOT load it, and editing it must NOT load them. This is the
package the "load only what changed" story is proven against: it stays skipped.
"""

import sys

from brix import ExternalProcessAction, Graph


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="build docs",
            command_line="{python} {tool} docs {src} {dest}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tools/step.py"),
                "src": d.file("docs/src.txt"),
            },
            outputs={"dest": d.file("build/docs/docs.txt")},
        )
    )
    return g
