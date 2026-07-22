"""Package: backend/libs/model — a Java library.

A workspace package exposes ``create_graph()`` and nothing else; the loader
merges it. Paths stay workspace-root-relative (as in a monolith), and each
package writes under its own ``build/`` subtree so outputs never collide.
"""

from brix import Graph, java


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        java.JarAction(
            source_files=d.glob("backend/libs/model/src/main/java/{name}.java"),
            output_file=d.file("build/backend/model.jar"),
            classes_dir="build/backend/model-classes",
        )
    )
    return g
