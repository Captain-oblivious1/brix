"""Typed C++ actions.

Strongly typed, ergonomic declarations — no magic string keys — that lower
into the same generic action/data model as `ExternalProcessAction` when
added to a graph. Raw and typed forms compose freely.

    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/obj/{name}.o"),
            compiler=data.tool("c++"),
            compile_options=["-O2", "-std=c++20"],
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/bin/app"),
            linker=data.tool("c++"),
        ),
    )
"""

from __future__ import annotations

from .actions import Action, ExpandAll, ExternalProcessAction
from .data import DataExpr, FileData, GlobData, ToolData, ValueData


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


def _single_binding(collection: GlobData, what: str) -> str:
    names = collection.pattern.binding_names
    if len(names) != 1:
        raise ValueError(
            f"{what} must have exactly one {{binding}}, got {collection.pattern.template!r}"
        )
    return names[0]


class CompileSourceAction(Action):
    """Compile every matching source into its object file, in parallel.

    Lowers to an ExpandAll over ``source_files`` whose child compiles one
    translation unit; ``source_files`` and ``object_files`` must share one
    binding name (e.g. ``src/{name}.cpp`` → ``build/obj/{name}.o``).
    """

    def __init__(
        self,
        *,
        source_files: GlobData,
        object_files: GlobData,
        compiler: ToolData | None = None,
        compile_options: list[str] | DataExpr | None = None,
    ) -> None:
        source_binding = _single_binding(source_files, "source_files")
        object_binding = _single_binding(object_files, "object_files")
        if source_binding != object_binding:
            raise ValueError(
                f"source_files binds {{{source_binding}}} but object_files "
                f"binds {{{object_binding}}}; they must match"
            )
        self.source_files = source_files
        self.object_files = object_files
        self.compiler = compiler if compiler is not None else ToolData("c++")
        self.compile_options = compile_options
        self._binding = source_binding

    def lower(self) -> list[Action]:
        name = self._binding
        obj_template = self.object_files.pattern.template
        depfile = f"{obj_template}.d"
        return [
            ExpandAll(
                element_name=name,
                collection=self.source_files,
                child=ExternalProcessAction(
                    name=f"compile {{{name}}}",
                    # -MMD writes user-header (not system-header) prerequisites;
                    # -MF puts the depfile where the incremental layer scans it,
                    # so editing an #included header re-stales this compile.
                    command_line=(
                        "{compiler} {options} -MMD -MF {depfile} -c {source} -o {object}"
                    ),
                    inputs={
                        "compiler": self.compiler,
                        "options": _options_expr(self.compile_options),
                        "source": FileData(self.source_files.pattern.template),
                    },
                    outputs={
                        "object": FileData(obj_template),
                        "depfile": FileData(depfile),
                    },
                    depfile=depfile,
                ),
            )
        ]


class LinkApplicationAction(Action):
    """Link objects (or any file collection) into one application."""

    def __init__(
        self,
        *,
        object_files: DataExpr,
        output_file: FileData,
        linker: ToolData | None = None,
        link_options: list[str] | DataExpr | None = None,
    ) -> None:
        self.object_files = object_files
        self.output_file = output_file
        self.linker = linker if linker is not None else ToolData("c++")
        self.link_options = link_options

    def lower(self) -> list[Action]:
        return [
            ExternalProcessAction(
                name=f"link {self.output_file.path}",
                command_line="{linker} {options} {objects} -o {output}",
                inputs={
                    "linker": self.linker,
                    "options": _options_expr(self.link_options),
                    "objects": self.object_files,
                },
                outputs={"output": self.output_file},
            )
        ]


class ArchiveAction(Action):
    """Bundle object files into a static library with ``ar``.

    Uses ``ar Dcrs``: **D**eterministic (no timestamps/uids, so the archive
    is byte-reproducible — its content hash is stable when the objects are,
    which keeps dependents from re-linking spuriously), **c**reate,
    **r**eplace, and build the **s**ymbol index. Link against the result by
    passing it as one of `LinkApplicationAction`'s ``object_files`` (the
    linker pulls the members it needs).

    Note on removed sources: ``ar`` *updates* an existing archive rather than
    rebuilding it. Under the atomic layer (the recommended stack) the archive
    is written into fresh staging each build, so a deleted source cannot
    leave a stale member. Without atomic, run ``clean`` after removing a
    source that fed the library.
    """

    def __init__(
        self,
        *,
        object_files: DataExpr,
        output_file: FileData,
        ar: ToolData | None = None,
    ) -> None:
        self.object_files = object_files
        self.output_file = output_file
        self.ar = ar if ar is not None else ToolData("ar")

    def lower(self) -> list[Action]:
        return [
            ExternalProcessAction(
                name=f"ar {self.output_file.path}",
                command_line="{ar} Dcrs {output} {objects}",
                inputs={"ar": self.ar, "objects": self.object_files},
                outputs={"output": self.output_file},
            )
        ]
