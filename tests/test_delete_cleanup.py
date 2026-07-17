"""Deleted-source cleanup after a failed build.

brix cleans up the outputs of a deleted source by remembering, between builds,
which collection members existed. That record was committed only on *success* —
so a member added by a build that ultimately FAILED (its output already on
disk) was forgotten, and if its source then vanished the output leaked from
incremental cleanup (only a full `clean` caught it). This is real only for
unstaged builds: atomic mode wipes a failed build's staging before it can reach
the workspace.

The fix: on a failed build the membership record becomes the UNION of what the
last build knew and what this build realized (never a replace) — so an
added-then-deleted member is still cleaned, while a member pending deletion is
retained until a successful build settles it (the retry invariant).

Both engines (the record is engine-shared metadata); the fix lives in each
incremental layer's on_build_end.
"""

import shutil
import sys

import pytest

from brix import AtomicBuilder, IncrementalBuilder

from test_incremental import make_graph, write_workspace

POISON = "import sys; sys.exit(7)\n"
CONCAT_OK = (
    "import sys\nfrom pathlib import Path\n"
    "parts=[Path(p).read_text() for p in sys.argv[1:-1]]\n"
    "Path(sys.argv[-1]).write_text(''.join(parts))\n"
)


def _poison_combine(root):
    (root / "tools/concat.py").write_text(POISON)


def _fix_combine(root):
    (root / "tools/concat.py").write_text(CONCAT_OK)


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_member_from_failed_build_then_deleted_is_cleaned(tmp_path, monkeypatch, engine):
    """The core leak: `c` is built by a build that then fails, so its member was
    never committed; deleting `c`'s source must still clean its output."""
    monkeypatch.setenv("BRIX_ENGINE", engine)
    write_workspace(tmp_path, {"a": "x", "b": "y"})

    assert IncrementalBuilder(make_graph, root=tmp_path).build().success  # [a, b]

    # Add c; its output builds, but the combine step fails the overall build.
    (tmp_path / "src/c.txt").write_text("z")
    _poison_combine(tmp_path)
    assert not IncrementalBuilder(make_graph, root=tmp_path).build().success
    assert (tmp_path / "build/upper/c.txt").exists()  # produced by the failed build

    # Delete c's source and let the build succeed: c's output must be gone —
    # matching a clean build, which would never have produced it.
    (tmp_path / "src/c.txt").unlink()
    _fix_combine(tmp_path)
    result = IncrementalBuilder(make_graph, root=tmp_path).build()
    assert result.success
    assert not (tmp_path / "build/upper/c.txt").exists()
    # The cleanup was a real, visible delete node (so it also stages under atomic).
    assert any("build/upper/c.txt" in n.label for n in result.plan.nodes
               if n.label.startswith("rm "))


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_vanished_member_survives_a_failed_attempt(tmp_path, monkeypatch, engine):
    """Retry invariant: a member whose deletion coincides with a failed build is
    not forgotten — it is cleaned once a build succeeds. (Union must not drop
    a pending-deletion member.)"""
    monkeypatch.setenv("BRIX_ENGINE", engine)
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    assert IncrementalBuilder(make_graph, root=tmp_path).build().success

    # Delete b's source while the build fails for an unrelated reason.
    (tmp_path / "src/b.txt").unlink()
    _poison_combine(tmp_path)
    assert not IncrementalBuilder(make_graph, root=tmp_path).build().success

    # Next successful build must have cleaned b's output regardless.
    _fix_combine(tmp_path)
    assert IncrementalBuilder(make_graph, root=tmp_path).build().success
    assert not (tmp_path / "build/upper/b.txt").exists()

    # Settled: nothing left to delete.
    third = IncrementalBuilder(make_graph, root=tmp_path).build()
    assert not any(n.label.startswith("rm ") for n in third.plan.nodes)


def _atomic(tmp_path):
    return AtomicBuilder(
        IncrementalBuilder(make_graph, root=tmp_path, metadata_dir="build/.brix"),
        staging_dir="build/.brix-staging",
    )


def test_atomic_mode_stays_clean_and_builds_succeed(tmp_path):
    """Staged builds are already protected (a failed build's staging is wiped
    before it reaches the workspace); the fix must not perturb that."""
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    assert _atomic(tmp_path).build().success
    assert (tmp_path / "build/upper/a.txt").exists()

    # Add c, fail the build: staged, never committed to the workspace.
    (tmp_path / "src/c.txt").write_text("z")
    _poison_combine(tmp_path)
    assert not _atomic(tmp_path).build().success
    assert not (tmp_path / "build/upper/c.txt").exists()  # never reached workspace

    # Delete c and succeed: still clean, and the build works end to end.
    (tmp_path / "src/c.txt").unlink()
    _fix_combine(tmp_path)
    assert _atomic(tmp_path).build().success
    assert not (tmp_path / "build/upper/c.txt").exists()
    assert (tmp_path / "build/all.txt").read_text() == "XY"


# --- nested: the docstring's formerly-"unrecoverable" case -----------------

COPY_POISON_AWARE = (
    "import sys\n"
    "from pathlib import Path\n"
    "text = Path(sys.argv[1]).read_text()\n"
    "assert 'FAIL' not in text\n"  # a poison scene fails the render
    "Path(sys.argv[2]).write_text(text.upper())\n"
)


def _nested_graph():
    from brix import ExpandAll, ExternalProcessAction, Graph

    graph = Graph()
    d = graph.data
    graph.add(
        ExpandAll(
            element_name="video",
            collection=d.glob("videos/{video:*}.py"),
            child=ExpandAll(
                element_name="scene",
                collection=d.glob("videos/{video:*}/{scene:*}.py"),
                child=ExternalProcessAction(
                    name="render {video}/{scene}",
                    command_line="{python} {tool} {source} {out}",
                    inputs={
                        "python": d.tool(sys.executable),
                        "tool": d.file("tools/copy.py"),
                        "source": d.file("videos/{video}/{scene}.py"),
                    },
                    outputs={"out": d.file("build/{video}/{scene}.out")},
                ),
            ),
        )
    )
    return graph


def _write_nested(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/copy.py").write_text(COPY_POISON_AWARE)
    (tmp_path / "videos/alpha").mkdir(parents=True)
    (tmp_path / "videos/alpha.py").write_text("# alpha\n")
    (tmp_path / "videos/alpha/01.py").write_text("alpha 01")


@pytest.mark.parametrize("engine", ["rust", "py"])
def test_nested_member_from_failed_build_then_deleted_is_cleaned(tmp_path, monkeypatch, engine):
    """A whole video added by a failed build, then deleted: its nested scene
    output must be cleaned. This needs the failed build to have recorded the
    video's inner scene membership — which the union-on-failure fix provides."""
    monkeypatch.setenv("BRIX_ENGINE", engine)
    _write_nested(tmp_path)
    assert IncrementalBuilder(_nested_graph, root=tmp_path).build().success

    # Add video gamma with a good scene (renders, output written) and a poison
    # scene (fails the render) -> the overall build fails after gamma/01 built.
    (tmp_path / "videos/gamma").mkdir()
    (tmp_path / "videos/gamma.py").write_text("# gamma\n")
    (tmp_path / "videos/gamma/01.py").write_text("gamma 01")
    (tmp_path / "videos/gamma/02.py").write_text("gamma 02 FAIL")
    assert not IncrementalBuilder(_nested_graph, root=tmp_path).build().success
    assert (tmp_path / "build/gamma/01.out").exists()  # produced by the failed build

    # Delete video gamma entirely and succeed: its scene output must be gone.
    (tmp_path / "videos/gamma.py").unlink()
    shutil.rmtree(tmp_path / "videos/gamma")
    assert IncrementalBuilder(_nested_graph, root=tmp_path).build().success
    assert not (tmp_path / "build/gamma/01.out").exists()
