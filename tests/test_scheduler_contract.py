"""The scheduler contract, pinned as an executable spec.

`docs/rust-port.md` flags a handful of scheduler subtleties as "only partially
pinned by tests" and reserves phase 3 for full-strength review: the
event-ordering contract, the stuck-node **snapshot** rule, and the
on-success-only failure cascade. This file makes each explicit, so the
eventual Rust scheduler port has a concrete net to pass — either engine must
satisfy these, and a plausible-but-wrong reimplementation trips here rather
than in a user's build.
"""

import sys

from brix import (
    CleanBuilder,
    EdgeTrigger,
    ExecutionPlan,
    ExpandAll,
    ExternalProcessAction,
    Graph,
    IncrementalBuilder,
    NodeKind,
    NodeState,
    Reporter,
)
from brix.executor import execute


def ok_action(name):
    return ExternalProcessAction(name=name, command_line=f"{sys.executable} -c 'pass'")


def fail_action(name="boom"):
    return ExternalProcessAction(name=name, command_line=f"{sys.executable} -c 'raise SystemExit(1)'")


class Recorder(Reporter):
    """Captures the full event stream so ordering can be asserted."""

    def __init__(self):
        self.events = []

    def build_begin(self, plan):
        self.events.append(("begin",))

    def node_started(self, node):
        self.events.append(("started", node.label))

    def process_started(self, node):
        self.events.append(("process", node.label))

    def node_completed(self, node, status, result, error=None):
        self.events.append(("completed", node.label))

    def node_skipped(self, node, why):
        self.events.append(("skipped", node.label))

    def collection_realized(self, node, members, children):
        self.events.append(("realized", node.label))

    def node_inserted(self, node, reason):
        self.events.append(("inserted", node.label))

    def build_end(self, result):
        self.events.append(("end",))

    def index(self, kind, label=None):
        for i, e in enumerate(self.events):
            if e[0] == kind and (label is None or (len(e) > 1 and e[1] == label)):
                return i
        raise AssertionError(f"no {kind} {label!r} in {self.events}")


# -- workspace helpers -------------------------------------------------------


def _upper_workspace(tmp_path, names=("a",)):
    (tmp_path / "src").mkdir(exist_ok=True)
    for n in names:
        (tmp_path / "src" / f"{n}.txt").write_text(n)
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools/upper.py").write_text(
        "import sys\nopen(sys.argv[2],'w').write(open(sys.argv[1]).read().upper())\n"
    )
    (tmp_path / "tools/concat.py").write_text(
        "import sys\n*srcs,dst=sys.argv[1:]\n"
        "open(dst,'w').write(''.join(open(s).read() for s in srcs))\n"
    )


def _expand_graph():
    g = Graph()
    d = g.data
    g.add(
        ExpandAll(
            "name",
            d.glob("src/{name}.txt"),
            ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": d.tool(sys.executable),
                    "tool": d.file("tools/upper.py"),
                    "source": d.file("src/{name}.txt"),
                },
                outputs={"dest": d.file("build/upper/{name}.txt")},
            ),
        )
    )
    return g


def _expand_then_combine_graph():
    g = Graph()
    d = g.data
    g.add(
        ExpandAll(
            "name",
            d.glob("src/{name}.txt"),
            ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": d.tool(sys.executable),
                    "tool": d.file("tools/upper.py"),
                    "source": d.file("src/{name}.txt"),
                },
                outputs={"dest": d.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tools/concat.py"),
                "parts": d.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": d.file("build/all.txt")},
        ),
    )
    return g


# -- event ordering ----------------------------------------------------------


def test_build_begin_is_first_and_build_end_is_last(tmp_path):
    _upper_workspace(tmp_path)
    r = Recorder()
    assert CleanBuilder(_expand_graph(), root=tmp_path).build(reporter=r, jobs=1).success
    assert r.events[0] == ("begin",)
    assert r.events[-1] == ("end",)


def test_per_node_started_then_process_then_completed(tmp_path):
    _upper_workspace(tmp_path)
    r = Recorder()
    assert CleanBuilder(_expand_graph(), root=tmp_path).build(reporter=r, jobs=1).success
    # For one node the order is fixed: node_started (scheduler) < process_started
    # (worker, before the spawn) < node_completed (scheduler, after the future).
    assert r.index("started", "upper a") < r.index("process", "upper a") < r.index(
        "completed", "upper a"
    )


def test_collection_realized_precedes_its_children(tmp_path):
    _upper_workspace(tmp_path)
    r = Recorder()
    assert CleanBuilder(_expand_graph(), root=tmp_path).build(reporter=r, jobs=1).success
    assert r.index("realized") < r.index("started", "upper a")


# -- stuck-node snapshot rule ------------------------------------------------


def test_two_node_cycle_reports_both_nodes_not_one(tmp_path):
    """The snapshot rule: every stuck node is judged against the same pre-advance
    state, so BOTH cycle nodes are 'unreachable'. A naive advance-then-rejudge
    would flip the first to SKIPPED and then quiet-skip the second."""
    plan = ExecutionPlan(root=tmp_path)
    a = plan.add_node(NodeKind.PROCESS, "a", ok_action("a"))
    b = plan.add_node(NodeKind.PROCESS, "b", ok_action("b"))
    plan.add_edge(a.id, b.id, "a->b")
    plan.add_edge(b.id, a.id, "b->a")

    result = execute(plan)
    assert not result.success
    unreachable = [e for e in result.errors if "unreachable" in e]
    assert len(unreachable) == 2  # both, not one
    assert result.states.status(a.id) is NodeState.SKIPPED
    assert result.states.status(b.id) is NodeState.SKIPPED


def test_stuck_node_with_terminal_predecessor_is_a_quiet_skip(tmp_path):
    """The other half of the rule: all-terminal predecessors -> quiet skip, not
    an 'unreachable' error. `boom` fails, cascade-skips `a` (on-success); `b`
    waits on `a` via on-completion, which never fires (a is SKIPPED), so `b`
    goes stuck with a terminal predecessor."""
    plan = ExecutionPlan(root=tmp_path)
    boom = plan.add_node(NodeKind.PROCESS, "boom", fail_action())
    a = plan.add_node(NodeKind.PROCESS, "a", ok_action("a"))
    b = plan.add_node(NodeKind.PROCESS, "b", ok_action("b"))
    plan.add_edge(boom.id, a.id, "boom->a")
    plan.add_edge(a.id, b.id, "a->b", trigger=EdgeTrigger.ON_COMPLETION)

    result = execute(plan)
    assert not result.success  # boom failed
    assert result.states.status(a.id) is NodeState.SKIPPED  # cascade
    assert result.states.status(b.id) is NodeState.SKIPPED  # stuck -> quiet
    assert not any("unreachable" in e for e in result.errors)  # not an error


# -- on-success-only failure cascade -----------------------------------------


def test_failure_cascades_transitively_along_on_success_edges(tmp_path):
    """A failure skips the whole downstream on-success chain, and those
    cleanly-cascaded nodes are NOT reported as 'unreachable' (distinct from
    stuck)."""
    plan = ExecutionPlan(root=tmp_path)
    boom = plan.add_node(NodeKind.PROCESS, "boom", fail_action())
    a = plan.add_node(NodeKind.PROCESS, "a", ok_action("a"))
    b = plan.add_node(NodeKind.PROCESS, "b", ok_action("b"))
    c = plan.add_node(NodeKind.PROCESS, "c", ok_action("c"))
    plan.add_edge(boom.id, a.id, "boom->a")
    plan.add_edge(a.id, b.id, "a->b")
    plan.add_edge(b.id, c.id, "b->c")

    result = execute(plan)
    assert not result.success
    for node in (a, b, c):
        assert result.states.status(node.id) is NodeState.SKIPPED
    assert not any("unreachable" in e for e in result.errors)


def test_on_completion_reactivates_a_branch_past_a_failure(tmp_path):
    """The cascade walks only on-success edges: an on-completion successor of a
    failed node runs, and its own on-success successor then runs too."""
    plan = ExecutionPlan(root=tmp_path)
    boom = plan.add_node(NodeKind.PROCESS, "boom", fail_action())
    a = plan.add_node(NodeKind.PROCESS, "a", ok_action("a"))
    b = plan.add_node(NodeKind.PROCESS, "b", ok_action("b"))
    plan.add_edge(boom.id, a.id, "boom->a", trigger=EdgeTrigger.ON_COMPLETION)
    plan.add_edge(a.id, b.id, "a->b")

    result = execute(plan)
    assert not result.success  # boom still failed the build
    assert result.states.status(a.id) is NodeState.DONE  # ran despite the failure
    assert result.states.status(b.id) is NodeState.DONE  # branch reactivated


# -- insertion-channel ordering ----------------------------------------------


def test_proposed_cleanup_runs_before_the_expansions_consumer(tmp_path):
    """Layer proposals from on_collection_realized are inserted ordered *before*
    the expansion's consumers: when a source vanishes, the stale-output deletion
    runs before `combine` reads the collection again."""
    _upper_workspace(tmp_path, names=("a", "b"))
    builder = IncrementalBuilder(_expand_then_combine_graph, root=tmp_path)
    assert builder.build().success
    assert (tmp_path / "build/upper/b.txt").exists()

    (tmp_path / "src/b.txt").unlink()  # b vanishes -> its output must be cleaned up
    r = Recorder()
    assert builder.build(reporter=r).success

    # A cleanup node was inserted, and it completed before combine started.
    assert any(e[0] == "inserted" for e in r.events)
    delete_completed = min(
        i for i, e in enumerate(r.events) if e[0] == "completed" and e[1].startswith("rm ")
    )
    assert delete_completed < r.index("started", "combine")
    assert not (tmp_path / "build/upper/b.txt").exists()
