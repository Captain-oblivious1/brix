"""Package: services/web — depends on services/api (two hops from core).

Consumes ``build/api/api.txt``. The chain is core -> api -> web, so editing
core must reload core, api, *and* web: the selector closes over transitive
dependents, not just direct ones.
"""

import sys

from brix import ExternalProcessAction, Graph


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name="build web",
            command_line="{python} {tool} web {src} {dest}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tools/step.py"),
                "src": d.file("build/api/api.txt"),
            },
            outputs={"dest": d.file("build/web/web.txt")},
        )
    )
    return g
