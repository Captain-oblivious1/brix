"""Depfile (discovered-header) build equivalence.

C/C++ dependencies on headers are not declared up front — the compiler reports
which headers it actually read (a `-MMD` depfile) and brix scans it. That makes
the dependency edge *discovered*, which is easy to get subtly wrong, so it is
worth an equivalence check.

Two translation units read different headers. The tests assert both selectivity
and soundness: editing a header recompiles exactly the unit that includes it
(the other replays), and the recompiled object matches a clean build of the same
final state.

Compiler-gated: skips where no `cc` is on PATH. Compares object bytes (gcc `-c`
without debug info is deterministic for the same source and relative paths).
"""

import os
import shutil
from pathlib import Path

import pytest

from brix import CleanBuilder, Graph, IncrementalBuilder, cpp
from brix.data import ToolData

needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no cc on PATH")

_rust_only = pytest.mark.skipif(
    os.environ.get("BRIX_ENGINE") == "py",
    reason="depfile sweep runs on the default/Rust engine; goldens pin cross-engine parity",
)

# main.c reads foo.h; other.c reads bar.h. The headers define the constants the
# sources compile in, so an edit changes the object bytes.
MAIN_C = '#include "foo.h"\nint main_val(void) { return FOO; }\n'
OTHER_C = '#include "bar.h"\nint other_val(void) { return BAR; }\n'


def initial_state() -> dict:
    return {"foo": 1, "bar": 2}


def materialize(root: Path, state: dict) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "src/main.c").write_text(MAIN_C)
    (root / "src/other.c").write_text(OTHER_C)
    (root / "include/foo.h").write_text(f"#define FOO {state['foo']}\n")
    (root / "include/bar.h").write_text(f"#define BAR {state['bar']}\n")


def apply_op(root: Path, state: dict, op: str) -> None:
    if op in ("foo", "bar"):
        state[op] += 1
    else:
        raise AssertionError(op)
    materialize(root, state)


def build_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        cpp.CompileSourceAction(
            source_file=data.glob("src/{name}.c"),
            object_file=data.glob("build/obj/{name}.o"),
            compiler=ToolData("cc"),
            compile_options=["-Iinclude"],
        )
    )
    return graph


def _builder(root: Path) -> IncrementalBuilder:
    return IncrementalBuilder(build_graph, root=root, metadata_dir=".brix-meta")


def _compile_source(result, action_name: str) -> str | None:
    for r in result.process_results:
        if r.action_name == action_name:
            return r.source
    return None


def _objects(root: Path) -> dict:
    d = root / "build/obj"
    if not d.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in d.rglob("*.o")}


@needs_cc
def test_header_edit_recompiles_only_its_dependent_and_matches_clean(tmp_path):
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    builder = _builder(inc)
    assert builder.build().success

    apply_op(inc, state, "foo")  # a header only main.c includes
    result = builder.build()
    assert result.success
    # Selectivity: the discovered edge fires for main, not other.
    assert _compile_source(result, "compile main") == "process"
    assert _compile_source(result, "compile other") == "replayed"

    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _objects(inc) == _objects(clean)


@needs_cc
def test_other_header_edit_is_selective_and_matches_clean(tmp_path):
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    builder = _builder(inc)
    assert builder.build().success

    apply_op(inc, state, "bar")  # a header only other.c includes
    result = builder.build()
    assert result.success
    assert _compile_source(result, "compile other") == "process"
    assert _compile_source(result, "compile main") == "replayed"

    clean = tmp_path / "clean"
    materialize(clean, state)
    assert CleanBuilder(build_graph(), root=clean).build().success
    assert _objects(inc) == _objects(clean)


_DEPFILE_SEQUENCES = [
    ["foo", "foo"],
    ["foo", "bar"],
    ["bar", "foo", "bar"],
]


_DEPFILE_IDS = ["→".join(s) for s in _DEPFILE_SEQUENCES]


@needs_cc
@_rust_only
@pytest.mark.parametrize("ops", _DEPFILE_SEQUENCES, ids=_DEPFILE_IDS)
def test_depfile_edit_sequences_match_clean(tmp_path, ops):
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
    assert _objects(inc) == _objects(clean), f"ops={ops}"
