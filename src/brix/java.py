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


def _runtime_union(classpath: list[DataExpr]) -> DataExpr:
    """Byte-lens view of the classpath jars (`identity=None`): execution
    depends on the *implementation*, so a test re-runs whenever any byte on
    its runtime classpath changes — the counterpart to `_classpath_union`,
    which uses the ABI lens for compilation."""
    atoms: list = []
    for expr in classpath:
        for atom in expr.atoms():
            if isinstance(atom, FileData):
                atoms.append(FileData(atom.path))  # drop any identity
            elif isinstance(atom, GlobData):
                atoms.append(GlobData(atom.pattern.template))
            else:
                atoms.append(atom)
    return DataUnion(*atoms)


def _classpath_paths(classpath: list[DataExpr]) -> list[str]:
    return [
        atom.path
        for expr in classpath
        for atom in expr.atoms()
        if isinstance(atom, FileData)
    ]


class JUnitTestAction(Action):
    """Compile JUnit 5 test sources and run them with the platform console
    launcher, so brix runs a suite only when its inputs actually change.

    The key move is two lenses on the same ``classpath`` jars: the test
    *compile* depends on them through ``jvm-abi`` (compilation cares only
    about the API), while the test *run* depends on them through their
    **bytes** (execution cares about the implementation). So an
    implementation-only change to the code under test does not recompile the
    tests but does re-run them; a change touching nothing on a suite's
    classpath re-runs nothing.

    The run declares no file outputs, so it replays purely on its input
    fingerprint: a passing suite is cached like any result (and restored on a
    clean checkout), while a failing suite is recorded as failed and re-runs
    until its inputs change.

    ``classpath`` is the code under test (jars). ``console_jar`` is a
    ``junit-platform-console-standalone`` jar — the runner and the source of
    the Jupiter API the tests compile against. Tests compile through the warm
    javac worker (required, for the classpath join).
    """

    def __init__(
        self,
        *,
        test_sources: DataExpr,
        classpath: list[DataExpr],
        console_jar: str,
        javac_worker: "WorkerSpec",
        name: str = "junit tests",
        test_classes_dir: str = "build/test-classes",
        test_resource_dirs: list[str] | None = None,
        java: ToolData | None = None,
        options: list[str] | DataExpr | None = None,
        select: str = "--scan-class-path",
    ) -> None:
        self.test_sources = test_sources
        self.classpath = classpath
        self.console_jar = console_jar
        self.javac_worker = javac_worker
        self.name = name
        self.test_classes_dir = test_classes_dir
        # Directories of test data the suite reads from the classpath (e.g.
        # src/test/resources). Added to the run classpath and their files
        # declared as byte-lens inputs, so editing test data re-runs the
        # suite — the "declare everything a test reads" soundness rule.
        self.test_resource_dirs = test_resource_dirs or []
        self.java = java if java is not None else ToolData("java")
        self.options = options
        self.select = select
        if javac_worker is None:
            raise ValueError("JUnitTestAction requires javac_worker= (classpath join)")

    def lower(self) -> list[Action]:
        test_classes = GlobData(f"{self.test_classes_dir}/{{name}}.class")

        # 1. Compile the tests: classpath = code under test + the console jar
        #    (which supplies the Jupiter API), through the jvm-abi lens.
        compile_tests = ExternalProcessAction(
            name=f"compile {self.name} -> {self.test_classes_dir}",
            worker=self.javac_worker,
            command_line=(
                "--classpath {classpath} --options {options} -d {out} --sources {sources}"
            ),
            inputs={
                "classpath": _classpath_union([*self.classpath, FileData(self.console_jar)]),
                "options": _options_expr(self.options),
                "sources": self.test_sources,
            },
            outputs={"out": FileData(self.test_classes_dir), "classes": test_classes},
        )

        # 2. Run the tests: --class-path is the compiled tests + the code under
        #    test + any test-resource dirs (baked literal paths); those jars
        #    and resource files are byte-lens inputs, so an implementation or
        #    test-data change re-runs the suite. No file outputs.
        cp_flags = " ".join(
            f"--class-path {p}"
            for p in [
                self.test_classes_dir,
                *_classpath_paths(self.classpath),
                *self.test_resource_dirs,
            ]
        )
        run_inputs: dict[str, DataExpr] = {
            "java": self.java,
            "console": FileData(self.console_jar),  # byte lens (framework)
            "under_test": _runtime_union(self.classpath),  # byte lens: impl matters
            "tests": test_classes,  # byte lens
        }
        for i, res_dir in enumerate(self.test_resource_dirs):
            run_inputs[f"data{i}"] = GlobData(f"{res_dir}/{{name}}")  # byte lens: test data
        run_tests = ExternalProcessAction(
            name=f"run {self.name}",
            command_line=(
                "{java} -jar {console} execute --disable-banner --details=none"
                f" --fail-if-no-tests {self.select} {cp_flags}"
            ),
            inputs=run_inputs,
            outputs={},  # exit code only — replays on the input fingerprint
        )
        return [compile_tests, run_tests]
