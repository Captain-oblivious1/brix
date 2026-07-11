"""ABI-lens build equivalence: the clever shortcut is both *taken* and *sound*.

The jvm-abi lens is where an incremental build deliberately does *less* than a
clean one: an implementation-only change to a library recompiles the library but
lets its dependents replay, because their compiled output would be identical
anyway. That is exactly the case where a missed judgement ships a stale output.

So these tests assert both halves:
  * the shortcut is *taken* — an impl-only edit to A skips recompiling B (else
    there is no ABI benefit, and an output match would prove nothing);
  * the shortcut is *sound* — the resulting classes are byte-identical to a
    clean build of the same final state; and an API edit *cascades* to B.

Compares extracted `.class` bytes (javac is deterministic), not jars — jar
entries carry timestamps that differ run to run even when the classes match.

JDK-gated: skips where no javac/jar/java is on PATH.
"""

import os
import shutil
from pathlib import Path

import pytest

from brix import CleanBuilder, ExternalProcessAction, Graph, IncrementalBuilder, java

needs_jdk = pytest.mark.skipif(
    shutil.which("javac") is None
    or shutil.which("jar") is None
    or shutil.which("java") is None,
    reason="no JDK on PATH",
)

# javac makes these builds costly, so the sweep runs on the default (Rust)
# engine — the shipping one; goldens pin that the Python engine agrees. The two
# focused impl/API tests below run under both engines.
_rust_only = pytest.mark.skipif(
    os.environ.get("BRIX_ENGINE") == "py",
    reason="ABI sweep runs on the default/Rust engine; goldens pin cross-engine parity",
)

# Module A. `value()`'s body is the impl knob (ABI-invariant); `extra()` is the
# API knob (adding it changes A's ABI, so dependents must recompile).
LIB = """package abidemo;
public class Lib {{
    public int value() {{ return {impl}; }}
{extra}}}
"""
EXTRA = "    public int extra() { return 9; }\n"

# Module B compiles against A's ABI and calls value(). Its bytecode references
# the method, not its body, so B.class is identical however value() is written.
APP = """package app;
import abidemo.Lib;
public class App {
    public static int run() { return new Lib().value(); }
}
"""

A_CLASSES = "build/a-classes"
B_CLASSES = "build/b-classes"


def initial_state() -> dict:
    return {"impl": 1, "api": False}


def materialize(root: Path, state: dict) -> None:
    (root / "a").mkdir(parents=True, exist_ok=True)
    (root / "b").mkdir(parents=True, exist_ok=True)
    (root / "a/Lib.java").write_text(
        LIB.format(impl=state["impl"], extra=EXTRA if state["api"] else "")
    )
    (root / "b/App.java").write_text(APP)


def apply_op(root: Path, state: dict, op: str) -> None:
    if op == "impl":
        state["impl"] += 1          # new method body, same signature: ABI-invariant
    elif op == "api":
        state["api"] = not state["api"]  # add/remove a public method: ABI change
    else:
        raise AssertionError(op)
    materialize(root, state)


def build_graph() -> Graph:
    # Hand-rolled with plain javac spawns (no warm worker) so the test needs no
    # worker bootstrap. The ABI lens comes purely from declaring B's classpath
    # jar as java.AbiFile: B's compile fingerprint then tracks A's *ABI*, so an
    # impl-only change to A leaves it untouched and B replays.
    graph = Graph()
    data = graph.data
    javac = data.tool("javac")
    jar = data.tool("jar")
    graph.add(
        ExternalProcessAction(
            name="compile-a",
            command_line="{javac} --release 17 -d {out} {sources}",
            inputs={"javac": javac, "sources": data.glob("a/{name}.java")},
            outputs={
                "out": data.file(A_CLASSES),
                "classes": data.glob(f"{A_CLASSES}/{{name}}.class"),
            },
        ),
        ExternalProcessAction(
            name="jar-a",
            command_line="{jar} cf {out} -C {classes_dir} .",
            inputs={
                "jar": jar,
                "classes_dir": data.file(A_CLASSES),
                "classes": data.glob(f"{A_CLASSES}/{{name}}.class"),
            },
            outputs={"out": data.file("build/a.jar")},
        ),
        ExternalProcessAction(
            name="compile-b",
            command_line="{javac} --release 17 -cp {cp} -d {out} {sources}",
            inputs={
                "javac": javac,
                "cp": java.AbiFile("build/a.jar"),  # ABI lens: B tracks A's API
                "sources": data.glob("b/{name}.java"),
            },
            outputs={
                "out": data.file(B_CLASSES),
                "classes": data.glob(f"{B_CLASSES}/{{name}}.class"),
            },
        ),
    )
    return graph


def _builder(root: Path) -> IncrementalBuilder:
    return IncrementalBuilder(build_graph, root=root, metadata_dir=".brix-meta")


def _compile_source(result, action_name: str) -> str | None:
    """'process' if that compile ran this build, 'replayed' if it was skipped,
    None if the node is absent."""
    for r in result.process_results:
        if r.action_name == action_name:
            return r.source
    return None


def _class_tree(root: Path) -> dict:
    out = {}
    for sub in (A_CLASSES, B_CLASSES):
        d = root / sub
        if d.exists():
            for p in d.rglob("*.class"):
                out[str(p.relative_to(root))] = p.read_bytes()
    return out


@needs_jdk
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
    assert _compile_source(result, "compile-a") == "process"
    assert _compile_source(result, "compile-b") == "replayed"

    # The shortcut is sound: the skipped B still equals a clean rebuild.
    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _class_tree(inc) == _class_tree(clean)


@needs_jdk
def test_api_edit_cascades_to_dependent_and_matches_clean(tmp_path):
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    builder = _builder(inc)
    assert builder.build().success

    apply_op(inc, state, "api")
    result = builder.build()
    assert result.success
    # An ABI change cascades: B recompiles.
    assert _compile_source(result, "compile-b") == "process"

    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _class_tree(inc) == _class_tree(clean)


# Every short sequence of impl/API edits ends where a clean build of the same
# state ends — the ABI counterpart of the byte-lens exhaustive sweep.
_ABI_SEQUENCES = [
    ["impl", "impl"],
    ["impl", "api"],
    ["api", "impl"],
    ["impl", "api", "impl"],
]


@needs_jdk
@_rust_only
@pytest.mark.parametrize("ops", _ABI_SEQUENCES, ids=["→".join(s) for s in _ABI_SEQUENCES])
def test_abi_edit_sequences_match_clean(tmp_path, ops):
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    builder = _builder(inc)
    assert builder.build().success
    for op in ops:
        apply_op(inc, state, op)
        assert builder.build().success, f"incremental build failed after {op!r}"

    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _class_tree(inc) == _class_tree(clean), f"ops={ops}"
