"""ts-dts build equivalence: the TypeScript counterpart of the jvm-abi shortcut.

`tsc`'s declaration emit writes only the public API, so an implementation-only
change to a module produces a byte-identical `.d.ts`. Viewed through the ts-dts
lens, a dependent's fingerprint is then unchanged and it **replays** instead of
recompiling — doing deliberately *less* than a clean build, exactly where a
missed judgement would ship a stale output. So these tests assert both halves:

  * the shortcut is *taken* — an impl-only edit to A skips recompiling B;
  * the shortcut is *sound* — B's emitted `.js` is byte-identical to a clean
    build of the same final state; and an API edit *cascades* to B.

tsc-gated: `tsc` is usually a local install, not global, so point the test at one
with `BRIX_TSC=/path/to/node_modules/.bin/tsc` (falls back to a global `tsc`);
skips when neither is present.
"""

import os
import shutil

import pytest

from brix import CleanBuilder, Graph, IncrementalBuilder, ts
from brix.data import ToolData

TSC = os.environ.get("BRIX_TSC") or shutil.which("tsc")
needs_tsc = pytest.mark.skipif(TSC is None, reason="no tsc (set BRIX_TSC to a tsc binary)")

# Predictable, deterministic emit: fix module/target and an explicit rootDir per
# module so output paths don't depend on the inputs' common ancestor.
_COMMON = ["--module", "commonjs", "--moduleResolution", "node", "--target", "es2020"]

# Module A. `value()`'s body is the impl knob (declaration-invariant); `extra()`
# is the API knob (exporting it changes A's `.d.ts`, so B must recompile).
LIB = "export function value(): number {{ return {impl}; }}\n{extra}"
EXTRA = "export function extra(): number { return 9; }\n"

# Module B imports A's built declaration and calls value(). Its emitted JS
# references the function, not its body, so B.js is identical however value() is
# written — the property the lens exploits.
APP = 'import { value } from "../../a/dist/lib";\n'
APP += "export function run(): number { return value(); }\n"


def initial_state() -> dict:
    return {"impl": 1, "api": False}


def materialize(root, state) -> None:
    (root / "a/src").mkdir(parents=True, exist_ok=True)
    (root / "b/src").mkdir(parents=True, exist_ok=True)
    (root / "a/src/lib.ts").write_text(
        LIB.format(impl=state["impl"], extra=EXTRA if state["api"] else "")
    )
    (root / "b/src/app.ts").write_text(APP)


def apply_op(root, state, op) -> None:
    if op == "impl":
        state["impl"] += 1                 # new body, same signature: API-invariant
    elif op == "api":
        state["api"] = not state["api"]    # add/remove an export: declaration change
    else:
        raise AssertionError(op)
    materialize(root, state)


def build_graph() -> Graph:
    g = Graph()
    d = g.data
    tsc = ToolData(TSC)
    g.add(
        ts.CompileAction(
            sources=d.glob("a/src/{name}.ts"),
            out_dir="a/dist",
            tsc=tsc,
            options=[*_COMMON, "--rootDir", "a/src"],
        ),
        # B tracks A's *declarations* purely by declaring a/dist/lib.d.ts as a
        # dependency (ts-dts-lensed inside CompileAction); tsc resolves the
        # import to it on disk. An impl-only change to A leaves it untouched.
        ts.CompileAction(
            sources=d.glob("b/src/{name}.ts"),
            out_dir="b/dist",
            tsc=tsc,
            options=[*_COMMON, "--rootDir", "b/src"],
            dependencies=[d.file("a/dist/lib.d.ts")],
        ),
    )
    return g


def _builder(root):
    return IncrementalBuilder(build_graph, root=root, metadata_dir=".brix-meta")


def _source(result, action_name):
    for r in result.process_results:
        if r.action_name == action_name:
            return r.source
    return None


def _js_tree(root):
    out = {}
    for sub in ("a/dist", "b/dist"):
        d = root / sub
        if d.exists():
            for p in d.rglob("*.js"):
                out[str(p.relative_to(root))] = p.read_bytes()
    return out


@needs_tsc
def test_impl_only_edit_skips_dependent_and_matches_clean(tmp_path):
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    builder = _builder(inc)
    assert builder.build().success

    apply_op(inc, state, "impl")
    result = builder.build()
    assert result.success
    # The shortcut is taken: A recompiled, B skipped.
    assert _source(result, "tsc -> a/dist") == "process"
    assert _source(result, "tsc -> b/dist") == "replayed"

    # The shortcut is sound: the skipped B still equals a clean rebuild.
    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _js_tree(inc) == _js_tree(clean)


@needs_tsc
def test_api_edit_cascades_to_dependent_and_matches_clean(tmp_path):
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    builder = _builder(inc)
    assert builder.build().success

    apply_op(inc, state, "api")
    result = builder.build()
    assert result.success
    # A declaration change cascades: B recompiles.
    assert _source(result, "tsc -> b/dist") == "process"

    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _js_tree(inc) == _js_tree(clean)


# --- automatic import tracking: an undeclared shared source (real tsc) --------


@needs_tsc
def test_editing_an_undeclared_imported_source_recompiles(tmp_path):
    """A module imports a shared `.ts` that is NOT in its `sources` glob and is
    NOT a declared dependency. Without import tracking, editing that file leaves
    the module stale; with it (the default), the compile re-runs. Proven here
    against a real tsc's --listFiles output."""
    root = tmp_path / "ws"
    (root / "app/src").mkdir(parents=True)
    (root / "common").mkdir()
    (root / "common/util.ts").write_text("export const tag = 'v1';\n")
    # The import reaches outside app/src — the declared glob never sees it.
    (root / "app/src/main.ts").write_text(
        'import { tag } from "../../common/util";\n'
        "export function run(): string { return tag; }\n"
    )

    def graph():
        g = Graph()
        g.add(
            ts.CompileAction(
                sources=g.data.glob("app/src/{name}.ts"),
                out_dir="app/dist",
                tsc=ToolData(TSC),
                options=[*_COMMON, "--rootDir", "app/src"],
            )
        )
        return g

    builder = IncrementalBuilder(graph, root=root, metadata_dir=".brix-meta")
    assert builder.build().success

    result = builder.build()  # no-op: nothing changed
    assert _source(result, "tsc -> app/dist") == "replayed"

    (root / "common/util.ts").write_text("export const tag = 'v2';\n")
    result = builder.build()
    assert result.success
    assert _source(result, "tsc -> app/dist") == "process"  # the hole is closed
