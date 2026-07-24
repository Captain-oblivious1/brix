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

import subprocess

from .actions import Action, ExpandAll, ExternalProcessAction
from .data import DataExpr, FileData, GlobData, ToolData, ValueData
from .deps import DepSet
from .env import SYSTEM

# Behaviour-affecting C/C++ toolchain environment, tracked (SYSTEM) so a change
# re-stales the compile/link — the phase-3 per-language default overlay
# (docs/environment-recording.md). CPATH/*_INCLUDE_PATH add include search dirs,
# LIBRARY_PATH adds link search dirs, and SOURCE_DATE_EPOCH is embedded for
# reproducibility — each can change output invisibly. (LANG/locale is deferred to
# the base-map increment; it is usually set and would make fingerprints noisy.)
_TOOLCHAIN_ENV = {
    "CPATH": SYSTEM,
    "C_INCLUDE_PATH": SYSTEM,
    "CPLUS_INCLUDE_PATH": SYSTEM,
    "LIBRARY_PATH": SYSTEM,
    "SOURCE_DATE_EPOCH": SYSTEM,
}


# Public API surface (curated for documentation and as a contract).
__all__ = [
    'CompileSourceAction',
    'LinkApplicationAction',
    'ArchiveAction',
    'RunTestAction',
    'DoxygenAction',
    'pkg_config',
]


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


# --- dependency resolution: query pkg-config for installed system libraries ---
#
# brix does not resolve dependencies (docs/dependencies.md) — pkg_config drives
# the system's pkg-config and consumes its flags. Unlike a jar classpath, system
# libraries are referenced by `-l<name>` (the linker locates the file), so they
# are carried as flags, not tracked file inputs — a system-lib upgrade via the
# package manager is outside the project and is not fingerprinted (a Conan/vcpkg
# adapter that yields concrete .a/.so paths would populate `artifacts`, which the
# link step *does* track).


def _pkg_config_query(pkg_config: str, flag: str, names: list[str]) -> list[str]:
    """Run `pkg-config <flag> <names...>` and split its output into tokens.
    Isolated so tests can substitute it without a live pkg-config."""
    out = subprocess.run(
        [pkg_config, flag, *names], check=True, capture_output=True, text=True
    ).stdout
    return out.split()


def pkg_config(names: list[str], *, pkg_config: str = "pkg-config") -> DepSet:
    """Resolve installed system libraries via ``pkg-config`` into a `DepSet`:
    ``--cflags`` become the set's compile flags (``-I``/``-D``) and ``--libs`` its
    link flags (``-L``/``-l``). Feed it to ``CompileSourceAction(deps=...)`` and
    ``LinkApplicationAction(deps=...)``."""
    cflags = _pkg_config_query(pkg_config, "--cflags", names)
    libs = _pkg_config_query(pkg_config, "--libs", names)
    return DepSet(compile_flags=tuple(cflags), link_flags=tuple(libs))


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
        deps: DepSet | None = None,
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
        # A DepSet's compile flags (-I/-D from e.g. cpp.pkg_config) are added to
        # every translation unit's command as a separate value, so they compose
        # with any compile_options form and re-stale the compile when they change.
        self.deps = deps
        self._binding = source_binding

    def lower(self) -> list[Action]:
        name = self._binding
        obj_template = self.object_files.pattern.template
        depfile = f"{obj_template}.d"
        dep_cflags = " ".join(self.deps.compile_flags) if self.deps is not None else ""
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
                        "{compiler} {options} {dep_cflags} -MMD -MF {depfile} "
                        "-c {source} -o {object}"
                    ),
                    inputs={
                        "compiler": self.compiler,
                        "options": _options_expr(self.compile_options),
                        "dep_cflags": ValueData(dep_cflags),
                        "source": FileData(self.source_files.pattern.template),
                    },
                    outputs={
                        "object": FileData(obj_template),
                        "depfile": FileData(depfile),
                    },
                    depfile=depfile,
                    env=_TOOLCHAIN_ENV,
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
        deps: DepSet | None = None,
    ) -> None:
        self.object_files = object_files
        self.output_file = output_file
        self.linker = linker if linker is not None else ToolData("c++")
        self.link_options = link_options
        # A DepSet's link artifacts (.a/.so, tracked as byte-lens inputs) and
        # link flags (-L/-l) go *after* the objects — link order is load-bearing.
        self.deps = deps

    def lower(self) -> list[Action]:
        from .data import DataUnion

        deps = self.deps
        dep_libs = DataUnion(*(deps.files() if deps is not None else []))
        dep_lflags = " ".join(deps.link_flags) if deps is not None else ""
        return [
            ExternalProcessAction(
                name=f"link {self.output_file.path}",
                command_line=(
                    "{linker} {options} {objects} {dep_libs} {dep_lflags} -o {output}"
                ),
                inputs={
                    "linker": self.linker,
                    "options": _options_expr(self.link_options),
                    "objects": self.object_files,
                    "dep_libs": dep_libs,  # tracked artifact files (empty for pkg-config)
                    "dep_lflags": ValueData(dep_lflags),
                },
                outputs={"output": self.output_file},
                env=_TOOLCHAIN_ENV,
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


class RunTestAction(Action):
    """Run a compiled test executable as a tracked action.

    The binary is a byte-lens input and the action declares **no outputs**, so it
    replays on its input fingerprint: a passing run is cached like any result, a
    failing one re-runs until its inputs change (the same contract as
    ``java.JUnitTestAction``'s run step).

    Because a test binary is itself a build product, brix's existing header
    (``-MMD``) and link tracking already make it stale exactly when code it
    *statically* links changes. So running **one ``RunTestAction`` per test
    binary is per-binary impact selection with no extra machinery**: edit a
    source, and only the binaries that link it re-run — every other test replays
    from cache. The selection unit (a binary) equals the run unit, so the
    incremental engine does the selecting.

    The one dependency this cannot see is code loaded at run time — a ``dlopen``'d
    plugin ``.so``, a data file read by path — which is not in the link graph and
    so does not re-stale the binary. Declare those as ``runtime_inputs`` (tracked
    byte-lens) so a change to them re-runs the test. ``brix.testimpact_cpp``
    detects the ``dlopen`` case from an object's symbols; a test that loads code
    by a name computed at run time and cannot enumerate it belongs in a suite that
    always runs.
    """

    def __init__(
        self,
        *,
        test_binary: str | FileData,
        name: str | None = None,
        args: list[str] | DataExpr | None = None,
        runtime_inputs: "list[str | DataExpr] | None" = None,
        runner: ToolData | None = None,
    ) -> None:
        self.test_binary = (
            test_binary if isinstance(test_binary, FileData) else FileData(test_binary)
        )
        self.name = name or f"run {self.test_binary.path}"
        self.args = args
        self.runtime_inputs = list(runtime_inputs or [])
        # An optional wrapper (e.g. a sandbox or `valgrind`); by default the
        # binary is executed directly. A leading "./" is added at render so a
        # repo-relative path is found without a PATH lookup.
        self.runner = runner

    def lower(self) -> list[Action]:
        inputs: dict[str, DataExpr] = {"binary": self.test_binary}
        for i, extra in enumerate(self.runtime_inputs):
            inputs[f"runtime{i}"] = (
                extra if isinstance(extra, DataExpr) else FileData(extra)
            )
        inputs["args"] = _options_expr(self.args)
        if self.runner is not None:
            inputs["runner"] = self.runner
            command = "{runner} {binary} {args}"
        else:
            # Execute the binary by path (it has a path separator, so no PATH
            # lookup); "./"-relative works under the workspace root.
            command = "./{binary} {args}"
        return [
            ExternalProcessAction(
                name=self.name,
                command_line=command,
                inputs=inputs,
                outputs={},  # exit code only — replays on the input fingerprint
            )
        ]


class DoxygenAction(Action):
    """Generate HTML API docs for a project's *own* C/C++ sources with
    ``doxygen``.

    Documentation *of the user's code*, run as an ordinary tracked step.
    doxygen is config-driven (``doxygen <Doxyfile>``), so unlike javadoc/typedoc
    there are no source arguments on the command line — the ``Doxyfile`` names
    the ``INPUT`` directories and the ``OUTPUT_DIRECTORY``. brix cannot read
    those out of the config, so you declare both to it: ``source_files`` (the
    inputs, tracked so an edited header rebuilds the docs) and ``output_dir``
    (which **must** match the Doxyfile's ``OUTPUT_DIRECTORY``/``HTML_OUTPUT``, so
    brix tracks and stages the tree doxygen actually writes). The config file is
    itself an input — changing a Doxyfile setting regenerates.

    ::

        cpp.DoxygenAction(
            config="Doxyfile",
            source_files=data.glob("src/{name}.h") + data.glob("src/{name}.cpp"),
            output_dir="build/docs/html",
        )
    """

    def __init__(
        self,
        *,
        config: str,
        source_files: DataExpr,
        output_dir: str,
        doxygen: ToolData | None = None,
    ) -> None:
        self.config = config
        self.source_files = source_files
        self.output_dir = output_dir
        self.doxygen = doxygen if doxygen is not None else ToolData("doxygen")

    def lower(self) -> list[Action]:
        # The sources are declared inputs but not command arguments — doxygen
        # reads them via the Doxyfile's INPUT. A recursive `{name}` glob under
        # output_dir captures the whole emitted tree (mirrors JavadocAction).
        return [
            ExternalProcessAction(
                name=f"doxygen -> {self.output_dir}",
                command_line="{doxygen} {config}",
                inputs={
                    "doxygen": self.doxygen,
                    "config": FileData(self.config),
                    "sources": self.source_files,
                },
                outputs={
                    "output_dir": FileData(self.output_dir),
                    "files": GlobData(f"{self.output_dir}/{{name}}"),
                },
            )
        ]
