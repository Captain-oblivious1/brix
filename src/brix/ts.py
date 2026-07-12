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

Scope for now: a single ``tsc`` spawn per module (no warm compiler daemon — like
`brix.cpp`, unlike `brix.java`'s javac worker) and no npm/``node_modules``
resolver (that is the `ts.npm`-style adapter of docs/dependencies.md, still
planned). ``tsc`` is often not global; pass ``tsc=data.tool(".../node_modules/.bin/tsc")``
to point at a local install.
"""

from __future__ import annotations

from .actions import Action, ExternalProcessAction
from .data import DataExpr, DataUnion, FileData, GlobData, ToolData, ValueData


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
    ) -> None:
        self.sources = sources
        self.out_dir = out_dir
        self.tsc = tsc if tsc is not None else ToolData("tsc")
        self.options = options
        self.declaration = declaration
        # Upstream modules' emitted .d.ts (ts-dts-lensed): fingerprint inputs
        # only. tsc resolves the imports to them on disk (they live in the
        # upstream out_dir this module imports from), so no classpath flag is
        # needed — declaring them as inputs is what makes brix rebuild this
        # module on an upstream API change and replay on an impl-only one.
        self.dependencies = dependencies or []

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

        return [
            ExternalProcessAction(
                name=f"tsc -> {self.out_dir}",
                command_line=(
                    f"{{tsc}} {{options}} {decl_flag}--outDir {{out_dir}} {{sources}}"
                ),
                inputs=inputs,
                outputs=outputs,
                self_resolved_inputs=self_resolved,
            )
        ]
