"""AtomicBuilder: either the whole build lands, or nothing on disk changes."""

import sys

from brix import AtomicBuilder, CleanBuilder, IncrementalBuilder, compute_status
from brix.atomic import COMMIT_MARKER

from test_incremental import make_graph, runs, write_workspace

STAGING = "build/staging"


def atomic_clean(tmp_path):
    return AtomicBuilder(CleanBuilder(make_graph(), root=tmp_path))


def atomic_incremental(tmp_path):
    return AtomicBuilder(IncrementalBuilder(make_graph, root=tmp_path))


def test_success_commits_and_clears_staging(tmp_path):
    write_workspace(tmp_path, {"a": "hi ", "b": "there"})
    result = atomic_clean(tmp_path).build()
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "HI THERE"
    assert (tmp_path / "build/upper/a.txt").read_text() == "HI "
    assert not (tmp_path / STAGING).exists()


def test_downstream_reads_staged_upstream_outputs(tmp_path):
    """combine consumes upper's outputs, which exist only in staging until
    commit — the overlay must make it see them."""
    write_workspace(tmp_path, {"a": "one ", "b": "two"})
    result = atomic_clean(tmp_path).build()
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "ONE TWO"


def test_failure_leaves_workspace_untouched(tmp_path):
    write_workspace(tmp_path, {"a": "good", "b": "bad"})
    builder = atomic_incremental(tmp_path)
    assert builder.build().success
    committed = (tmp_path / "build/all.txt").read_text()

    # Change both sources, but make the tool fail on the second.
    (tmp_path / "src/a.txt").write_text("NEW A")
    (tmp_path / "src/b.txt").write_text("NEW B")
    (tmp_path / "tools/upper.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        'Path("runs.log").open("a").write("upper:" + sys.argv[1] + "\\n")\n'
        'if "b.txt" in sys.argv[1]: raise SystemExit(2)\n'
        "Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text().upper())\n"
    )
    result = atomic_incremental(tmp_path).build()
    assert not result.success
    # Nothing changed in the workspace — not even upper a's successful output.
    assert (tmp_path / "build/all.txt").read_text() == committed
    assert (tmp_path / "build/upper/a.txt").read_text() == "GOOD"
    # The successful part of the failed build is staged, inspectable.
    assert (tmp_path / STAGING / "add/build/upper/a.txt").read_text() == "NEW A"


def test_status_reports_staged_state_and_next_build_discards_it(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    (tmp_path / "tools/concat.py").write_text("raise SystemExit(3)\n")
    assert not atomic_incremental(tmp_path).build().success

    report = compute_status(make_graph, root=tmp_path)
    assert report.has_staged_state
    assert "build/upper/a.txt" in report.staged_files
    assert not report.is_clean

    # Fix the tool; the next build discards stale staging and succeeds.
    write_workspace(tmp_path, {"a": "x"})
    assert atomic_incremental(tmp_path).build().success
    assert not (tmp_path / STAGING).exists()
    assert compute_status(make_graph, root=tmp_path).is_clean


def test_atomic_composes_with_incremental(tmp_path):
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})
    assert atomic_incremental(tmp_path).build().success
    after_first = runs(tmp_path)

    # Nothing changed: no processes, nothing staged, still committed.
    assert atomic_incremental(tmp_path).build().success
    assert runs(tmp_path) == after_first
    assert not (tmp_path / STAGING).exists()
    assert (tmp_path / "build/all.txt").read_text() == "AABB"

    # One edit: only its chain reruns; result commits.
    (tmp_path / "src/a.txt").write_text("changed ")
    assert atomic_incremental(tmp_path).build().success
    counts = runs(tmp_path)
    assert counts["upper:src/a.txt"] == 2
    assert counts["upper:src/b.txt"] == 1
    assert (tmp_path / "build/all.txt").read_text() == "CHANGED BB"


def test_deleted_source_deletion_is_staged_then_committed(tmp_path):
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})
    assert atomic_incremental(tmp_path).build().success
    (tmp_path / "src/b.txt").unlink()
    result = atomic_incremental(tmp_path).build()
    assert result.success
    # The stale output is gone from the workspace only via commit,
    # and combine never saw it through the overlay.
    assert not (tmp_path / "build/upper/b.txt").exists()
    assert (tmp_path / "build/all.txt").read_text() == "AA"
    assert not (tmp_path / STAGING).exists()


def test_generated_collection_realizes_through_staging(tmp_path):
    """Generator writes into staging; ExpandAll must see the staged files."""
    from brix import ExpandAll, ExternalProcessAction, Graph

    def graph():
        g = Graph()
        data = g.data
        g.add(
            ExternalProcessAction(
                name="generate",
                command_line="{python} {tool} {out_dir} alpha beta",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/generate.py"),
                },
                outputs={
                    # Declared as an output so it renders through output_root:
                    # the generator writes into staging, not the workspace.
                    "out_dir": data.file("build/generated"),
                    "generated": data.glob("build/generated/{name}.txt"),
                },
            ),
            ExpandAll(
                element_name="name",
                collection=data.glob("build/generated/{name}.txt"),
                child=ExternalProcessAction(
                    name="upper {name}",
                    command_line="{python} {tool} {source} {dest}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "tool": data.file("tools/upper.py"),
                        "source": data.file("build/generated/{name}.txt"),
                    },
                    outputs={"dest": data.file("build/upper/{name}.txt")},
                ),
            ),
        )
        return g

    write_workspace(tmp_path, {})
    (tmp_path / "tools/generate.py").write_text(
        "import os, sys\n"
        "os.makedirs(sys.argv[1], exist_ok=True)\n"
        "for name in sys.argv[2:]:\n"
        "    open(os.path.join(sys.argv[1], name + '.txt'), 'w').write(name)\n"
    )
    result = AtomicBuilder(CleanBuilder(graph(), root=tmp_path)).build()
    assert result.success
    assert (tmp_path / "build/upper/alpha.txt").read_text() == "ALPHA"
    assert (tmp_path / "build/generated/beta.txt").read_text() == "beta"
    assert not (tmp_path / STAGING).exists()


def test_interrupted_commit_is_completed_on_next_build(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    # Simulate a crash mid-commit: staged output + COMMIT marker, half-committed.
    staged = tmp_path / STAGING / "add/build/half.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("recovered")
    (tmp_path / STAGING / COMMIT_MARKER).touch()

    report = compute_status(make_graph, root=tmp_path)
    assert report.interrupted_commit

    assert atomic_incremental(tmp_path).build().success
    assert (tmp_path / "build/half.txt").read_text() == "recovered"
    assert not (tmp_path / STAGING).exists()


def test_stale_staging_without_marker_is_discarded(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    garbage = tmp_path / STAGING / "add/build/garbage.txt"
    garbage.parent.mkdir(parents=True)
    garbage.write_text("should never land")

    assert atomic_incremental(tmp_path).build().success
    assert not (tmp_path / "build/garbage.txt").exists()
    assert not (tmp_path / STAGING).exists()
