"""Typed TypeScript actions.

Like `brix.java` and `brix.cpp`, these lower into the generic action/data model
when added to a graph — one underlying representation, so TypeScript composes
freely with the other languages (a code generator can emit `.ts` that feeds a
`ts.CompileAction`; a `.js` bundle can feed a later step).

    ts.CompileAction(
        sources=data.glob("src/{name}.ts"),
        out_dir="dist",
        options=["--module", "nodenext", "--target", "es2022", "--strict"],
    )

`tsc` compiles a set of `.ts` sources into an `outDir` of `.js` (and, with
declaration emit, `.d.ts`). The action declares the sources as inputs and the
emitted `.js`/`.d.ts` as recursive `{name}` globs under ``out_dir`` (bare
``{name}`` matches across ``/``, so nested modules — ``sub/mod.js`` — are
tracked, not just top-level ones), so brix owns incrementality: it re-runs
``tsc`` only when a source or a tracked dependency actually changes, rather than
delegating to ``tsc --build``'s own up-to-date check.

**The cross-module lens.** A module that type-checks against another module's
built declarations declares them via ``dependencies=`` — the emitted upstream
``.d.ts`` files, viewed through the **ts-dts** lens (`brix.ts` owns this typed
atom, as `brix.java` owns `AbiFile`). Because ``tsc``'s declaration emit writes
only the public API, an implementation-only change upstream produces a
byte-identical ``.d.ts`` and the ts-dts fingerprint is unchanged, so the
dependent **replays** instead of recompiling — the TypeScript counterpart of the
jvm-abi shortcut (docs/content-key.md). The upstream ``.d.ts`` are tracked
fingerprint inputs; ``tsc``'s own module resolution locates them on disk (they
sit in the upstream ``out_dir`` the dependent imports from), so no classpath-style
flag is needed.

**Automatic import tracking.** The declared ``sources`` glob covers a module's
own tree, but a TS ``import`` can reach a shared ``.ts`` *outside* it (a common
``utils/`` file, an ambient ``.d.ts``). Left undiscovered, editing that file
would leave the module stale — the TS version of the C/C++ header problem. So by
default (``track_imports=True``) brix wraps ``tsc`` in a ``--listFiles`` recorder
that writes a Makefile depfile of every workspace ``.ts``/``.d.ts`` the compile
actually read; the incremental layer scans it exactly like a ``cc -MMD`` one, so
editing a deeply-imported source re-stales the compile. It is complementary to
``dependencies=``: a *declared* upstream ``.d.ts`` is tracked through the ts-dts
lens (impl-only upstream change → replay), and its directory is excluded from
the depfile so that byte-tracking never overrides the lens; the depfile is only
for the *undeclared* source imports the lens doesn't cover. Set
``track_imports=False`` for a bare single ``tsc`` spawn.

Scope for now: no warm compiler daemon (like `brix.cpp`, unlike `brix.java`'s
javac worker) and no npm/``node_modules`` resolver (that is the `ts.npm`-style
adapter of docs/dependencies.md, still planned). ``tsc`` is often not global;
pass ``tsc=data.tool(".../node_modules/.bin/tsc")`` to point at a local install.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .actions import Action, ExternalProcessAction
from .data import AssetData, DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData
from .deps import DepSet

# brix's tsc wrapper: runs the compile with --listFiles and records the files
# tsc actually read as a Makefile depfile (the TS counterpart of `cc -MMD`).
# A brix-shipped helper, referenced portably by logical asset name and resolved
# engine-side — never by a machine path, so the fingerprint is identical across
# machines and frontends (docs/asset-engine.md).
_LISTFILES_ASSET = "ts/tsc_listfiles.py"


# Public API surface (curated for documentation and as a contract).
__all__ = ['CompileAction', 'RunTestAction', 'TypedocAction', 'npm', 'dts', 'DtsFile', 'DtsGlob']


def _resolve_npm_types(base: Path, node_modules: str, name: str) -> list[str]:
    """The workspace-relative ``.d.ts`` files a package contributes: the
    package's own type tree plus its DefinitelyTyped ``@types/<name>`` tree, if
    present. Nested ``node_modules`` are skipped (modern npm hoists transitive
    deps to the top level, and descending would over-track wildly). Scoped names
    map to DefinitelyTyped's mangled directory (``@scope/pkg`` -> ``scope__pkg``).
    """
    at_types = name[1:].replace("/", "__") if name.startswith("@") else name
    candidates = [
        Path(node_modules) / name,
        Path(node_modules) / "@types" / at_types,
    ]
    out: list[str] = []
    for rel_dir in candidates:
        abs_dir = base / rel_dir
        if not abs_dir.is_dir():
            continue
        for p in sorted(abs_dir.rglob("*.d.ts")):
            if "node_modules" in p.relative_to(abs_dir).parts:
                continue  # a nested install; the top-level one is tracked already
            out.append(p.relative_to(base).as_posix())
    return out


def npm(
    names: list[str],
    *,
    node_modules: str = "node_modules",
    root: str | Path = ".",
) -> DepSet:
    """Resolve npm packages' TypeScript type declarations into a `DepSet`.

    brix does not run npm — you ``npm install`` and this adapter consumes the
    result (the `java.pom`/`cpp.pkg_config` pattern for the JS ecosystem). For
    each package it collects that package's ``.d.ts`` under ``node_modules``
    (and ``@types/<name>`` for DefinitelyTyped packages), so upgrading a
    dependency whose types changed re-stales the compile.

    ``tsc`` still resolves the ``import``s off the real filesystem at compile
    time; these are *tracked inputs*, not command arguments. Pass the result as
    ``ts.CompileAction(deps=…)`` — the consumer applies the **ts-dts** lens, so a
    comment-only change to a dependency's ``.d.ts`` lets the module replay while
    a real type change rebuilds it.

    Resolution reads the real filesystem now (per build, like the Maven
    adapters). Paths are returned relative to ``root`` (the workspace root the
    build runs from). Raises if a named package has no discoverable ``.d.ts``.
    """
    base = Path(root)
    artifacts: list[str] = []
    for name in names:
        found = _resolve_npm_types(base, node_modules, name)
        if not found:
            raise FileNotFoundError(
                f"ts.npm: no .d.ts type declarations for {name!r} under "
                f"{node_modules}/ — is it installed, and does it ship types "
                f"(or need @types/{name})?"
            )
        artifacts.extend(found)
    # Order-preserving de-dup: overlapping names (or a package listed twice)
    # otherwise repeat .d.ts. DepSet's constructor does not de-dup; `+` does.
    return DepSet(tuple(dict.fromkeys(artifacts)))


class DtsFile(FileData):
    """A `.d.ts` declaration file compared through the ts-dts lens, not its
    bytes: a consumer recompiles on a public-API change, not an
    implementation-only change or a doc-comment edit. ``brix.ts`` owns this lens
    — the core dispatches the ``ts-dts`` name through its content-key registry
    (docs/content-key.md)."""

    content_key = "ts-dts"


class DtsGlob(GlobData):
    """The glob counterpart of `DtsFile`: the ts-dts lens applied to every
    matched `.d.ts`."""

    content_key = "ts-dts"


def dts(expr: DataExpr) -> DataExpr:
    """View a data expression through the ts-dts lens: every plain file/glob atom
    becomes its `DtsFile`/`DtsGlob` counterpart, so fingerprints compare those
    declaration files by public surface. Non-file atoms (tools, values) and atoms
    that already carry a lens pass through unchanged. A module can depend on many
    upstream ``.d.ts``, so this applies across an expression's atoms."""
    lensed: list = []
    for atom in expr.atoms():
        if type(atom) is FileData:
            lensed.append(DtsFile(atom.path))
        elif type(atom) is GlobData:
            lensed.append(DtsGlob(atom.pattern.template, atom.exclude))
        else:
            lensed.append(atom)
    return lensed[0] if len(lensed) == 1 else DataUnion(*lensed)


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


class RunTestAction(Action):
    """Run one vitest test file as a tracked action.

    No file outputs, so it replays on its input fingerprint: a passing run is
    cached, a failing one re-runs until its inputs change (the same contract as
    ``cpp.RunTestAction`` and ``java.JUnitTestAction``'s run step).

    The byte-lens inputs are the test file plus ``sources`` — the workspace files
    it transitively imports. Editing any of them re-runs the test; editing a file
    outside its import closure does not. **Running one ``RunTestAction`` per test
    file is per-file impact selection**, done by the incremental engine, because
    the run unit (a test file) is the selection unit — the TS analogue of C++'s
    per-binary selection.

    Compute ``sources`` with ``brix.testimpact_ts`` — ``TsProvider.test_input_sets()``
    returns exactly this footprint per test, widening to the whole source set for
    a test that reaches a computed ``import()``/``require()`` or an unresolved
    import (the run-on-any-change fail-open).

    ``runner`` is the vitest executable (default ``ToolData("vitest")``; point it
    at ``node_modules/.bin/vitest`` for a local install).
    """

    def __init__(
        self,
        *,
        test_file: str | FileData,
        sources: "list[str | DataExpr] | None" = None,
        runner: ToolData | None = None,
        name: str | None = None,
        args: list[str] | DataExpr | None = None,
    ) -> None:
        self.test_file = (
            test_file if isinstance(test_file, FileData) else FileData(test_file)
        )
        self.sources = list(sources or [])
        self.runner = runner if runner is not None else ToolData("vitest")
        self.name = name or f"vitest {self.test_file.path}"
        self.args = args

    def lower(self) -> list[Action]:
        inputs: dict[str, DataExpr] = {
            "vitest": self.runner,
            "test": self.test_file,  # both the command arg and a byte-lens input
        }
        for i, src in enumerate(self.sources):
            inputs[f"src{i}"] = src if isinstance(src, DataExpr) else FileData(src)
        inputs["args"] = _options_expr(self.args)
        return [
            ExternalProcessAction(
                name=self.name,
                command_line="{vitest} run {test} {args}",
                inputs=inputs,
                outputs={},  # exit code only — replays on the input fingerprint
            )
        ]


class CompileAction(Action):
    """Compile a module's TypeScript sources with ``tsc`` into ``out_dir``.

    Emits ``.js`` for every source and, when ``declaration`` is set (the
    default, so the module is usable as a dependency), a ``.d.ts`` beside each.
    ``dependencies`` is the list of upstream modules' built ``.d.ts`` this module
    type-checks against; they are tracked through the **ts-dts** lens, so an
    implementation-only change upstream lets this module replay.
    """

    def __init__(
        self,
        *,
        sources: DataExpr,
        out_dir: str,
        tsc: ToolData | None = None,
        options: list[str] | DataExpr | None = None,
        declaration: bool = True,
        dependencies: list[DataExpr] | None = None,
        deps: DepSet | None = None,
        track_imports: bool = True,
        python: ToolData | None = None,
    ) -> None:
        self.sources = sources
        self.out_dir = out_dir
        self.tsc = tsc if tsc is not None else ToolData("tsc")
        self.options = options
        self.declaration = declaration
        # Discover the transitive import graph: run tsc with --listFiles and
        # record the workspace .ts/.d.ts it read as a depfile, so editing a
        # deeply-imported source that the declared `sources` glob doesn't cover
        # re-stales this compile (the TS answer to the C/C++ header problem).
        # On by default — a correctness feature, like C++'s always-on -MMD.
        self.track_imports = track_imports
        self.python = python if python is not None else ToolData(sys.executable)
        # Upstream modules' emitted .d.ts (ts-dts-lensed): fingerprint inputs
        # only. tsc resolves the imports to them on disk (they live in the
        # upstream out_dir this module imports from), so no classpath flag is
        # needed — declaring them as inputs is what makes brix rebuild this
        # module on an upstream API change and replay on an impl-only one.
        # `deps` is a resolved npm DepSet (ts.npm): its .d.ts artifacts join the
        # dependency list and are tracked exactly like an upstream module's
        # declaration — ts-dts-lensed, resolved on disk by tsc, and excluded from
        # the import-tracking depfile so the lens governs them (not byte-tracking).
        resolved_deps = list(deps.files()) if deps is not None else []
        self.dependencies = list(dependencies or []) + resolved_deps

    def lower(self) -> list[Action]:
        # Recursive `{name}` globs reach nested emit (`sub/mod.js`), like
        # java's `{name}.class`. `.d.ts` is not matched by the `.js` glob
        # (different suffix), so the two output sets don't overlap.
        js = GlobData(f"{self.out_dir}/{{name}}.js")
        outputs: dict[str, DataExpr] = {
            "out_dir": FileData(self.out_dir),
            "js": js,
        }
        decl_flag = ""
        if self.declaration:
            decl_flag = "--declaration "
            outputs["dts"] = GlobData(f"{self.out_dir}/{{name}}.d.ts")

        inputs: dict[str, DataExpr] = {
            "tsc": self.tsc,
            "options": _options_expr(self.options),
            "sources": self.sources,
        }
        self_resolved: tuple[str, ...] = ()
        if self.dependencies:
            # ts-dts-lensed; not a command placeholder (tsc finds them on disk),
            # but declared so the fingerprint tracks the upstream API surface.
            inputs["types"] = dts(DataUnion(*self.dependencies))
            # tsc resolves these `import`s off the real filesystem, so they are
            # invisible to atomic staging — flag them so the atomic builder
            # refuses this build with one uniform message rather than silently
            # reading a stale declaration (docs/capability-audit.md).
            self_resolved = ("types",)

        tsc_command = f"{{tsc}} {{options}} {decl_flag}--outDir {{out_dir}} {{sources}}"
        if not self.track_imports:
            return [
                ExternalProcessAction(
                    name=f"tsc -> {self.out_dir}",
                    command_line=tsc_command,
                    inputs=inputs,
                    outputs=outputs,
                    self_resolved_inputs=self_resolved,
                )
            ]

        # Wrap tsc in brix's --listFiles recorder, writing a Makefile depfile
        # the incremental layer scans like any C/C++ one. Exclude this module's
        # own out_dir (its emit is not an input) and each declared dependency's
        # directory (those .d.ts are tracked through the ts-dts lens above;
        # byte-tracking them here would defeat the impl-only-change replay).
        depfile = f"{self.out_dir}/.brix-tsdeps.d"
        outputs["depfile"] = FileData(depfile)
        inputs["python"] = self.python
        inputs["wrapper"] = AssetData(_LISTFILES_ASSET)
        exclude_dirs = {self.out_dir.rstrip("/")}
        for dep in self.dependencies:
            for atom in dep.atoms():
                pattern = getattr(atom, "pattern", None)
                if pattern is not None and pattern.literal_prefix_dir:
                    exclude_dirs.add(pattern.literal_prefix_dir)
        exclude_flags = " ".join(f"--exclude {d}" for d in sorted(exclude_dirs))
        inputs["excludes"] = ValueData(exclude_flags)

        return [
            ExternalProcessAction(
                name=f"tsc -> {self.out_dir}",
                command_line=(
                    f"{{python}} {{wrapper}} {{depfile}} {{excludes}} "
                    f"-- {tsc_command}"
                ),
                inputs=inputs,
                outputs=outputs,
                self_resolved_inputs=self_resolved,
                depfile=depfile,
            )
        ]


class TypedocAction(Action):
    """Generate HTML API docs for a project's *own* TypeScript sources with
    ``typedoc``.

    Documentation *of the user's code*, run as an ordinary tracked step: the
    declared ``entry_points`` (the sources) are inputs, so the doc set rebuilds
    on a source change and replays when nothing changed. Pass typedoc flags
    (``--name``, ``--excludePrivate``, ``--tsconfig …``) through ``options``.

    typedoc is usually a local install, not global; pass
    ``typedoc=data.tool(".../node_modules/.bin/typedoc")`` to point at it.

    ::

        ts.TypedocAction(
            entry_points=data.glob("src/{name}.ts"),
            output_dir="build/docs/api",
        )
    """

    def __init__(
        self,
        *,
        entry_points: DataExpr,
        output_dir: str,
        typedoc: ToolData | None = None,
        options: list[str] | DataExpr | None = None,
    ) -> None:
        self.entry_points = entry_points
        self.output_dir = output_dir
        self.typedoc = typedoc if typedoc is not None else ToolData("typedoc")
        self.options = options

    def lower(self) -> list[Action]:
        # One declared output tree: a recursive `{name}` glob under output_dir
        # captures every emitted page and asset, so a removed page is reflected
        # and the tree stages atomically (mirrors JavadocAction).
        return [
            ExternalProcessAction(
                name=f"typedoc -> {self.output_dir}",
                command_line="{typedoc} --out {output_dir} {options} {entry_points}",
                inputs={
                    "typedoc": self.typedoc,
                    "options": _options_expr(self.options),
                    "entry_points": self.entry_points,
                },
                outputs={
                    "output_dir": FileData(self.output_dir),
                    "files": GlobData(f"{self.output_dir}/{{name}}"),
                },
            )
        ]
