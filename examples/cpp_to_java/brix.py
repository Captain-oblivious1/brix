#!/usr/bin/env python3
"""The composability proof: C++, generic, and Java actions in one graph.

Needs c++, javac, and jar on PATH. The chain:

    tools/schema_generator/*.cpp   --compile+link-->   schema_generator
    schema_generator + schema.csv  --runs-->           build/generated/java/*.java
    handwritten + generated .java  --javac+jar-->      build/libs/app.jar

Then:  java -jar examples/cpp_to_java/build/libs/app.jar

One logical graph, three kinds of action (typed C++, raw process, typed
Java), dependency edges inferred from data overlap, built with every layer:
incremental (touch nothing, nothing runs; edit schema.csv and only
generation onward reruns) inside atomic (kill it halfway, the workspace is
untouched).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import (
    AtomicBuilder,
    ExternalProcessAction,
    Graph,
    IncrementalBuilder,
    cli,
    cpp,
    java,
)

HERE = Path(__file__).parent


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("tools/schema_generator/{name}.cpp"),
            object_files=data.glob("build/tools/obj/{name}.o"),
            compiler=data.tool("c++"),
            compile_options=["-O2", "-std=c++17"],
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/tools/obj/{name}.o"),
            output_file=data.file("build/tools/schema_generator"),
        ),
        ExternalProcessAction(
            name="generate java sources",
            command_line="{generator} {schema} {out_dir}",
            inputs={
                "generator": data.file("build/tools/schema_generator"),
                "schema": data.file("src/schema.csv"),
            },
            outputs={
                # out_dir is an *output* declaration, so it renders through
                # output_root and the generated files stage under atomic.
                "out_dir": data.file("build/generated/java"),
                "generated": data.glob("build/generated/java/{class_name}.java"),
            },
        ),
        java.JarAction(
            source_files=(
                data.glob("src/main/java/{class_name}.java")
                + data.glob("build/generated/java/{class_name}.java")
            ),
            output_file=data.file("build/libs/app.jar"),
            entry_point="App",
        ),
    )

    return graph


def create_builder() -> AtomicBuilder:
    return AtomicBuilder(IncrementalBuilder(create_graph, root=HERE))


if __name__ == "__main__":
    sys.exit(cli.main(create_graph, root=HERE, builder_factory=create_builder))
