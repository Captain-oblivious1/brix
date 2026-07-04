"""Status: build-state inspection without building, and the CLI over it."""

import sys

from brix import IncrementalBuilder, cli, compute_status
from brix.status import (
    FAILED,
    FRESH,
    INPUTS_CHANGED,
    NEVER_BUILT,
    OUTDATED,
    OUTPUTS_MISSING,
)

from test_incremental import make_graph, runs, write_workspace


def by_label(report):
    return {a.label: a for a in report.actions}


def test_never_built_everything_stale(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    report = compute_status(make_graph, root=tmp_path)
    assert not report.is_clean
    assert all(a.state == NEVER_BUILT for a in report.actions)
    assert {a.label for a in report.actions} == {"upper a", "upper b", "combine"}


def test_after_build_everything_fresh(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    IncrementalBuilder(make_graph, root=tmp_path).build()
    report = compute_status(make_graph, root=tmp_path)
    assert report.is_clean
    assert all(a.state == FRESH for a in report.actions)


def test_status_does_not_build_or_run(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    compute_status(make_graph, root=tmp_path)
    assert runs(tmp_path) == {}  # no process ran
    assert not (tmp_path / "build").exists() or not (tmp_path / "build/all.txt").exists()


def test_edited_source_stales_action_and_outdates_downstream(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    IncrementalBuilder(make_graph, root=tmp_path).build()
    (tmp_path / "src/a.txt").write_text("CHANGED")
    report = by_label(compute_status(make_graph, root=tmp_path))
    assert report["upper a"].state == INPUTS_CHANGED
    assert report["upper b"].state == FRESH
    # combine's inputs haven't changed *yet*, but upstream will rebuild.
    assert report["combine"].state == OUTDATED


def test_deleted_output_reported(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    IncrementalBuilder(make_graph, root=tmp_path).build()
    (tmp_path / "build/upper/a.txt").unlink()
    report = by_label(compute_status(make_graph, root=tmp_path))
    assert report["upper a"].state == OUTPUTS_MISSING


def test_deleted_source_reports_pending_deletion(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    IncrementalBuilder(make_graph, root=tmp_path).build()
    (tmp_path / "src/b.txt").unlink()
    report = compute_status(make_graph, root=tmp_path)
    (deletion,) = report.pending_deletions
    assert deletion.member == "b"
    assert deletion.paths == ["build/upper/b.txt"]
    assert by_label(report)["combine"].state == OUTDATED


def test_failed_action_reported_with_result(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    (tmp_path / "tools/upper.py").write_text(
        "import sys; print('cannot upper', file=sys.stderr); sys.exit(4)\n"
    )
    IncrementalBuilder(make_graph, root=tmp_path).build()
    report = by_label(compute_status(make_graph, root=tmp_path))
    assert report["upper a"].state == FAILED
    assert report["upper a"].last_result.exit_code == 4
    assert "cannot upper" in report["upper a"].last_result.stderr


def test_cli_status_and_errors_and_build(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})

    assert cli.main(make_graph, root=tmp_path, argv=["status"]) == 1  # work pending
    assert "never built" in capsys.readouterr().out

    assert cli.main(make_graph, root=tmp_path, argv=["build", "--silent"]) == 0
    assert capsys.readouterr().out == ""

    assert cli.main(make_graph, root=tmp_path, argv=["status"]) == 0  # clean
    assert "fresh" in capsys.readouterr().out

    assert cli.main(make_graph, root=tmp_path, argv=["errors"]) == 0
    assert "no recorded failures" in capsys.readouterr().out


def test_cli_errors_replays_failure_diagnostics(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    (tmp_path / "tools/upper.py").write_text(
        "import sys; print('boom line 7', file=sys.stderr); sys.exit(4)\n"
    )
    assert cli.main(make_graph, root=tmp_path, argv=["build", "--silent"]) == 1
    capsys.readouterr()
    assert cli.main(make_graph, root=tmp_path, argv=["errors"]) == 1
    out = capsys.readouterr().out
    assert "FAILED (exit 4)" in out
    assert "boom line 7" in out  # stderr replayed without rebuilding


def test_cli_clean_removes_outputs_and_metadata(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    cli.main(make_graph, root=tmp_path, argv=["build", "--silent"])
    assert (tmp_path / "build/all.txt").exists()
    assert cli.main(make_graph, root=tmp_path, argv=["clean"]) == 0
    assert not (tmp_path / "build/all.txt").exists()
    assert not (tmp_path / "build/upper/a.txt").exists()
    assert not (tmp_path / "build/.brix").exists()
    # After clean, status is back to never-built.
    capsys.readouterr()
    assert cli.main(make_graph, root=tmp_path, argv=["status"]) == 1


def test_cli_graph_prints_plan(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    assert cli.main(make_graph, root=tmp_path, argv=["graph"]) == 0
    out = capsys.readouterr().out
    assert "upper {name}" in out
    assert "combine" in out
