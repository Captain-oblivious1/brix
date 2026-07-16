"""Multi-module Kotlin soundness: the dual of the ts-dts/jvm-abi skip.

A Kotlin `inline fun` copies its body into consumers' bytecode at compile time,
so a dependent's output genuinely depends on the *body* of an inline function in
a module it imports — not just that function's signature. `kotlin.JarAction`
therefore byte-tracks its classpath instead of ABI-lensing it (which
`java.JarAction` can safely do, because javac does not inline across methods).

These tests pin the property that a wrong lens would violate — serving a stale
dependent — using two knobs in the `core` module:

  * a *normal* function body (`greet`): changing it does not change the
    dependent's bytecode, so recompiling the dependent is conservative but sound;
  * an *inline* function body (`combine`): changing it DOES change the
    dependent's bytecode, so the dependent MUST recompile or it ships stale.

Both cases assert the dependent recompiles and its emitted classes equal a clean
rebuild of the same state. The inline case additionally asserts the dependent's
bytes actually changed — otherwise the test would prove nothing.

kotlinc-gated: point at a compiler with `BRIX_KOTLINC=/path/to/kotlinc` (falls
back to a `kotlinc` on PATH); skips when neither is present.
"""

import os
import shutil
import zipfile

import pytest

from brix import CleanBuilder, Graph, IncrementalBuilder, kotlin
from brix.data import FileData, GlobData, ToolData

KOTLINC = os.environ.get("BRIX_KOTLINC") or shutil.which("kotlinc")
needs_kotlinc = pytest.mark.skipif(
    KOTLINC is None, reason="no kotlinc (set BRIX_KOTLINC to a kotlinc binary)"
)

# core exports a normal function (impl knob) and an inline function (body knob).
LIB = 'package demo.core\n'
LIB += 'class Greeter {{ fun greet(name: String): String = "hi-{impl}-$name" }}\n'
LIB += "inline fun combine(block: () -> String): String = {inline_body}\n"

# app calls both; combine() is inlined, so app's bytecode embeds combine's body.
APP = "package demo.app\n"
APP += "import demo.core.Greeter\nimport demo.core.combine\n"
APP += 'fun main() { val g = Greeter(); println(combine { g.greet("brix") }) }\n'


def initial_state() -> dict:
    return {"impl": 1, "inline": 2}


def _inline_body(reps: int) -> str:
    return " + ".join(["block()"] * reps)


def materialize(root, state) -> None:
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "core/lib.kt").write_text(
        LIB.format(impl=state["impl"], inline_body=_inline_body(state["inline"]))
    )
    (root / "app/main.kt").write_text(APP)


def apply_op(root, state, op) -> None:
    if op == "impl":
        state["impl"] += 1        # new normal-fun body, dependent bytecode invariant
    elif op == "inline":
        state["inline"] += 1      # new inline-fun body, copied into the dependent
    else:
        raise AssertionError(op)
    materialize(root, state)


def build_graph() -> Graph:
    g = Graph()
    kc = ToolData(KOTLINC)
    g.add(
        kotlin.JarAction(
            source_files=GlobData("core/{name}.kt"),
            output_file=FileData("build/core.jar"),
            kotlinc=kc, classes_dir="build/core-classes", jvm_target="17",
        ),
        kotlin.JarAction(
            source_files=GlobData("app/{name}.kt"),
            output_file=FileData("build/app.jar"),
            kotlinc=kc, classes_dir="build/app-classes", jvm_target="17",
            classpath=[FileData("build/core.jar")],
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


def _app_classes(root):
    jar = root / "build/app.jar"
    with zipfile.ZipFile(jar) as z:
        return {n: z.read(n) for n in sorted(z.namelist()) if n.endswith(".class")}


def _clean_app_classes(tmp_path, state):
    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    return _app_classes(clean)


@needs_kotlinc
def test_no_op_rebuild_is_clean(tmp_path):
    root = tmp_path / "inc"
    materialize(root, initial_state())
    builder = _builder(root)
    assert builder.build().success
    result = builder.build()
    assert result.success
    # A no-op rebuild runs no process: every node replays its prior result.
    assert _source(result, "kotlinc -> build/core-classes") == "replayed"
    assert _source(result, "kotlinc -> build/app-classes") == "replayed"


@needs_kotlinc
def test_inline_change_recompiles_dependent_and_matches_clean(tmp_path):
    root = tmp_path / "inc"
    state = initial_state()
    materialize(root, state)
    builder = _builder(root)
    assert builder.build().success
    before = _app_classes(root)

    apply_op(root, state, "inline")
    result = builder.build()
    assert result.success
    # The dependent MUST recompile: its bytecode embeds the inlined body.
    assert _source(result, "kotlinc -> build/app-classes") == "process"
    after = _app_classes(root)
    # The change really propagated (otherwise this test proves nothing)...
    assert after != before
    # ...and the incrementally-built dependent equals a clean rebuild — the
    # anti-stale property a wrong (ABI) lens would break.
    assert after == _clean_app_classes(tmp_path, state)


@needs_kotlinc
def test_normal_change_recompiles_and_matches_clean(tmp_path):
    root = tmp_path / "inc"
    state = initial_state()
    materialize(root, state)
    builder = _builder(root)
    assert builder.build().success

    apply_op(root, state, "impl")
    result = builder.build()
    assert result.success
    # Conservative but sound: byte-tracking recompiles the dependent even though
    # a normal-body change leaves its bytecode invariant. It still matches clean.
    assert _source(result, "kotlinc -> build/app-classes") == "process"
    assert _app_classes(root) == _clean_app_classes(tmp_path, state)
