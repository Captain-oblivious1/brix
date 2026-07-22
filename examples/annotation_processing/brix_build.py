#!/usr/bin/env python3
"""Self-contained annotation-processing example.

Brix first builds the processor jar, then uses it as both the annotation API
classpath and javac processor path for the application compile.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, java  # noqa: E402


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    worker = java.javac_worker(graph)
    graph.add(
        java.JarAction(
            source_files=data.glob("processor/src/{name}.java"),
            output_file=data.file("build/message-processor.jar"),
            classes_dir="build/processor-classes",
            javac_worker=worker,
        ),
        java.JarAction(
            source_files=data.glob("app/src/{name}.java"),
            output_file=data.file("build/app.jar"),
            classes_dir="build/app-classes",
            classpath=[data.file("build/message-processor.jar")],
            processorpath=[data.file("build/message-processor.jar")],
            options=["-processor", "MessageProcessor"],
            javac_worker=worker,
            entry_point="App",
        ),
    )
    return graph


if __name__ == "__main__":
    sys.exit(
        cli.main(
            create_graph,
            root=HERE,
            metadata_dir="build/.brix",
            aliases={"app": "build/app.jar"},
        )
    )
