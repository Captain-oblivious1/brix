"""Multi-module Kotlin: the kotlin-abi lens's end-to-end equivalence oracle.

A Kotlin `inline fun` copies its body into consumers' bytecode at compile time
— line-number tables included (SMAP) — so a dependent's output genuinely
depends on the *implementation* of inline functions in modules it imports.
Plain jvm-abi lensing is therefore unsound here (it once let brix serve a
stale dependent — commit 3746bd1), and plain byte-tracking is sound but
recompiles dependents on every implementation edit. `kotlin.JarAction` tracks
its classpath through the **kotlin-abi** lens (brix/kotlinabi.py): jvm-abi
counting for inline-free classes, whole-class byte counting exactly where
inline is involved.

These tests pin both directions of that contract with three knobs in `core`:

  * a *normal* function body (`greet`, in the inline-free class `Greeter`):
    changing it leaves the dependent's bytecode invariant, and the lens now
    proves it — the dependent REPLAYS (the recovered cross-module skip);
  * an *inline* function body (`combine`): changing it changes the dependent's
    bytecode, so the dependent MUST recompile or it ships stale;
  * *padding lines* above the inline fun: nothing changes but line numbers —
    which the inlined copy carries — so the dependent must still recompile.

Every case asserts the incrementally-built dependent's classes equal a clean
rebuild of the same state: the anti-stale equivalence oracle. The inline and
line-shift cases additionally assert the dependent's bytes actually changed —
otherwise they would prove nothing.

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

# core exports a normal function in an inline-free class (impl knob), padding
# lines (shift knob), and an inline function (body knob). Greeter sits ABOVE
# the padding so only the inline fun moves when the shift knob turns.
LIB = "package demo.core\n"
LIB += 'class Greeter {{ fun greet(name: String): String = "hi-{impl}-$name" }}\n'
LIB += "{padding}"
LIB += "inline fun combine(block: () -> String): String = {inline_body}\n"

# app calls both; combine() is inlined, so app's bytecode embeds combine's body.
APP = "package demo.app\n"
APP += "import demo.core.Greeter\nimport demo.core.combine\n"
APP += 'fun main() { val g = Greeter(); println(combine { g.greet("brix") }) }\n'


def initial_state() -> dict:
    return {"impl": 1, "inline": 2, "shift": 0}


def _inline_body(reps: int) -> str:
    return " + ".join(["block()"] * reps)


def materialize(root, state) -> None:
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "core/lib.kt").write_text(
        LIB.format(
            impl=state["impl"],
            padding="// pad\n" * state["shift"],
            inline_body=_inline_body(state["inline"]),
        )
    )
    (root / "app/main.kt").write_text(APP)


def apply_op(root, state, op) -> None:
    if op == "impl":
        state["impl"] += 1        # new normal-fun body, dependent bytecode invariant
    elif op == "inline":
        state["inline"] += 1      # new inline-fun body, copied into the dependent
    elif op == "shift":
        state["shift"] += 1       # inline fun moves down one line; SMAP leaks it
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
    # anti-stale property a wrong (jvm-abi) lens historically broke.
    assert after == _clean_app_classes(tmp_path, state)


@needs_kotlinc
def test_line_shift_above_inline_recompiles_dependent_and_matches_clean(tmp_path):
    root = tmp_path / "inc"
    state = initial_state()
    materialize(root, state)
    builder = _builder(root)
    assert builder.build().success
    before = _app_classes(root)

    apply_op(root, state, "shift")
    result = builder.build()
    assert result.success
    # Nothing changed but the inline fun's line numbers — which the inlined
    # copy in app carries (SMAP). The dependent must recompile.
    assert _source(result, "kotlinc -> build/app-classes") == "process"
    after = _app_classes(root)
    assert after != before  # the line numbers really do reach app's bytes
    assert after == _clean_app_classes(tmp_path, state)


@needs_kotlinc
def test_normal_change_skips_dependent_and_matches_clean(tmp_path):
    root = tmp_path / "inc"
    state = initial_state()
    materialize(root, state)
    builder = _builder(root)
    assert builder.build().success
    before = _app_classes(root)

    apply_op(root, state, "impl")
    result = builder.build()
    assert result.success
    # core recompiles (its source changed)...
    assert _source(result, "kotlinc -> build/core-classes") == "process"
    # ...but the dependent REPLAYS: Greeter carries no inline members, so its
    # body change is invisible through the kotlin-abi lens. This is the
    # cross-module skip that byte-tracking gave up and the lens recovers.
    assert _source(result, "kotlinc -> build/app-classes") == "replayed"
    after = _app_classes(root)
    assert after == before
    # The skip is SOUND: the replayed dependent equals a clean rebuild of the
    # new state, byte for byte. This assertion is the equivalence oracle — if
    # the lens ever wrongly skips a change that matters, it fails here.
    assert after == _clean_app_classes(tmp_path, state)
