"""Edge trigger modes: on-success (default) vs on-completion."""

import sys

from brix import (
    EdgeTrigger,
    ExternalProcessAction,
)
from brix.actions import DeleteFilesAction
from brix.executor import execute
from brix.plan import ExecutionPlan, NodeKind, NodeState


def _failing_action():
    return ExternalProcessAction(
        name="boom",
        command_line=f"{sys.executable} -c 'raise SystemExit(1)'",
    )


def test_on_success_edge_skips_after_failure(tmp_path):
    (tmp_path / "junk.txt").write_text("stale")
    plan = ExecutionPlan(root=tmp_path)
    boom = plan.add_node(NodeKind.PROCESS, "boom", _failing_action())
    cleanup_action = DeleteFilesAction(["junk.txt"])
    cleanup = plan.add_node(NodeKind.PROCESS, cleanup_action.label, cleanup_action)
    plan.add_edge(boom.id, cleanup.id, "ordinary data edge")  # default trigger

    result = execute(plan)
    assert not result.success
    assert result.states.status(cleanup.id) is NodeState.SKIPPED
    assert (tmp_path / "junk.txt").exists()  # cleanup never ran


def test_on_completion_edge_fires_after_failure(tmp_path):
    (tmp_path / "junk.txt").write_text("stale")
    plan = ExecutionPlan(root=tmp_path)
    boom = plan.add_node(NodeKind.PROCESS, "boom", _failing_action())
    cleanup_action = DeleteFilesAction(["junk.txt"])
    cleanup = plan.add_node(NodeKind.PROCESS, cleanup_action.label, cleanup_action)
    plan.add_edge(
        boom.id, cleanup.id, "failure cleanup", trigger=EdgeTrigger.ON_COMPLETION
    )

    result = execute(plan)
    assert not result.success  # boom still failed the build
    assert result.states.status(boom.id) is NodeState.FAILED
    assert result.states.status(cleanup.id) is NodeState.DONE
    assert not (tmp_path / "junk.txt").exists()  # cleanup ran *because* boom completed


def test_on_completion_edge_also_fires_after_success(tmp_path):
    (tmp_path / "junk.txt").write_text("stale")
    plan = ExecutionPlan(root=tmp_path)
    ok = plan.add_node(
        NodeKind.PROCESS,
        "ok",
        ExternalProcessAction(name="ok", command_line=f"{sys.executable} -c 'pass'"),
    )
    cleanup_action = DeleteFilesAction(["junk.txt"])
    cleanup = plan.add_node(NodeKind.PROCESS, cleanup_action.label, cleanup_action)
    plan.add_edge(ok.id, cleanup.id, "cleanup", trigger=EdgeTrigger.ON_COMPLETION)

    result = execute(plan)
    assert result.success
    assert result.states.status(cleanup.id) is NodeState.DONE
    assert not (tmp_path / "junk.txt").exists()
