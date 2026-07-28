"""Emit the Python lowering of a core `kotlin` skin graph as JSON."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import ExternalProcessAction, Graph, kotlin  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        kotlin.JarAction(
            source_files=d.glob("src/main/kotlin/{name}.kt"),
            output_file=d.file("build/libs/app.jar"),
            kotlinc=d.tool("kotlinc-custom"),
            jar_tool=d.tool("jar-custom"),
            classes_dir="build/classes/kotlin",
            jvm_target="21",
            spring_allopen="tools/allopen-compiler-plugin.jar",
            options=["-Xjsr305=strict"],
            entry_point="com.acme.AppKt",
            resource_dir="src/main/resources",
        ),
        ExternalProcessAction(
            name="consume Kotlin ABI",
            command_line="{tool} {classpath}",
            inputs={
                "tool": d.tool("consumer"),
                "classpath": kotlin.abi(
                    d.file("build/libs/app.jar") + d.glob("libs/{name}.jar")
                ),
            },
            outputs={"output": d.file("build/consumed.txt")},
        ),
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
