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
tracking uses a ``{name}.class`` glob, which covers default-package
classes; packaged (nested-directory) class tracking needs regex output
collections — future work.
"""

from __future__ import annotations

from .actions import Action, ExternalProcessAction
from .data import DataExpr, FileData, GlobData, ToolData, ValueData


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


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
    ) -> None:
        self.source_files = source_files
        self.output_file = output_file
        self.javac = javac if javac is not None else ToolData("javac")
        self.jar_tool = jar_tool if jar_tool is not None else ToolData("jar")
        self.options = options
        self.classes_dir = classes_dir
        self.entry_point = entry_point

    def lower(self) -> list[Action]:
        classes = GlobData(f"{self.classes_dir}/{{name}}.class")
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
        package = ExternalProcessAction(
            name=f"jar {self.output_file.path}",
            command_line=jar_command,
            inputs={
                "jar": self.jar_tool,
                "classes_dir": FileData(self.classes_dir),
                "classes": GlobData(f"{self.classes_dir}/{{name}}.class"),
                **jar_inputs,
            },
            outputs={"output": self.output_file},
        )
        return [compile_java, package]
