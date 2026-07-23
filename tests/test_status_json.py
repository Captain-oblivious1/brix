"""The machine-readable status document — `status --json` and to_public_dict.

Pins the public, versioned schema (docs/status-json.md): a stable contract for
web/IDE/CI frontends, independent of the internal engine wire boundary.
"""

import json

from brix import IncrementalBuilder, cli, compute_status
from brix.status import STATUS_SCHEMA_VERSION

from test_incremental import make_graph, runs, write_workspace


def _doc(report, **kw):
    return report.to_public_dict(**kw)


def test_schema_shape_when_never_built(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    doc = _doc(compute_status(make_graph, root=tmp_path))

    assert doc["schema"] == STATUS_SCHEMA_VERSION
    assert doc["clean"] is False
    # every action present, each with the fixed key set
    assert doc["counts"] == {"never built": 3}
    for a in doc["actions"]:
        assert set(a) == {"label", "state", "fresh", "detail", "last_result"}
        assert a["state"] == "never built"
        assert a["fresh"] is False
        assert a["last_result"] is None  # nothing recorded yet
    # staging block always present, empty here
    assert doc["staging"] == {
        "held": False,
        "interrupted_commit": False,
        "staged_files": [],
        "staged_deletions": [],
    }
    assert doc["pending_deletions"] == []


def test_to_public_dict_does_not_build(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    _doc(compute_status(make_graph, root=tmp_path))
    assert runs(tmp_path) == {}  # pure inspection


def test_clean_after_build_carries_results(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    IncrementalBuilder(make_graph, root=tmp_path).build()
    doc = _doc(compute_status(make_graph, root=tmp_path))

    assert doc["clean"] is True
    assert doc["counts"] == {"fresh": 3}
    for a in doc["actions"]:
        assert a["fresh"] is True
        # a successful run is recorded and exposed with derived ok/summary
        assert a["last_result"]["ok"] is True
        assert "summary" in a["last_result"]


def test_edited_source_shows_changed_and_outdated(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    IncrementalBuilder(make_graph, root=tmp_path).build()
    (tmp_path / "src/a.txt").write_text("CHANGED")
    doc = _doc(compute_status(make_graph, root=tmp_path))
    by_label = {a["label"]: a for a in doc["actions"]}

    assert doc["clean"] is False
    assert by_label["upper a"]["state"] == "inputs changed"
    assert by_label["upper b"]["state"] == "fresh"
    assert by_label["combine"]["state"] == "outdated"
    assert by_label["combine"]["detail"] == "upstream will rebuild"


def test_failed_action_exposes_result(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    (tmp_path / "tools/upper.py").write_text(
        "import sys; print('boom', file=sys.stderr); sys.exit(4)\n"
    )
    IncrementalBuilder(make_graph, root=tmp_path).build()
    doc = _doc(compute_status(make_graph, root=tmp_path))
    failed = [a for a in doc["actions"] if a["state"] == "failed last build"]

    assert failed, "the failing action should report as failed"
    result = failed[0]["last_result"]
    assert result["ok"] is False
    assert result["exit_code"] == 4
    assert "boom" in result["stderr"]


# --- the CLI surface --------------------------------------------------------

def test_cli_json_is_pure_parseable_json(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    code = cli.main(make_graph, root=tmp_path, argv=["status", "--json"])
    out = capsys.readouterr().out

    assert code == 1  # work pending
    # stdout is *only* the JSON document — no human text mixed in
    doc = json.loads(out)
    assert doc["schema"] == STATUS_SCHEMA_VERSION
    assert "never built" not in out.split("\n", 1)[0]  # no render() header line


def test_cli_json_clean_exit_zero_and_environment_block(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x"})
    cli.main(make_graph, root=tmp_path, argv=["build", "--silent"])
    capsys.readouterr()

    code = cli.main(make_graph, root=tmp_path, argv=["status", "--json"])
    doc = json.loads(capsys.readouterr().out)

    assert code == 0
    assert doc["clean"] is True
    # the environment diff folds in so a frontend gets it in one call
    assert set(doc["environment"]) == {"changed", "note"}
    assert isinstance(doc["environment"]["changed"], bool)
