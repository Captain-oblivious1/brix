"""Build-state inspection without building.

`compute_status` evaluates the graph against persisted metadata and the
workspace as it is *right now*: which actions are fresh, which are stale
and why, which failed last time, and which stale outputs are pending
deletion because their source vanished. Nothing executes; this is exactly
the freshness decision the incremental layer would make at node-ready
time, asked hypothetically.

Staleness also propagates: an action whose fingerprint currently matches
is still reported ``outdated`` if something upstream will rebuild, since
its inputs are about to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .data import FileData
from .graph import Graph
from .incremental import (
    action_fingerprint,
    action_key,
    membership_key,
    outputs_intact,
)
from .layers import NodeContext
from .metadata import BuildMetadata, TimeAndHashChangeDetector
from .plan import NodeKind
from .planner import plan_graph
from .process import ProcessResult

# Realization logic lives with the scheduler; status asks the same question
# ("what are the members right now?") without executing anything.
from .executor import _collection_members

FRESH = "fresh"
NEVER_BUILT = "never built"
INPUTS_CHANGED = "inputs changed"
OUTPUTS_MISSING = "outputs missing or modified"
FAILED = "failed last build"
OUTDATED = "outdated"


@dataclass
class ActionStatus:
    label: str
    state: str
    detail: str = ""
    last_result: ProcessResult | None = None

    @property
    def is_fresh(self) -> bool:
        return self.state == FRESH


@dataclass
class PendingDeletion:
    expansion: str
    member: str
    paths: list[str]


@dataclass
class StatusReport:
    actions: list[ActionStatus] = field(default_factory=list)
    pending_deletions: list[PendingDeletion] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.pending_deletions and all(a.is_fresh for a in self.actions)

    @property
    def failures(self) -> list[ActionStatus]:
        return [a for a in self.actions if a.state == FAILED]

    def render(self) -> str:
        lines = []
        for a in self.actions:
            detail = f"  ({a.detail})" if a.detail else ""
            lines.append(f"{a.state:>27}:  {a.label}{detail}")
        for d in self.pending_deletions:
            paths = " ".join(d.paths)
            lines.append(
                f"{'pending deletion':>27}:  {paths}  (member {d.member!r} left {d.expansion})"
            )
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.state] = counts.get(a.state, 0) + 1
        summary = ", ".join(f"{n} {state}" for state, n in sorted(counts.items()))
        if self.pending_deletions:
            summary += f", {len(self.pending_deletions)} pending deletion(s)"
        lines.append(summary if lines else "nothing to build")
        return "\n".join(lines)


def _action_status(action, ctx, detector, metadata) -> ActionStatus:
    key = action_key(action)
    record = metadata.action_record(key)
    if record is None:
        return ActionStatus(label=action.label, state=NEVER_BUILT)
    last_result = ProcessResult(**record["result"]) if record.get("result") else None
    if not record.get("ok", True):
        detail = ""
        if last_result is not None:
            detail = last_result.summary().removeprefix("FAILED ").strip()
        return ActionStatus(action.label, FAILED, detail=detail, last_result=last_result)
    if record.get("fingerprint") != action_fingerprint(action, ctx, detector):
        return ActionStatus(action.label, INPUTS_CHANGED, last_result=last_result)
    if not outputs_intact(record, ctx, detector):
        return ActionStatus(action.label, OUTPUTS_MISSING, last_result=last_result)
    return ActionStatus(action.label, FRESH, last_result=last_result)


def compute_status(
    graph_supplier,
    root: str | Path = ".",
    metadata_dir: str | Path = "build/.brix",
) -> StatusReport:
    graph: Graph = graph_supplier() if callable(graph_supplier) else graph_supplier
    root = Path(root).resolve()
    plan = plan_graph(graph, root)
    metadata = BuildMetadata(root, metadata_dir)
    metadata.load()
    detector = TimeAndHashChangeDetector(root, metadata)
    ctx = NodeContext.for_root(root)

    report = StatusReport()
    node_entries: dict[int, list[int]] = {}  # plan node id -> indices in report.actions
    node_dirty: dict[int, bool] = {}

    for node in plan.nodes:
        indices: list[int] = []
        dirty = False
        if node.kind is NodeKind.EXPAND:
            expand = node.action
            members = _collection_members(expand, ctx)
            previous = metadata.membership(membership_key(expand)) or []
            for member in sorted(set(previous) - set(members)):
                vanished = expand.child.substitute({expand.element_name: member})
                paths = [
                    atom.path
                    for expr in vanished.outputs.values()
                    for atom in expr.atoms()
                    if isinstance(atom, FileData) and atom.pattern.is_concrete
                ]
                report.pending_deletions.append(
                    PendingDeletion(expansion=node.label, member=member, paths=paths)
                )
                dirty = True
            for member in members:
                concrete = expand.child.substitute({expand.element_name: member})
                status = _action_status(concrete, ctx, detector, metadata)
                indices.append(len(report.actions))
                report.actions.append(status)
                dirty = dirty or not status.is_fresh
        else:
            status = _action_status(node.action, ctx, detector, metadata)
            indices.append(len(report.actions))
            report.actions.append(status)
            dirty = not status.is_fresh
        node_entries[node.id] = indices
        node_dirty[node.id] = dirty

    # Propagate: anything downstream of dirty work is outdated even if its
    # own fingerprint currently matches — its inputs are about to change.
    changed = True
    while changed:
        changed = False
        for edge in plan.edges:
            if node_dirty.get(edge.from_id) and not node_dirty.get(edge.to_id):
                node_dirty[edge.to_id] = True
                changed = True
                for i in node_entries.get(edge.to_id, []):
                    if report.actions[i].is_fresh:
                        report.actions[i].state = OUTDATED
                        report.actions[i].detail = "upstream will rebuild"

    return report
