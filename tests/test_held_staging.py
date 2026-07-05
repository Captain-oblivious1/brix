"""Held staging: build --stage, review, then commit or discard."""

import pytest

from brix import AtomicBuilder, IncrementalBuilder, cli, compute_status
from brix.atomic import COMMIT_MARKER, HELD_MARKER

from test_incremental import UPPER_TOOL, make_graph, runs, write_workspace


def atomic(tmp_path):
    return AtomicBuilder(
        IncrementalBuilder(make_graph, root=tmp_path), staging_dir="build/staging"
    )


def test_hold_leaves_workspace_untouched(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    result = atomic(tmp_path).build(hold=True)
    assert result.success
    assert not (tmp_path / "build/all.txt").exists()
    assert not (tmp_path / "build/upper/a.txt").exists()
    staging = tmp_path / "build/staging"
    assert (staging / HELD_MARKER).exists()
    assert (staging / "add/build/all.txt").read_text() == "XY"


def test_status_reads_held_staging_through_the_overlay(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    assert atomic(tmp_path).build(hold=True).success
    report = compute_status(make_graph, root=tmp_path)
    assert report.held
    assert not report.is_clean  # the commit is pending work
    # Freshness is judged as the next build will see it: staged outputs
    # count as intact, so nothing reports "outputs missing".
    assert all(a.is_fresh for a in report.actions)
    assert "build/all.txt" in report.staged_files
    assert "awaiting commit" in report.render()


def test_commit_applies_and_clears(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    builder = atomic(tmp_path)
    assert builder.build(hold=True).success

    files, deletions = builder.commit()
    assert files == 3 and deletions == 0
    assert (tmp_path / "build/all.txt").read_text() == "XY"
    assert not (tmp_path / "build/staging").exists()
    assert compute_status(make_graph, root=tmp_path).is_clean
    assert builder.commit() is None  # nothing left


def test_plain_build_on_top_of_held_commits_everything(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    builder = atomic(tmp_path)
    assert builder.build(hold=True).success
    ran_before = runs(tmp_path)

    result = builder.build()  # no hold: replay on top, then commit all
    assert result.success
    assert runs(tmp_path) == ran_before  # nothing re-ran
    assert (tmp_path / "build/all.txt").read_text() == "X"
    assert not (tmp_path / "build/staging").exists()


def test_discard_throws_staging_away(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    builder = atomic(tmp_path)
    assert builder.build(hold=True).success

    assert builder.discard() == 2  # upper/a.txt + all.txt
    assert not (tmp_path / "build/staging").exists()
    assert not (tmp_path / "build/all.txt").exists()  # workspace untouched
    assert builder.discard() is None


def test_failure_on_top_of_held_never_commits(tmp_path):
    """A failed build mixes held outputs with its own leavings; that tree
    must never reach the workspace — commit refuses, next build discards."""
    write_workspace(tmp_path, {"a": "x"})
    builder = atomic(tmp_path)
    assert builder.build(hold=True).success

    (tmp_path / "tools/upper.py").write_text("import sys; sys.exit(3)\n")
    assert not builder.build().success
    assert not (tmp_path / "build/staging" / HELD_MARKER).exists()
    with pytest.raises(ValueError, match="failed build"):
        builder.commit()

    (tmp_path / "tools/upper.py").write_text(UPPER_TOOL)
    result = builder.build()  # discards the debris, rebuilds, commits
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "X"


def test_discard_refuses_an_interrupted_commit(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    builder = atomic(tmp_path)
    assert builder.build(hold=True).success
    (tmp_path / "build/staging" / COMMIT_MARKER).touch()  # simulate crash
    with pytest.raises(ValueError, match="interrupted commit"):
        builder.discard()


def test_cli_stage_commit_flow(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    factory = lambda: atomic(tmp_path)  # noqa: E731
    args = dict(root=tmp_path, builder_factory=factory)

    assert cli.main(make_graph, argv=["build", "--stage", "--silent"], **args) == 0
    assert not (tmp_path / "build/all.txt").exists()
    assert cli.main(make_graph, argv=["status"], **args) == 1  # commit pending
    assert "awaiting commit" in capsys.readouterr().out

    assert cli.main(make_graph, argv=["commit"], **args) == 0
    assert "committed 2 file(s)" in capsys.readouterr().out
    assert (tmp_path / "build/all.txt").exists()
    assert cli.main(make_graph, argv=["status"], **args) == 0

    assert cli.main(make_graph, argv=["commit"], **args) == 0
    assert "nothing staged" in capsys.readouterr().out


def test_cli_stage_and_commit_require_atomic_builder(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    # Default builder is a bare IncrementalBuilder: no staging to hold.
    assert cli.main(make_graph, root=tmp_path, argv=["build", "--stage"]) == 2
    assert "requires an AtomicBuilder" in capsys.readouterr().err
    assert cli.main(make_graph, root=tmp_path, argv=["commit"]) == 2
    assert "requires an AtomicBuilder" in capsys.readouterr().err
