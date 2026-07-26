"""Emit the Python lowering of a `java` skin graph as JSON.

Exercises JarAction (compile with the JVM env-used snapshot + jar with an entry
point and a resource dir) and JavadocAction with a jvm-abi-lensed classpath —
so the oracle covers both the env-sentinel path and the content-lens crossing.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, java  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        java.JarAction(
            source_files=d.glob("src/main/java/{name}.java"),
            output_file=d.file("build/libs/app.jar"),
            options=["--release", "17"],
            classes_dir="build/classes",
            entry_point="com.acme.App",
            resource_dir="src/main/resources",
        ),
        java.JavadocAction(
            source_files=d.glob("src/main/java/{name}.java"),
            output_dir="build/docs",
            classpath=[d.glob("libs/{name}.jar")],
            options=["-quiet"],
        ),
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
