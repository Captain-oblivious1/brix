"""The BuildLayer / NodeContext seams and the scheduler-owned state table."""

import sys

import pytest

from brix import (
    BuildLayer,
    CleanBuilder,
    ExpandAll,
    ExternalProcessAction,
    Graph,
    NodeState,
    NodeStateTable,
)


def test_state_table_defaults_to_pending():
    states = NodeStateTable()
    assert states.status(0) is NodeState.PENDING
    assert states.result(0) is None


def test_state_table_is_monotone():
    states = NodeStateTable()
    states.advance(0, NodeState.RUNNING)
    states.advance(0, NodeState.DONE)
    with pytest.raises(ValueError):
        states.advance(0, NodeState.RUNNING)  # backwards
    with pytest.raises(ValueError):
        states.advance(0, NodeState.FAILED)  # out of a terminal state
    with pytest.raises(ValueError):
        states.advance(0, NodeState.DONE)  # not even twice


class RecordingLayer(BuildLayer):
    """Trivial layer: observes everything, changes nothing."""

    def __init__(self):
        self.events = []

    def on_build_begin(self, plan, ctx):
        self.events.append("begin")

    def around_node(self, node, ctx, inner):
        self.events.append(f"around: {node.label}")
        return inner(ctx)

    def on_collection_realized(self, node, members, ctx):
        self.events.append(f"realized: {sorted(members)}")

    def on_build_end(self, result, ctx):
        self.events.append(f"end: ok={result.success}")


def _upper_graph():
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
                outputs={"dest": data.file("build/{name}.txt")},
            ),
        )
    )
    return graph


def _write_workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.txt").write_text("hi")
    (tmp_path / "src/b.txt").write_text("yo")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/upper.py").write_text(
        "import sys\n"
        "with open(sys.argv[1]) as f: text = f.read()\n"
        "with open(sys.argv[2], 'w') as f: f.write(text.upper())\n"
    )


def test_layer_hooks_fire_including_for_expanded_nodes(tmp_path):
    _write_workspace(tmp_path)
    layer = RecordingLayer()
    plan = CleanBuilder(_upper_graph(), root=tmp_path).plan()
    result = plan.run(echo=False, layers=[layer])
    assert result.success
    assert (tmp_path / "build/a.txt").read_text() == "HI"
    assert layer.events[0] == "begin"
    assert layer.events[-1] == "end: ok=True"
    assert "realized: ['a', 'b']" in layer.events
    # Nodes created by runtime expansion went through the stack too.
    assert "around: upper a" in layer.events
    assert "around: upper b" in layer.events


def test_layer_can_answer_without_running_a_process(tmp_path):
    """A shortcut is completing a node with a result obtained elsewhere."""
    from brix import ProcessResult

    class SkipEverything(BuildLayer):
        def around_node(self, node, ctx, inner):
            return ProcessResult(action_name=node.label, command="<skipped>", exit_code=0)

    _write_workspace(tmp_path)
    plan = CleanBuilder(_upper_graph(), root=tmp_path).plan()
    result = plan.run(echo=False, layers=[SkipEverything()])
    assert result.success
    # No process ran, so no output file was produced -- but every node is DONE.
    assert not (tmp_path / "build/a.txt").exists()
    assert all(result.states.status(n.id) is NodeState.DONE for n in result.plan.nodes)


def test_layer_can_redirect_output_root(tmp_path):
    """The atomic seam: outputs land where ctx.output_root points."""

    class Redirect(BuildLayer):
        def around_node(self, node, ctx, inner):
            from brix import NodeContext

            staged = NodeContext(
                workspace_root=ctx.workspace_root,
                output_root=ctx.workspace_root / "staging",
            )
            return inner(staged)

    _write_workspace(tmp_path)
    plan = CleanBuilder(_upper_graph(), root=tmp_path).plan()
    result = plan.run(echo=False, layers=[Redirect()])
    assert result.success
    assert (tmp_path / "staging/build/a.txt").read_text() == "HI"
    assert not (tmp_path / "build/a.txt").exists()


def test_stack_order_last_layer_is_outermost(tmp_path):
    order = []

    def make_layer(tag):
        class Tagged(BuildLayer):
            def around_node(self, node, ctx, inner):
                order.append(f">{tag}")
                result = inner(ctx)
                order.append(f"<{tag}")
                return result

        return Tagged()

    _write_workspace(tmp_path)
    (tmp_path / "src/b.txt").unlink()  # one node keeps ordering deterministic
    plan = CleanBuilder(_upper_graph(), root=tmp_path).plan()
    result = plan.run(
        echo=False, layers=[make_layer("incremental"), make_layer("atomic")]
    )
    assert result.success
    assert order == [">atomic", ">incremental", "<incremental", "<atomic"]
