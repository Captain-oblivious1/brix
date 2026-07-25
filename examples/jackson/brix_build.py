#!/usr/bin/env python3
"""Build three real Jackson modules and run one selective smoke suite per module.

The graph is ``annotations -> databind <- core``. Compile edges use the
``jvm-abi`` lens; test-run edges use complete jar bytes. Consequently, a core
implementation change rebuilds core and reruns the core + databind suites, but
does not recompile databind or touch the annotations suite.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, java  # noqa: E402
from brix.actions import ExternalProcessAction  # noqa: E402
from brix.data import FileData  # noqa: E402

_configured_root = Path(
    os.environ.get("BRIX_JACKSON_ROOT", os.path.expanduser("~/Projects/brix_test"))
).resolve()
# Preserve the old override, which pointed directly at jackson-core.
ROOT = _configured_root.parent if _configured_root.name == "jackson-core" else _configured_root

CORE = "jackson-core"
ANNOTATIONS = "jackson-annotations"
DATABIND = "jackson-databind"
VERSION = "2.19.4"
LIB = f"{CORE}/lib"
CONSOLE = f"{LIB}/junit-platform-console-standalone-1.11.4.jar"
FASTDOUBLE = f"{LIB}/fastdoubleparser-2.0.1.jar"
GEN_VERSION = str(HERE / "gen_version.py")


def _generated_version(
    graph: Graph, *, module: str, package: str, artifact: str, output: str
) -> None:
    d = graph.data
    template = f"{module}/src/main/java/{package.replace('.', '/')}/PackageVersion.java.in"
    graph.add(
        ExternalProcessAction(
            name=f"generate {artifact} PackageVersion",
            command_line=(
                "{python} {gen} {template} {out}"
                f" package={package} projectversion={VERSION}"
                " projectgroupid=com.fasterxml.jackson.core"
                f" projectartifactid={artifact}"
            ),
            inputs={
                "python": d.tool(sys.executable),
                "gen": FileData(GEN_VERSION),
                "template": d.file(template),
            },
            outputs={"out": d.file(output)},
        )
    )


def build_module_jars(graph: Graph, worker) -> dict[str, str]:
    """Add the three real Jackson module jars to ``graph`` and return their
    output paths keyed by module. Shared so a second build file (the real
    databind test-impact demo) can reuse the exact same jar graph and worker
    without recompiling the smoke suites."""
    d = graph.data
    annotations_jar = "build/libs/jackson-annotations.jar"
    core_jar = "build/libs/jackson-core.jar"
    databind_jar = "build/libs/jackson-databind.jar"

    graph.add(
        java.JarAction(
            source_files=d.glob(f"{ANNOTATIONS}/src/main/java/{{name}}.java"),
            output_file=d.file(annotations_jar),
            classes_dir="build/classes/annotations",
            resource_dir=f"{ANNOTATIONS}/src/main/resources",
            javac_worker=worker,
            options=["--release", "17", "-encoding", "UTF-8"],
        )
    )

    core_generated = "build/generated/core/com/fasterxml/jackson/core/json/PackageVersion.java"
    _generated_version(
        graph,
        module=CORE,
        package="com.fasterxml.jackson.core.json",
        artifact="jackson-core",
        output=core_generated,
    )
    graph.add(
        java.JarAction(
            source_files=(
                d.glob(f"{CORE}/src/main/java/{{name}}.java") + d.file(core_generated)
            ),
            output_file=d.file(core_jar),
            classes_dir="build/classes/core",
            resource_dir=f"{CORE}/src/main/resources",
            classpath=[d.file(FASTDOUBLE)],
            javac_worker=worker,
            options=["--release", "17", "-encoding", "UTF-8"],
        )
    )

    databind_generated = (
        "build/generated/databind/com/fasterxml/jackson/databind/cfg/PackageVersion.java"
    )
    _generated_version(
        graph,
        module=DATABIND,
        package="com.fasterxml.jackson.databind.cfg",
        artifact="jackson-databind",
        output=databind_generated,
    )
    graph.add(
        java.JarAction(
            source_files=(
                d.glob(f"{DATABIND}/src/main/java/{{name}}.java")
                + d.file(databind_generated)
            ),
            output_file=d.file(databind_jar),
            classes_dir="build/classes/databind",
            resource_dir=f"{DATABIND}/src/main/resources",
            classpath=[d.file(annotations_jar), d.file(core_jar)],
            javac_worker=worker,
            options=["--release", "17", "-encoding", "UTF-8"],
        )
    )
    return {"annotations": annotations_jar, "core": core_jar, "databind": databind_jar}


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)

    jars = build_module_jars(graph, worker)
    annotations_jar = jars["annotations"]
    core_jar = jars["core"]
    databind_jar = jars["databind"]

    suites = (
        ("annotations", "AnnotationsSmokeTest.java", [annotations_jar]),
        ("core", "CoreSmokeTest.java", [core_jar, FASTDOUBLE]),
        (
            "databind",
            "DatabindSmokeTest.java",
            [databind_jar, core_jar, annotations_jar, FASTDOUBLE],
        ),
    )
    for name, source, classpath in suites:
        graph.add(
            java.JUnitTestAction(
                name=f"{name} smoke tests",
                test_sources=d.file(str(HERE / "smoke" / name / source)),
                classpath=[d.file(path) for path in classpath],
                console_jar=CONSOLE,
                test_classes_dir=f"build/test-classes/{name}",
                javac_worker=worker,
                options=["--release", "17", "-encoding", "UTF-8"],
            )
        )
    return graph


def builder():
    # The JUnit runner reads real jar paths, so outputs are written in place.
    return CacheBuilder(
        IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
        cache_dir="build/.brix-cache",
    )


if __name__ == "__main__":
    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            builder_factory=builder,
            aliases={
                "annotations": "build/libs/jackson-annotations.jar",
                "core": "build/libs/jackson-core.jar",
                "databind": "build/libs/jackson-databind.jar",
            },
        )
    )
