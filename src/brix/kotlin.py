"""Kotlin compilation via ``kotlinc``, mirroring ``brix.java.JarAction``.

Two things make Kotlin different from ``javac`` and shape this module:

1. **kotlinc compiles a whole module at once.** There is no per-file object;
   one edited source recompiles the module and rewrites every ``.class``. That
   is correct, not a regression — the action declares all sources as one input
   set and the whole classes tree as its output.

2. **Framework classes are ``final`` by default.** Spring proxies its
   ``@Configuration``/``@Component``/``@Transactional`` beans by subclassing
   them, which a ``final`` class silently defeats *at run time* (it compiles
   clean, then fails to boot). Gradle's ``kotlin("plugin.spring")`` fixes this
   by running the *all-open* compiler plugin with its ``spring`` preset, which
   makes classes carrying those annotations open. This module reproduces that
   exactly via ``spring_allopen=`` — omitting it is the trap, so the helper
   makes it a first-class argument rather than something you assemble by hand.

Classpath handling follows the ``brix.java`` precedent for a JVM tool that is
*not* the warm worker (``JUnitTestAction``'s run step): kotlinc's ``-classpath``
takes a single ``File.pathSeparator``-joined token, so the resolved jar paths
are joined into one value for the command line, while the same jars are
declared as **kotlin-abi**-lensed fingerprint inputs so a dependency change
recompiles the module exactly when it can matter.

The classpath is lensed through ``kotlin-abi``, **not** the ``jvm-abi`` lens
``java.JarAction`` uses: a Kotlin ``inline fun`` copies its body — line-number
tables and nested synthetic classes included — into consumers' bytecode at
compile time, so the "body is not part of the interface" assumption behind
``jvm-abi`` is unsound for a consumed Kotlin module (it once let brix skip a
recompile whose output had actually changed — a stale result, the worst build
failure). ``kotlin-abi`` (brix/kotlinabi.py) keeps jvm-abi's skip for classes
without inline members and counts inline-carrying classes by whole-class
bytes, failing closed on anything it will not vouch for: implementation-only
edits in inline-free classes no longer recompile dependents, and inline
changes always do.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .actions import Action, ExternalProcessAction
from .data import DataAtom, DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData
from .java import (
    _as_classpath_list,
    _classpath_paths,
    _options_expr,
)

if TYPE_CHECKING:
    from .deps import DepSet
    from .graph import Graph
    from .workers import WorkerSpec

# The kotlinc worker bootstrap source (a small in-process K2JVMCompiler
# wrapper, reflection-only so it compiles with plain javac); see
# `kotlinc_worker` and src/brix/workers.py.
_WORKER_SOURCE = Path(__file__).parent / "_java" / "KotlincWorker.java"

# The all-open plugin's built-in preset that mirrors Gradle's
# kotlin("plugin.spring"): open every class annotated (directly or through a
# meta-annotation) with @Component, @Async, @Transactional, @Cacheable, or
# @SpringBootTest.
_SPRING_PRESET = "plugin:org.jetbrains.kotlin.allopen:preset=spring"


# Public API surface (curated for documentation and as a contract).
__all__ = ['JarAction', 'kotlinc_worker', 'abi', 'KotlinAbiFile', 'KotlinAbiGlob']


class KotlinAbiFile(FileData):
    """A jar/class compared through the Kotlin ABI lens: jvm-abi semantics,
    except classes whose Kotlin metadata declares inline members (whose
    implementation compiles into consumers) count by whole-class bytes.
    ``brix.kotlin`` owns this lens — the core dispatches the ``kotlin-abi``
    name through its content-key registry (docs/content-key.md)."""

    content_key = "kotlin-abi"


class KotlinAbiGlob(GlobData):
    """The glob counterpart of `KotlinAbiFile`: the Kotlin ABI lens applied to
    every matched jar/class."""

    content_key = "kotlin-abi"


def abi(expr: DataExpr) -> DataExpr:
    """View a data expression through the Kotlin ABI lens: every plain
    file/glob atom becomes its `KotlinAbiFile`/`KotlinAbiGlob` counterpart.
    Non-file atoms (tools, values) and atoms that already carry a lens pass
    through unchanged. Safe on mixed classpaths: a plain Java jar has no
    Kotlin metadata, so the lens degrades to exactly jvm-abi for it."""
    lensed: list[DataAtom] = []
    for atom in expr.atoms():
        if type(atom) is FileData:
            lensed.append(KotlinAbiFile(atom.path))
        elif type(atom) is GlobData:
            lensed.append(KotlinAbiGlob(atom.pattern.template, atom.exclude))
        else:
            lensed.append(atom)
    return lensed[0] if len(lensed) == 1 else DataUnion(*lensed)


def _classpath_union(classpath: list[DataExpr]) -> DataExpr:
    """Flatten classpath entries into one kotlin-abi-lensed expression: a
    dependent recompiles when a dependency's Kotlin ABI — API surface plus
    every inline-carrying class — changes, and skips otherwise."""
    return abi(DataUnion(*classpath))


def kotlinc_worker(
    graph: "Graph",
    *,
    kotlin_compiler: str | FileData | ToolData,
    java: ToolData | None = None,
    javac: ToolData | None = None,
    jar_tool: ToolData | None = None,
    work_dir: str = "build/.brix-kotlin",
) -> "WorkerSpec":
    """Add the kotlinc worker bootstrap to ``graph`` and return its `WorkerSpec`.

    A cold ``kotlinc`` pays JVM startup *and* loads/JIT-warms the whole Kotlin
    compiler on every call, so keeping it warm between compilations is the big
    edit-loop win — the Kotlin analogue of ``java.javac_worker``. Pass the
    returned spec to ``JarAction(kotlinc_worker=...)`` to route module
    compilation through the warm worker instead of spawning ``kotlinc``.

    ``kotlin_compiler`` is the distribution's ``lib/kotlin-compiler.jar`` (the
    self-contained compiler the worker instantiates in-process by reflection);
    the worker itself is plain Java, so it compiles with ``javac`` alone. Note
    the compile-vs-launch JDK split: the worker is compiled with ``--release
    17`` so it runs on whatever ``java`` launches it, which must be a JDK the
    Kotlin compiler supports (its version, not the worker's, is what compiles
    your sources).

    Idempotent: called more than once on a graph, it adds the bootstrap only
    once and returns the same spec, so many `JarAction`s share one worker jar.
    """
    from .workers import WorkerSpec

    cached = getattr(graph, "_brix_kotlinc_worker", None)
    if cached is not None:
        return cached

    java = java if java is not None else ToolData("java")
    javac = javac if javac is not None else ToolData("javac")
    jar_tool = jar_tool if jar_tool is not None else ToolData("jar")
    classes_dir = f"{work_dir}/worker-classes"
    worker_jar = f"{work_dir}/kotlinc-worker.jar"
    classes = GlobData(f"{classes_dir}/{{name}}.class")

    graph.add(
        ExternalProcessAction(
            name="kotlinc-worker: compile",
            # --release 17 decouples the compile JDK from the (Kotlin-compatible)
            # launch JDK: the worker class then runs on any java >= 17.
            command_line="{javac} --release 17 -d {classes_dir} {source}",
            inputs={"javac": javac, "source": FileData(str(_WORKER_SOURCE.resolve()))},
            outputs={"classes_dir": FileData(classes_dir), "classes": classes},
        ),
        ExternalProcessAction(
            name="kotlinc-worker: jar",
            command_line="{jar} cfe {out} KotlincWorker -C {classes_dir} .",
            inputs={
                "jar": jar_tool,
                "classes_dir": FileData(classes_dir),
                "classes": GlobData(f"{classes_dir}/{{name}}.class"),
            },
            outputs={"out": FileData(worker_jar)},
        ),
    )
    if isinstance(kotlin_compiler, DataExpr):
        compiler_expr: DataExpr = kotlin_compiler
    else:
        compiler_expr = FileData(str(kotlin_compiler))
    spec = WorkerSpec(
        name="kotlinc",
        # -cp joins the worker jar and the compiler jar in one token: the
        # placeholders are adjacent (no whitespace), so {sep} renders the
        # platform path separator between the two overlay-resolved paths.
        command_line="{java} -cp {worker_jar}{sep}{kotlin_compiler} KotlincWorker",
        inputs={
            "java": java,
            "worker_jar": FileData(worker_jar),
            "sep": ValueData(os.pathsep),
            "kotlin_compiler": compiler_expr,
        },
    )
    graph._brix_kotlinc_worker = spec  # type: ignore[attr-defined]
    return spec


class JarAction(Action):
    """Compile a whole Kotlin module with ``kotlinc`` and package it into a jar.

    ``spring_allopen`` is the path to the distribution's
    ``allopen-compiler-plugin.jar``; when given, the Spring all-open preset is
    applied (the equivalent of Gradle's ``kotlin("plugin.spring")``). The
    plugin jar is a byte-lens input, so a kotlinc upgrade recompiles.
    """

    def __init__(
        self,
        *,
        source_files: DataExpr,
        output_file: FileData,
        kotlinc: ToolData | None = None,
        jar_tool: ToolData | None = None,
        classes_dir: str = "build/kotlin-classes",
        classpath: "DepSet | list[DataExpr] | None" = None,
        jvm_target: str = "17",
        spring_allopen: str | None = None,
        options: list[str] | DataExpr | None = None,
        entry_point: str | None = None,
        resource_dir: str | None = None,
        kotlinc_worker: "WorkerSpec | None" = None,
    ) -> None:
        self.source_files = source_files
        self.output_file = output_file
        self.kotlinc = kotlinc if kotlinc is not None else ToolData("kotlinc")
        self.jar_tool = jar_tool if jar_tool is not None else ToolData("jar")
        self.classes_dir = classes_dir
        self.classpath = _as_classpath_list(classpath)
        self.jvm_target = jvm_target
        self.spring_allopen = spring_allopen
        self.options = options
        self.entry_point = entry_point
        self.resource_dir = resource_dir
        # When set, the compile step routes through the warm kotlinc worker
        # (see kotlin.kotlinc_worker); otherwise it spawns kotlinc. Both paths
        # lens the classpath through kotlin-abi identically — the worker is
        # pure transport, so what recompiles a dependent does not change.
        self.kotlinc_worker = kotlinc_worker

    def lower(self) -> list[Action]:
        classes = GlobData(f"{self.classes_dir}/{{name}}.class")

        if self.kotlinc_worker is not None:
            compile_kotlin = self._compile_via_worker(classes)
        else:
            compile_kotlin = self._compile_via_spawn(classes)

        if self.entry_point:
            jar_command = "{jar} cfe {output} {entry_point} -C {classes_dir} ."
            jar_inputs: dict[str, DataExpr] = {"entry_point": ValueData(self.entry_point)}
        else:
            jar_command = "{jar} cf {output} -C {classes_dir} ."
            jar_inputs = {}
        if self.resource_dir is not None:
            jar_command += " -C {resource_dir} ."
            jar_inputs["resource_dir"] = FileData(self.resource_dir)
            jar_inputs["resources"] = GlobData(f"{self.resource_dir}/{{name}}")
        package = ExternalProcessAction(
            name=f"jar {self.output_file.path}",
            command_line=jar_command,
            inputs={
                "jar": self.jar_tool,
                "classes_dir": FileData(self.classes_dir),
                "classes": classes,
                **jar_inputs,
            },
            outputs={"output": self.output_file},
        )
        return [compile_kotlin, package]

    def _compile_via_spawn(self, classes: GlobData) -> ExternalProcessAction:
        parts = ["{kotlinc}", "-jvm-target {jvm_target}"]
        inputs: dict[str, DataExpr] = {
            "kotlinc": self.kotlinc,
            "jvm_target": ValueData(self.jvm_target),
            "options": _options_expr(self.options),
        }
        if self.classpath:
            # kotlinc wants one pathsep-joined -classpath token, so the resolved
            # jar paths are baked into a value; the same jars ride along as a
            # kotlin-abi-lensed fingerprint input (unreferenced by the template
            # but still hashed and dependency-inferred, like JUnitTestAction's
            # run step).
            #
            # kotlin-abi, NOT the jvm-abi lens java.JarAction uses: a Kotlin
            # `inline fun` copies its body into this module's bytecode at
            # compile time, so jvm-abi's "body is not interface" assumption
            # would let brix skip recompiling after an inline change that DID
            # alter our output — a stale-result bug caught live (3746bd1).
            # kotlin-abi byte-counts exactly the inline-carrying classes and
            # keeps the ABI skip for the rest (see brix/kotlinabi.py).
            joined = os.pathsep.join(_classpath_paths(self.classpath))
            inputs["classpath"] = ValueData(joined)
            inputs["classpath_track"] = _classpath_union(self.classpath)
            parts.append("-classpath {classpath}")
        if self.spring_allopen is not None:
            # -Xplugin=<jar> glues the path onto the flag (one token); the preset
            # spec is a separate -P argument. The plugin jar is byte-tracked.
            inputs["allopen"] = FileData(self.spring_allopen)
            inputs["preset"] = ValueData(_SPRING_PRESET)
            parts.append("-Xplugin={allopen} -P {preset}")
        parts += ["{options}", "-d {classes_dir}", "{sources}"]
        inputs["sources"] = self.source_files
        return ExternalProcessAction(
            name=f"kotlinc -> {self.classes_dir}",
            command_line=" ".join(parts),
            inputs=inputs,
            outputs={"classes_dir": FileData(self.classes_dir), "classes": classes},
        )

    def _compile_via_worker(self, classes: GlobData) -> ExternalProcessAction:
        # The warm worker joins the classpath itself and resolves the plugin
        # path, so — unlike the spawn path — the classpath is a plain
        # kotlin-abi-lensed input referenced by the template (no baked value),
        # and the plugin jar travels as an explicit path arg. jvm-target and
        # user options ride --options; the worker passes them through verbatim.
        # The classpath lens is identical to the spawn path: the worker is pure
        # transport, so what recompiles a dependent is unchanged (see
        # _compile_via_spawn for the kotlin-abi rationale).
        inputs: dict[str, DataExpr] = {
            "jvm_target": ValueData(self.jvm_target),
            "options": _options_expr(self.options),
            "sources": self.source_files,
        }
        parts = ["--options -jvm-target {jvm_target} {options}"]
        if self.classpath:
            inputs["classpath"] = _classpath_union(self.classpath)
            parts.append("--classpath {classpath}")
        if self.spring_allopen is not None:
            inputs["allopen"] = FileData(self.spring_allopen)
            inputs["preset"] = ValueData(_SPRING_PRESET)
            parts.append("--plugin {allopen} --plugin-option {preset}")
        parts += ["-d {classes_dir}", "--sources {sources}"]
        return ExternalProcessAction(
            name=f"kotlinc(worker) -> {self.classes_dir}",
            worker=self.kotlinc_worker,
            command_line=" ".join(parts),
            inputs=inputs,
            outputs={"classes_dir": FileData(self.classes_dir), "classes": classes},
        )
