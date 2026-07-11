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

import random
import sys
from pathlib import Path

import pytest

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

NAME_POOL = ["a", "b", "c", "d"]
SUFFIXES = ["", "!", "#end"]          # tool variants
SEPARATORS = [",", "|", ""]           # separator values


def build_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
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
    return {"sources": {"a": "a-0", "b": "b-0"}, "variant": 0, "sep": 0}


def materialize(root: Path, state: dict) -> None:
    """Write the *entire* workspace from state (used for the clean rebuild)."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    for name, text in state["sources"].items():
        (root / "src" / f"{name}.txt").write_text(text)
    (root / "tools/upper.py").write_text(UPPER_TOOL.format(suffix=SUFFIXES[state["variant"]]))
    (root / "tools/concat.py").write_text(CONCAT_TOOL)
    (root / "config/sep.txt").write_text(SEPARATORS[state["sep"]])


def apply_op(root: Path, state: dict, op: tuple, tick: int) -> None:
    """Mutate state and the on-disk workspace by a single edit (used for the
    incremental sequence — edits land the way a person would make them)."""
    kind = op[0]
    if kind == "edit":
        name = op[1]
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


def test_equiv_mixed_sequence(tmp_path):
    assert_incremental_matches_clean(
        tmp_path,
        [("add", "c"), ("tool",), ("edit", "a"), ("delete", "b"), ("sep",), ("add", "d")],
        "mixed",
    )


# --- generative: random edit sequences hunt for missed edges ---


def _random_ops(rng: random.Random, present: set, n: int) -> list:
    ops = []
    for _ in range(n):
        choices = ["tool", "sep"]
        if present:
            choices += ["edit", "edit", "delete"]  # weight edits a little
        absent = [x for x in NAME_POOL if x not in present]
        if absent:
            choices.append("add")
        kind = rng.choice(choices)
        if kind == "edit":
            ops.append(("edit", rng.choice(sorted(present))))
        elif kind == "delete":
            name = rng.choice(sorted(present))
            present.discard(name)
            ops.append(("delete", name))
        elif kind == "add":
            name = rng.choice(absent)
            present.add(name)
            ops.append(("add", name))
        else:
            ops.append((kind,))
    return ops


@pytest.mark.parametrize("seed", range(24))
def test_equiv_random_edit_sequences(tmp_path, seed):
    """Any random walk of edits ends where a clean build of the same state
    ends. A failure reprints the seed and ops for deterministic replay."""
    rng = random.Random(seed)
    ops = _random_ops(rng, present={"a", "b"}, n=rng.randint(4, 8))
    assert_incremental_matches_clean(tmp_path, ops, f"seed={seed}")
