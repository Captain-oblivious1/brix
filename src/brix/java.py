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

import hashlib
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .actions import Action, ExternalProcessAction
from .data import DataAtom, DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData
from .deps import DepSet

if TYPE_CHECKING:
    from .graph import Graph
    from .workers import WorkerSpec

_WORKER_SOURCE = Path(__file__).parent / "_java" / "JavacWorker.java"


# --- dependency resolution: adopt a pom, or resolve inline coordinates ---
#
# brix does not resolve dependencies itself — it drives Maven and consumes the
# resolved classpath as tracked file inputs (docs/dependencies.md). Resolution
# is eager (at graph-construction) and memoized to `cache_dir` keyed by the pom
# bytes / coordinate list, so a no-op build re-reads the cache instead of
# re-running Maven; editing the pom or the coordinates re-resolves.


# Public API surface (curated for documentation and as a contract).
__all__ = [
    'JarAction',
    'JUnitTestAction',
    'javac_worker',
    'abi',
    'AbiFile',
    'AbiGlob',
    'pom',
    'maven',
    'gradle',
]


def _parse_classpath(text: str) -> list[str]:
    """The jar paths from a Maven `dependency:build-classpath` file (one
    os.pathsep-joined line of absolute ~/.m2 paths)."""
    return [p for p in text.strip().split(os.pathsep) if p.endswith(".jar")]


def _maven_build_classpath(
    mvn: str, pom: str, scope: str, out_file: str, cwd: str | None = None
) -> None:
    """Run `dependency:build-classpath` for a pom, writing the classpath to
    out_file. `cwd` is the directory Maven runs in (the project, so a `./mvnw`
    wrapper finds its `.mvn/`). Isolated so tests can substitute it without a
    live Maven."""
    subprocess.run(
        [
            mvn, "-q", "-f", pom, "dependency:build-classpath",
            f"-DincludeScope={scope}", f"-Dmdep.outputFile={out_file}",
        ],
        check=True,
        cwd=cwd,
    )


_SYNTHETIC_POM = """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>brix.synthetic</groupId>
  <artifactId>deps</artifactId>
  <version>1</version>
  <dependencies>
{deps}  </dependencies>
</project>
"""


def _coords_to_pom(coords: list[str]) -> str:
    body = ""
    for c in coords:
        parts = c.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"coordinate {c!r} must be 'group:artifact:version'"
            )
        g, a, v = parts
        body += (
            f"    <dependency><groupId>{g}</groupId>"
            f"<artifactId>{a}</artifactId><version>{v}</version></dependency>\n"
        )
    return _SYNTHETIC_POM.format(deps=body)


def _key(kind: str, payload: str, scope: str) -> str:
    h = hashlib.sha256(f"{kind}\0{scope}\0{payload}".encode()).hexdigest()[:16]
    return f"{kind}-{scope}-{h}"


def _cached(cache_dir: str | None, key: str) -> list[str] | None:
    if not cache_dir:
        return None
    cache = Path(cache_dir) / f"{key}.classpath"
    return [ln for ln in cache.read_text().splitlines() if ln] if cache.exists() else None


def _resolve_pom_file(
    key: str, pom_file: str, cache_dir: str | None, mvn: str, scope: str, cwd: str | None
) -> list[str]:
    """Resolve an on-disk pom file's classpath, memoized under cache_dir. The
    pom is resolved *in place* (so a parent pom / module layout still works);
    only the raw output and the cache live under cache_dir."""
    hit = _cached(cache_dir, key)
    if hit is not None:
        return hit
    work = Path(cache_dir) if cache_dir else Path(pom_file).resolve().parent
    work.mkdir(parents=True, exist_ok=True)
    out_file = (work / f"{key}.rawcp").resolve()
    _maven_build_classpath(mvn, str(Path(pom_file).resolve()), scope, str(out_file), cwd)
    jars = _parse_classpath(out_file.read_text())
    if cache_dir:
        (Path(cache_dir) / f"{key}.classpath").write_text("\n".join(jars))
    return jars


def pom(
    path: str | Path,
    *,
    mvn: str = "mvn",
    scope: str = "compile",
    cache_dir: str | None = ".brix-deps",
    cwd: str | None = None,
) -> DepSet:
    """Adopt an existing Maven ``pom.xml``: resolve its declared dependencies to
    a classpath and return it as a `DepSet`. The dependency declarations stay in
    the pom — this is the *migration* path (point brix at what Maven already
    resolves). Editing the pom re-resolves (the cache key is the pom's bytes).
    ``cwd`` is where Maven runs (pass the project dir when ``mvn`` is a
    ``./mvnw`` wrapper); ``mvn`` may be a wrapper path like ``"./mvnw"``."""
    path = Path(path)
    key = _key("pom", path.read_text(), scope)
    jars = _resolve_pom_file(key, str(path), cache_dir, mvn, scope, cwd)
    return DepSet(tuple(jars))


def maven(
    coords: list[str],
    *,
    mvn: str = "mvn",
    scope: str = "compile",
    cache_dir: str | None = ".brix-deps",
    cwd: str | None = None,
) -> DepSet:
    """Resolve inline Maven coordinates (``"group:artifact:version"``) to a
    classpath via Maven, returned as a `DepSet`. The declarations live in
    ``brix.py`` — the *greenfield / single-file* path. Editing the list
    re-resolves. Naming the engine (``maven``) is deliberate: Maven and Gradle
    apply different version-conflict rules, so this gives Maven's resolution."""
    key = _key("maven", "\n".join(coords), scope)
    hit = _cached(cache_dir, key)
    if hit is not None:
        return DepSet(tuple(hit))
    work = Path(cache_dir) if cache_dir else Path(".")
    work.mkdir(parents=True, exist_ok=True)
    pom_file = work / f"{key}.pom.xml"
    pom_file.write_text(_coords_to_pom(coords))
    jars = _resolve_pom_file(key, str(pom_file), cache_dir, mvn, scope, cwd)
    return DepSet(tuple(jars))


# --- dependency resolution: adopt a Gradle build ---
#
# The Gradle counterpart of java.pom: adopt an existing Gradle project and
# consume the classpath *Gradle* resolves. Naming the engine (`gradle`, vs
# `maven`) is load-bearing — Gradle picks the highest requested version on a
# conflict where Maven picks the nearest in the tree, so the same coordinates
# can resolve to different jars. Two mechanics differ from Maven:
#   * Gradle has no `dependency:build-classpath` goal, so we inject a temporary
#     init script (never editing the project's own build files) that writes a
#     configuration's resolved files;
#   * a pom is (mostly) self-contained, but Gradle spreads dependency
#     declarations across the build script, settings, gradle.properties and a
#     version catalog — so the cache key hashes all of them, not just one file.

# Companion files (beyond the build scripts) whose bytes fold into the cache key.
# Keying on the catalog too means bumping a version in `libs.versions.toml`
# re-resolves. Limitation: only the target project's own directory is scanned, so
# a version catalog at the *repo root* of a multi-project build is not seen here —
# bump a build file, or clear the cache, to force re-resolution in that case.
_GRADLE_COMPANION_FILES = (
    "gradle.properties",
    "gradle/libs.versions.toml",
)


def _gradle_manifest_material(project_dir: Path) -> str:
    """The dependency-declaring files' bytes, as the cache key material. The
    build scripts are matched by glob, not fixed names: Gradle lets a project use
    a custom build-file name (junit5 uses ``junit-platform-commons.gradle.kts``,
    not ``build.gradle.kts``), so ``*.gradle`` / ``*.gradle.kts`` catches those
    too. Sorted for a deterministic key."""
    files = sorted(
        [*project_dir.glob("*.gradle"), *project_dir.glob("*.gradle.kts")]
    )
    files += [project_dir / rel for rel in _GRADLE_COMPANION_FILES]
    parts = [
        f"{f.relative_to(project_dir)}\0{f.read_text()}"
        for f in files
        if f.is_file()
    ]
    if not parts:
        raise ValueError(f"no Gradle build files under {project_dir}")
    return "\0".join(parts)


# Registered on every project so a subproject can be targeted; only the invoked
# project's task runs, so exactly one write happens. `conf.files` forces
# resolution. Project-dependency entries (a class dir, not a jar) are dropped by
# `_parse_classpath`. The configuration name and output path are baked into the
# script rather than passed as `-P` properties: some builds intercept and
# *validate* project properties (junit5's `build-parameters` plugin rejects an
# unknown `-P`), so external properties are a collision surface we avoid.
_GRADLE_INIT_SCRIPT = """\
// Injected by brix (java.gradle) to print a configuration's resolved classpath
// without editing the project's own build files.
allprojects { p ->
    p.tasks.register("brixPrintClasspath") {
        doLast {
            def conf = p.configurations.findByName('__BRIX_CONFIGURATION__')
            if (conf == null) {
                throw new GradleException(
                    "java.gradle: no configuration '__BRIX_CONFIGURATION__' in ${p.path}")
            }
            new File('__BRIX_OUT__').text =
                conf.files.collect { it.absolutePath }.join(File.pathSeparator)
        }
    }
}
"""


def _render_gradle_init(configuration: str, out_file: str) -> str:
    def esc(s: str) -> str:  # for a single-quoted Groovy string literal
        return s.replace("\\", "\\\\").replace("'", "\\'")

    return _GRADLE_INIT_SCRIPT.replace(
        "__BRIX_CONFIGURATION__", esc(configuration)
    ).replace("__BRIX_OUT__", esc(out_file))


def _gradle_build_classpath(
    gradle: str,
    project_dir: str,
    configuration: str,
    project: str | None,
    out_file: str,
    cwd: str | None = None,
) -> None:
    """Resolve a Gradle configuration's classpath to out_file via an injected
    init script. `project` is a Gradle project path (":app"); None targets the
    root project. `cwd` is where gradle runs — defaults to project_dir so a
    `./gradlew` wrapper and settings.gradle are found. Isolated so tests can
    substitute it without a live Gradle."""
    init = Path(out_file).with_suffix(".init.gradle")
    init.write_text(_render_gradle_init(configuration, out_file))
    proj = ":" + project.lstrip(":") if project else ""
    subprocess.run(
        [
            gradle, "--quiet", "--console=plain",
            "--init-script", str(init), f"{proj}:brixPrintClasspath",
        ],
        check=True,
        cwd=cwd or project_dir,
    )


def gradle(
    path: str | Path,
    *,
    gradle: str = "gradle",
    configuration: str = "compileClasspath",
    project: str | None = None,
    cache_dir: str | None = ".brix-deps",
    cwd: str | None = None,
) -> DepSet:
    """Adopt an existing Gradle project: resolve a *configuration*'s classpath
    (Gradle's ``compileClasspath`` / ``runtimeClasspath`` / ``testRuntimeClasspath``,
    the analog of a Maven scope) and return it as a `DepSet`. This is the
    *migration* path for a Gradle build — the declarations stay in the build
    files; brix drives Gradle and consumes what it resolves (with Gradle's own
    version-conflict rules). Editing a build file re-resolves (see
    ``_GRADLE_MANIFEST_FILES`` for exactly which files the cache key covers).

    ``path`` is the project directory (or a build script inside it). ``project``
    is a Gradle project path (``":service-a"``) to target a subproject of a
    multi-project build; None targets the root project. ``gradle`` may be a
    wrapper path like ``"./gradlew"``; ``cwd`` is where it runs (defaults to the
    project dir)."""
    project_dir = Path(path)
    if project_dir.is_file():
        project_dir = project_dir.parent
    payload = _gradle_manifest_material(project_dir) + f"\0project={project or ''}"
    key = _key("gradle", payload, configuration)
    hit = _cached(cache_dir, key)
    if hit is not None:
        return DepSet(tuple(hit))
    work = Path(cache_dir) if cache_dir else project_dir
    work.mkdir(parents=True, exist_ok=True)
    out_file = (work / f"{key}.rawcp").resolve()
    _gradle_build_classpath(
        gradle, str(project_dir.resolve()), configuration, project, str(out_file), cwd
    )
    jars = _parse_classpath(out_file.read_text())
    if cache_dir:
        (Path(cache_dir) / f"{key}.classpath").write_text("\n".join(jars))
    return DepSet(tuple(jars))


class AbiFile(FileData):
    """A jar/class compared through the JVM ABI lens, not its bytes: a
    dependent recompiles on an API change, not an implementation-only one.
    ``brix.java`` owns this lens — the core dispatches the ``jvm-abi`` name
    through its content-key registry (docs/content-key.md)."""

    content_key = "jvm-abi"


class AbiGlob(GlobData):
    """The glob counterpart of `AbiFile`: the JVM ABI lens applied to every
    matched jar/class."""

    content_key = "jvm-abi"


def abi(expr: DataExpr) -> DataExpr:
    """View a data expression through the JVM ABI lens: every plain file/glob
    atom becomes its `AbiFile`/`AbiGlob` counterpart, so fingerprints compare
    those files by API surface. Non-file atoms (tools, values) and atoms that
    already carry a lens pass through unchanged. A classpath is many jars, so
    this applies across an expression's atoms, not just one file."""
    lensed: list[DataAtom] = []
    for atom in expr.atoms():
        if type(atom) is FileData:
            lensed.append(AbiFile(atom.path))
        elif type(atom) is GlobData:
            lensed.append(AbiGlob(atom.pattern.template, atom.exclude))
        else:
            lensed.append(atom)
    return lensed[0] if len(lensed) == 1 else DataUnion(*lensed)


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


def _as_classpath_list(
    classpath: "DepSet | list[DataExpr] | None",
) -> list[DataExpr] | None:
    """Normalize a classpath argument to a list of data expressions. A `DepSet`
    (from java.pom/java.maven) contributes its resolved jars as byte-lens file
    atoms; the consumer re-lenses (compile ABI, runtime byte), so the DepSet
    stays lens-neutral."""
    if classpath is None:
        return None
    if isinstance(classpath, DepSet):
        return list(classpath.files())
    return classpath


def _classpath_union(classpath: list[DataExpr]) -> DataExpr:
    """Flatten classpath entries into one ABI-lensed expression — the whole
    point is that a dependency recompiles a dependent only on an API change."""
    return abi(DataUnion(*classpath))


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
    """Compile Java sources and package them into a jar.

    ``processorpath`` contains annotation-processor jars. Unlike ``classpath``,
    these are tracked by their complete bytes: changing processor
    implementation must recompile sources even when the processor's Java ABI
    did not change.
    """

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
        classpath: "DepSet | list[DataExpr] | None" = None,
        processorpath: "DepSet | list[DataExpr] | None" = None,
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
        self.classpath = _as_classpath_list(classpath)
        self.processorpath = _as_classpath_list(processorpath)
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
        if (classpath or processorpath) and javac_worker is None:
            raise ValueError(
                "JarAction: classpath= and processorpath= currently require "
                "javac_worker= (java.javac_worker(graph)); pass one, or drop "
                "the classpath arguments"
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
            if self.processorpath:
                # Processors are executable build-time code. Their complete
                # bytes matter, not merely their public Java ABI.
                inputs["processorpath"] = _runtime_union(self.processorpath)
                parts.append("--processorpath {processorpath}")
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
    """Byte-lens view of the classpath jars (no content_key): execution
    depends on the *implementation*, so a test re-runs whenever any byte on
    its runtime classpath changes — the counterpart to `_classpath_union`,
    which uses the ABI lens for compilation. Strips any lens back to the plain
    byte-hashed atom."""
    atoms: list = []
    for expr in classpath:
        for atom in expr.atoms():
            if isinstance(atom, FileData):
                atoms.append(FileData(atom.path))  # drop any lens
            elif isinstance(atom, GlobData):
                atoms.append(GlobData(atom.pattern.template, atom.exclude))
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
        classpath: "DepSet | list[DataExpr]",
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
        self.classpath = _as_classpath_list(classpath) or []
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
