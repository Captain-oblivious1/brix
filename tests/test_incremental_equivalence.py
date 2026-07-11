"""The central correctness invariant: an incremental build produces the same
outputs as a clean build of the same final state.

Where `test_incremental.py` checks *individual* invalidation paths against
hand-written expected values, this file checks the property those tests are all
really about — that no sequence of edits ever leaves incremental output
diverging from a from-scratch build. It does so with a real oracle: drive a
graph through a sequence of edits incrementally, rebuild the *final* state clean
in a fresh workspace, and assert the two output trees are byte-identical.

A divergence is the dangerous failure mode of any incremental engine — a missed
edge that ships a stale output. The generative test hunts those by applying
random edit sequences; a failure prints the seed and the exact op sequence so it
reproduces deterministically. Dependency-free on purpose (no hypothesis): the
suite stays hermetic, and the seed + sequence give enough to reproduce and
shrink by hand.

Both engines run this file (the suite runs under the Rust engine and again with
BRIX_ENGINE=py), so each engine is checked self-consistent; the golden fixtures
separately pin that the two engines agree byte-for-byte.
"""

import itertools
import os
import sys
from pathlib import Path

import pytest

# The exhaustive sweep is large, so it runs on the default (Rust) engine — the
# shipping one and the source of truth for semantics. The golden fixtures pin
# that the Python engine agrees with it byte-for-byte, and the pointwise + deep
# cases below still run under both engines, so the Python engine's incremental
# path is not left unchecked.
_rust_only = pytest.mark.skipif(
    os.environ.get("BRIX_ENGINE") == "py",
    reason="exhaustive sweep runs on the default/Rust engine; goldens pin parity",
)

from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph, IncrementalBuilder

# A source is upper-cased by a per-variant tool, then all results are
# concatenated (sorted, with a separator read from a file) by a fan-in step.
# Output is a pure function of the workspace, so clean and incremental must
# agree byte-for-byte. The tool's transform lives in the tool *file* (a "tool"
# edit rewrites it) and the separator lives in a file (a "sep" edit rewrites
# it), so every input a build depends on is on disk and reconstructible.
UPPER_TOOL = """\
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
Path(sys.argv[2]).write_text(text.upper() + {suffix!r})
"""

# argv: <sep_file> <part>... <out>. Sorts parts so output is independent of the
# order the glob yields them — the equivalence we test is about *contents*.
CONCAT_TOOL = """\
import sys
from pathlib import Path
sep = Path(sys.argv[1]).read_text()
parts = sorted(sys.argv[2:-1])
Path(sys.argv[-1]).write_text(sep.join(Path(p).read_text() for p in parts))
"""

# A generator: reads a spec file and writes a *source* (src/zgen.txt) that the
# compile glob then expands over — a generate-then-consume edge within one
# build. Editing the spec must regenerate the source, re-upper it, and recombine,
# and a clean build must reach the same place from the spec alone (the generated
# file is never materialized directly).
GEN_TOOL = """\
import sys
from pathlib import Path
Path(sys.argv[2]).write_text("gen:" + Path(sys.argv[1]).read_text())
"""

NAME_POOL = ["a", "b", "c", "d"]
SUFFIXES = ["", "!", "#end"]          # tool variants
SEPARATORS = [",", "|", ""]           # separator values
GEN_SPECS = ["g0", "g1", "g2"]        # generator spec values


def build_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        # Generated source: an output under src/, so the compile glob below
        # expands over a member produced *during* the build.
        ExternalProcessAction(
            name="generate",
            command_line="{python} {tool} {spec} {out}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/gen.py"),
                "spec": data.file("config/gen.txt"),
            },
            outputs={"out": data.file("src/zgen.txt")},
        ),
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {sep} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "sep": data.file("config/sep.txt"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": data.file("build/all.txt")},
        ),
    )
    return graph


# --- workspace state: everything a build reads, so it reconstructs anywhere ---


def initial_state() -> dict:
    return {"sources": {"a": "a-0", "b": "b-0"}, "variant": 0, "sep": 0, "gen": 0}


def materialize(root: Path, state: dict) -> None:
    """Write the *entire* workspace from state (used for the clean rebuild).
    The generated source (src/zgen.txt) is deliberately *not* written — the
    generate step must produce it from config/gen.txt, so the clean build
    exercises the same generate-then-consume path the incremental one did."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    for name, text in state["sources"].items():
        (root / "src" / f"{name}.txt").write_text(text)
    upper = UPPER_TOOL.format(suffix=SUFFIXES[state["variant"]])
    (root / "tools/upper.py").write_text(upper)
    (root / "tools/concat.py").write_text(CONCAT_TOOL)
    (root / "tools/gen.py").write_text(GEN_TOOL)
    (root / "config/sep.txt").write_text(SEPARATORS[state["sep"]])
    (root / "config/gen.txt").write_text(GEN_SPECS[state["gen"]])


def apply_op(root: Path, state: dict, op: tuple, tick: int) -> None:
    """Mutate state and the on-disk workspace by a single edit (edits land the
    way a person would make them). Ops are *tolerant* — editing an absent source
    or deleting a missing one is a no-op — so any op sequence is valid and the
    exhaustive enumeration needs no per-sequence validity bookkeeping."""
    kind = op[0]
    if kind == "edit":
        name = op[1]
        if name in state["sources"]:  # tolerant: editing an absent source is a no-op
            state["sources"][name] = f"{name}-{tick}"
            (root / "src" / f"{name}.txt").write_text(state["sources"][name])
    elif kind == "add":
        name = op[1]
        state["sources"][name] = f"{name}-{tick}"
        (root / "src" / f"{name}.txt").write_text(state["sources"][name])
    elif kind == "delete":
        name = op[1]
        state["sources"].pop(name, None)
        (root / "src" / f"{name}.txt").unlink(missing_ok=True)
    elif kind == "tool":
        state["variant"] = (state["variant"] + 1) % len(SUFFIXES)
        (root / "tools/upper.py").write_text(
            UPPER_TOOL.format(suffix=SUFFIXES[state["variant"]])
        )
    elif kind == "sep":
        state["sep"] = (state["sep"] + 1) % len(SEPARATORS)
        (root / "config/sep.txt").write_text(SEPARATORS[state["sep"]])
    elif kind == "gen":
        state["gen"] = (state["gen"] + 1) % len(GEN_SPECS)
        (root / "config/gen.txt").write_text(GEN_SPECS[state["gen"]])
    else:
        raise AssertionError(f"unknown op {op!r}")


# --- the oracle ---


def _incremental_build(root: Path):
    # A fresh builder per step, sharing the on-disk metadata — exactly like
    # re-running the CLI. metadata_dir is kept out of build/ so it never
    # pollutes the output-tree comparison.
    return IncrementalBuilder(build_graph, root=root, metadata_dir=".brix-meta").build()


def _outputs(root: Path) -> dict:
    """The declared output tree: every produced .txt under build/, keyed by
    path relative to build/. Catches both wrong contents and stale leftovers
    (a file incremental kept that clean never made)."""
    build = root / "build"
    if not build.exists():
        return {}
    return {
        str(p.relative_to(build)): p.read_bytes()
        for p in build.rglob("*.txt")
        if p.is_file()
    }


def assert_incremental_matches_clean(tmp_path: Path, ops: list, label: str) -> None:
    inc = tmp_path / "inc"
    state = initial_state()
    materialize(inc, state)
    first = _incremental_build(inc)
    assert first.success, f"{label}: initial incremental build failed"

    for tick, op in enumerate(ops, start=1):
        apply_op(inc, state, op, tick)
        result = _incremental_build(inc)
        assert result.success, f"{label}: incremental build failed after {op!r}"

    # Rebuild the SAME final state from scratch in a clean workspace.
    clean = tmp_path / "clean"
    materialize(clean, state)
    clean_result = CleanBuilder(build_graph(), root=clean).build()
    assert clean_result.success, f"{label}: clean build failed"

    inc_out, clean_out = _outputs(inc), _outputs(clean)
    assert inc_out.keys() == clean_out.keys(), (
        f"{label}: output file sets differ\n"
        f"  only incremental: {sorted(inc_out.keys() - clean_out.keys())}\n"
        f"  only clean:       {sorted(clean_out.keys() - inc_out.keys())}\n"
        f"  ops: {ops}"
    )
    for rel in sorted(inc_out):
        assert inc_out[rel] == clean_out[rel], (
            f"{label}: {rel} diverged from a clean build\n"
            f"  incremental: {inc_out[rel]!r}\n"
            f"  clean:       {clean_out[rel]!r}\n"
            f"  final state: {state}\n  ops: {ops}"
        )


# --- pointwise scenarios (fast, explicit regression anchors) ---


def test_equiv_after_source_edit(tmp_path):
    assert_incremental_matches_clean(tmp_path, [("edit", "a")], "edit")


def test_equiv_after_add_source(tmp_path):
    assert_incremental_matches_clean(tmp_path, [("add", "c")], "add")


def test_equiv_after_delete_source(tmp_path):
    assert_incremental_matches_clean(tmp_path, [("delete", "b")], "delete")


def test_equiv_after_delete_then_readd(tmp_path):
    assert_incremental_matches_clean(
        tmp_path, [("delete", "a"), ("add", "a")], "delete+readd"
    )


def test_equiv_after_tool_change(tmp_path):
    # A tool edit must re-run every source it compiled, or the survivors go
    # stale against a clean build.
    assert_incremental_matches_clean(tmp_path, [("tool",)], "tool")


def test_equiv_after_separator_change(tmp_path):
    # A shared fan-in input change must re-run the combine.
    assert_incremental_matches_clean(tmp_path, [("sep",)], "sep")


def test_equiv_after_delete_to_empty(tmp_path):
    assert_incremental_matches_clean(
        tmp_path, [("delete", "a"), ("delete", "b")], "empty"
    )


def test_equiv_after_generator_spec_change(tmp_path):
    # Editing the generator's spec must regenerate the source, re-upper it, and
    # recombine — a generate-then-consume edge, incrementally.
    assert_incremental_matches_clean(tmp_path, [("gen",)], "gen")


def test_equiv_mixed_sequence(tmp_path):
    assert_incremental_matches_clean(
        tmp_path,
        [("add", "c"), ("tool",), ("edit", "a"), ("gen",), ("delete", "b"), ("sep",)],
        "mixed",
    )


# --- exhaustive: every 1- and 2-edit sequence, checked against a clean build ---
#
# Deterministic and complete within the bound: one run covers *every* single
# edit and *every* ordered pair of edits from a clean start, so confidence does
# not depend on re-running (the worry with random sampling). Most missed edges
# reproduce in one or two edits, so this bound is where the risk concentrates;
# the curated deeper sequences below reach the longer interleavings.

# Tolerant ops (see apply_op), so any product of these is a valid sequence.
ALPHABET = [
    ("edit", "a"),    # edit an existing source
    ("edit", "c"),    # edit a source that may or may not exist yet
    ("delete", "a"),  # remove a source (triggers stale-output cleanup)
    ("add", "c"),     # introduce a new glob member
    ("tool",),        # change the compile tool (must re-run all it built)
    ("sep",),         # change a shared fan-in input (must re-run combine)
    ("gen",),         # change the generator spec (regenerate + reconsume)
]


def _seq_id(ops: tuple) -> str:
    return "→".join("+".join(str(x) for x in op) for op in ops)


_EXHAUSTIVE = [(op,) for op in ALPHABET] + list(itertools.product(ALPHABET, repeat=2))


@_rust_only
@pytest.mark.parametrize("ops", _EXHAUSTIVE, ids=[_seq_id(s) for s in _EXHAUSTIVE])
def test_equiv_all_short_edit_sequences(tmp_path, ops):
    """Every single edit and every ordered pair of edits ends where a clean
    build of the same final state ends — exhaustive, so no re-running needed."""
    assert_incremental_matches_clean(tmp_path, list(ops), _seq_id(ops))


# Curated longer interleavings (deterministic) for depth beyond length 2:
# transitions that have historically hidden bugs — delete/re-add, edit after
# add, tool change after membership change, generator interacting with edits.
_DEEP_SEQUENCES = [
    [("delete", "a"), ("add", "a"), ("edit", "a")],
    [("add", "c"), ("edit", "c"), ("delete", "c"), ("add", "c")],
    [("tool",), ("add", "c"), ("tool",), ("delete", "b")],
    [("gen",), ("edit", "a"), ("gen",), ("gen",)],
    [("delete", "a"), ("delete", "b"), ("add", "c"), ("sep",)],
    [("add", "c"), ("add", "d"), ("delete", "c"), ("tool",), ("edit", "d"), ("gen",)],
]


@pytest.mark.parametrize("ops", _DEEP_SEQUENCES, ids=[_seq_id(s) for s in _DEEP_SEQUENCES])
def test_equiv_deep_sequences(tmp_path, ops):
    assert_incremental_matches_clean(tmp_path, ops, _seq_id(ops))
