"""Typed Java actions.

Like `brix.cpp`, these lower into the generic action/data model when added
to a graph — one underlying representation, so C++, Java, and raw process
actions compose freely (a C++ tool can generate Java sources that feed a
`JarAction`).

    java.JarAction(
        source_files=(
            data.glob("src/main/java/{class_name}.java")
            + data.glob("build/generated/java/{class_name}.java")
        ),
        output_file=data.file("build/libs/app.jar"),
        entry_point="App",
    )

Java compilation is inherently whole-program (javac sees every source at
once), so this lowers to two actions: one javac invocation producing a
classes directory, and one jar invocation packaging it.

Directory convention: the classes directory is declared as a
``data.file`` *output* of the javac step (so it renders through
``output_root`` and stages under atomic builds) and as a ``data.file``
*input* of the jar step (so it resolves through the overlay). Class-file
tracking uses a bare ``{name}.class`` glob, which is recursive by default
(matches across ``/``) and so covers packaged classes
(``com/foo/Bar.class``), not just default-package ones — a changed
packaged class thus re-runs the jar step, avoiding a stale jar.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .actions import Action, ExternalProcessAction
from .data import DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData

if TYPE_CHECKING:
    from .graph import Graph
    from .workers import WorkerSpec

_WORKER_SOURCE = Path(__file__).parent / "_java" / "JavacWorker.java"


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


def _classpath_union(classpath: list[DataExpr]) -> DataExpr:
    """Flatten classpath entries into one expression, defaulting each file to
    ``identity="jvm-abi"`` — the whole point is that a dependency recompiles a
    dependent only on an API change."""
    atoms: list = []
    for expr in classpath:
        for atom in expr.atoms():
            if isinstance(atom, FileData) and atom.identity is None:
                atoms.append(FileData(atom.path, identity="jvm-abi"))
            elif isinstance(atom, GlobData) and atom.identity is None:
                atoms.append(GlobData(atom.pattern.template, identity="jvm-abi"))
            else:
                atoms.append(atom)
    return DataUnion(*atoms)


def javac_worker(
    graph: "Graph",
    *,
    java: ToolData | None = None,
    javac: ToolData | None = None,
    jar_tool: ToolData | None = None,
    work_dir: str = "build/.brix-java",
) -> "WorkerSpec":
    """Add the javac worker bootstrap to ``graph`` and return its `WorkerSpec`.

    The worker is a small in-process ``JavaCompiler`` wrapper shipped with brix;
    it is compiled and jarred by two plain (spawned) nodes here, so the worker
    jar is itself a fingerprinted, cacheable build product. Pass the returned
    spec to ``JarAction(javac_worker=...)`` to route module compilation through
    the warm worker.

    Idempotent: called more than once on a graph, it adds the bootstrap only
    once and returns the same spec, so many `JarAction`s share one worker jar.
    """
    from .workers import WorkerSpec

    cached = getattr(graph, "_brix_javac_worker", None)
    if cached is not None:
        return cached

    java = java if java is not None else ToolData("java")
    javac = javac if javac is not None else ToolData("javac")
    jar_tool = jar_tool if jar_tool is not None else ToolData("jar")
    classes_dir = f"{work_dir}/worker-classes"
    worker_jar = f"{work_dir}/javac-worker.jar"
    classes = GlobData(f"{classes_dir}/{{name}}.class")

    graph.add(
        ExternalProcessAction(
            name="javac-worker: compile",
            command_line="{javac} -d {classes_dir} {source}",
            inputs={"javac": javac, "source": FileData(str(_WORKER_SOURCE.resolve()))},
            outputs={"classes_dir": FileData(classes_dir), "classes": classes},
        ),
        ExternalProcessAction(
            name="javac-worker: jar",
            command_line="{jar} cfe {out} JavacWorker -C {classes_dir} .",
            inputs={
                "jar": jar_tool,
                "classes_dir": FileData(classes_dir),
                "classes": GlobData(f"{classes_dir}/{{name}}.class"),
            },
            outputs={"out": FileData(worker_jar)},
        ),
    )
    spec = WorkerSpec(
        name="javac",
        command_line="{java} -jar {worker_jar}",
        inputs={"java": java, "worker_jar": FileData(worker_jar)},
    )
    graph._brix_javac_worker = spec  # type: ignore[attr-defined]
    return spec


class JarAction(Action):
    """Compile Java sources and package them into a jar."""

    def __init__(
        self,
        *,
        source_files: DataExpr,
        output_file: FileData,
        javac: ToolData | None = None,
        jar_tool: ToolData | None = None,
        options: list[str] | DataExpr | None = None,
        classes_dir: str = "build/classes",
        entry_point: str | None = None,
        classpath: list[DataExpr] | None = None,
        resource_dir: str | None = None,
        javac_worker: "WorkerSpec | None" = None,
    ) -> None:
        self.source_files = source_files
        self.output_file = output_file
        self.javac = javac if javac is not None else ToolData("javac")
        self.jar_tool = jar_tool if jar_tool is not None else ToolData("jar")
        self.options = options
        self.classes_dir = classes_dir
        self.entry_point = entry_point
        self.classpath = classpath
        # A resources root (e.g. "src/main/resources"): its whole tree is
        # packaged into the jar at the same relative paths as the classes, so
        # a module's META-INF/services registrations, .properties, etc. ship
        # in the jar. Its files are jar-step inputs, so an edited resource
        # re-jars. None = classes only.
        self.resource_dir = resource_dir
        # When set, the compile step routes through the warm javac worker (see
        # java.javac_worker); otherwise it spawns javac. classpath= currently
        # rides the worker (the wrapper joins entries with the path separator).
        self.javac_worker = javac_worker
        if classpath and javac_worker is None:
            raise ValueError(
                "JarAction: classpath= currently requires javac_worker= "
                "(java.javac_worker(graph)); pass one, or drop classpath"
            )

    def lower(self) -> list[Action]:
        # Bare `{name}` is recursive, so class tracking reaches packaged classes
        # (`com/foo/Bar.class`), not just default-package ones — otherwise a
        # changed packaged class re-runs javac but not the jar step, leaving a
        # stale jar. Realizes through the overlay-aware `find()`, so it also sees
        # freshly compiled classes staged under an atomic build.
        classes = GlobData(f"{self.classes_dir}/{{name}}.class")
        if self.javac_worker is not None:
            inputs: dict[str, DataExpr] = {
                "options": _options_expr(self.options),
                "sources": self.source_files,
            }
            parts = []
            if self.classpath:
                inputs["classpath"] = _classpath_union(self.classpath)
                parts.append("--classpath {classpath}")
            parts += ["--options {options}", "-d {classes_dir}", "--sources {sources}"]
            compile_java = ExternalProcessAction(
                name=f"javac(worker) -> {self.classes_dir}",
                worker=self.javac_worker,
                command_line=" ".join(parts),
                inputs=inputs,
                outputs={
                    "classes_dir": FileData(self.classes_dir),
                    "classes": classes,
                },
            )
        else:
            compile_java = ExternalProcessAction(
                name=f"javac -> {self.classes_dir}",
                command_line="{javac} {options} -d {classes_dir} {sources}",
                inputs={
                    "javac": self.javac,
                    "options": _options_expr(self.options),
                    "sources": self.source_files,
                },
                outputs={
                    "classes_dir": FileData(self.classes_dir),
                    "classes": classes,
                },
            )
        if self.entry_point:
            jar_command = "{jar} cfe {output} {entry_point} -C {classes_dir} ."
            jar_inputs = {"entry_point": ValueData(self.entry_point)}
        else:
            jar_command = "{jar} cf {output} -C {classes_dir} ."
            jar_inputs = {}
        if self.resource_dir is not None:
            # A second `-C <root> .` packs the resource tree at its relative
            # paths (jar accepts repeated -C). The tree's files are inputs so
            # an edited resource re-jars; the recursive `{name}` glob reaches
            # nested resources (META-INF/services/...), like the classes glob.
            jar_command += " -C {resource_dir} ."
            jar_inputs["resource_dir"] = FileData(self.resource_dir)
            jar_inputs["resources"] = GlobData(f"{self.resource_dir}/{{name}}")
        package = ExternalProcessAction(
            name=f"jar {self.output_file.path}",
            command_line=jar_command,
            inputs={
                "jar": self.jar_tool,
                "classes_dir": FileData(self.classes_dir),
                "classes": classes,  # recursive glob — reruns on any class change
                **jar_inputs,
            },
            outputs={"output": self.output_file},
        )
        return [compile_java, package]
