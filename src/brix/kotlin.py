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
declared as ABI-lensed fingerprint inputs so a dependency's API change
recompiles the module and an implementation-only change does not.

The ABI lens is applied to the *compile classpath* (external dependencies),
never to this module's own outputs: a Kotlin ``inline fun`` copies its body
into cross-module call sites, so the "body is not part of the interface"
assumption behind ``jvm-abi`` is unsound for a Kotlin module *consumed* by
another. Single-module builds have no such consumer; multi-module Kotlin needs
a soundness-checked Kotlin ABI lens before any downstream skipping (tracked as
future work — see docs/roadmap.md).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .actions import Action, ExternalProcessAction
from .data import DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData
from .java import (
    _as_classpath_list,
    _classpath_paths,
    _classpath_union,
    _options_expr,
)

if TYPE_CHECKING:
    from .deps import DepSet

# The all-open plugin's built-in preset that mirrors Gradle's
# kotlin("plugin.spring"): open every class annotated (directly or through a
# meta-annotation) with @Component, @Async, @Transactional, @Cacheable, or
# @SpringBootTest.
_SPRING_PRESET = "plugin:org.jetbrains.kotlin.allopen:preset=spring"


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

    def lower(self) -> list[Action]:
        classes = GlobData(f"{self.classes_dir}/{{name}}.class")

        parts = ["{kotlinc}", "-jvm-target {jvm_target}"]
        inputs: dict[str, DataExpr] = {
            "kotlinc": self.kotlinc,
            "jvm_target": ValueData(self.jvm_target),
            "options": _options_expr(self.options),
        }
        if self.classpath:
            # kotlinc wants one pathsep-joined -classpath token, so the resolved
            # jar paths are baked into a value; the same jars ride along as an
            # ABI-lensed fingerprint input (unreferenced by the template but
            # still hashed and dependency-inferred, like JUnitTestAction's run).
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
        compile_kotlin = ExternalProcessAction(
            name=f"kotlinc -> {self.classes_dir}",
            command_line=" ".join(parts),
            inputs=inputs,
            outputs={"classes_dir": FileData(self.classes_dir), "classes": classes},
        )

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
