"""The scheduler: runs an execution plan.

The scheduler owns all mutable build state: readiness, the failure cascade,
the node state table, and the insertion channel through which new nodes enter
the plan (runtime expansion today; layer-proposed cleanup later). Node
definitions are frozen; nodes never mutate graph structure themselves.

Capabilities attach as a `BuildLayer` stack: each ready process node is run
through every layer's `around_node` (innermost first in the given list, so
the last layer is outermost), and build-scoped events fire at the start, at
each collection realization, and at the end.
"""

from __future__ import annotations

import os
import shlex
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from .actions import DeleteFilesAction, ExpandAll, ExternalProcessAction
from .data import GlobData, RegexData
from .layers import BuildLayer, NodeContext, NodeRunner
from .plan import (
    EdgeTrigger,
    ExecutionNode,
    ExecutionPlan,
    NodeKind,
    NodeState,
    NodeStateTable,
)
from .process import ProcessResult, run_process
from .render import RenderError, prepare_output_dirs, render_command, render_env
from .report import Reporter

_TERMINAL = {NodeState.DONE, NodeState.FAILED, NodeState.SKIPPED}


def _run_delete(action: DeleteFilesAction, ctx: NodeContext) -> ProcessResult:
    deleted: list[str] = []
    try:
        for rel in action.paths:
            target = ctx.output_root / rel
            if target.exists():
                target.unlink()
                deleted.append(rel)
    except OSError as exc:
        return ProcessResult(
            action_name=action.label, command=action.label, exit_code=1,
            stderr=f"brix: cannot delete: {exc}",
        )
    return ProcessResult(
        action_name=action.label, command=action.label, exit_code=0,
        stdout="".join(f"deleted {p}\n" for p in deleted),
    )


def _run_process_node(node: ExecutionNode, ctx: NodeContext, workers) -> ProcessResult:
    action = node.action
    if isinstance(action, DeleteFilesAction):
        return _run_delete(action, ctx)
    assert isinstance(action, ExternalProcessAction)
    if action.worker is not None:
        # Same rendering as a spawn (overlay output-redirection included), but
        # the argv is dispatched to a worker instead of spawned. Env is not
        # part of the worker protocol; worker actions do not use env= in v1.
        try:
            prepare_output_dirs(action, ctx)
            args = shlex.split(render_command(action, ctx))
        except (RenderError, ValueError) as exc:
            return ProcessResult(action_name=action.label, command="", stderr=f"brix: {exc}")
        return workers.run(action.worker, action.label, args, ctx)
    try:
        prepare_output_dirs(action, ctx)
        command = render_command(action, ctx)
        env = render_env(action, ctx)
    except RenderError as exc:
        return ProcessResult(action_name=action.label, command="", stderr=f"brix: {exc}")
    if ctx.scrub:
        from .process import scrub_env

        env = scrub_env(env, set(action.env_used) | set(action.env_pass), action.env_unset)
        return run_process(
            action.label, command, cwd=ctx.workspace_root, env=env, authoritative=True,
        )
    return run_process(
        action.label, command, cwd=ctx.workspace_root,
        env=env or None, env_unset=action.env_unset or None,
    )


def _stacked_runner(
    node: ExecutionNode, layers: list[BuildLayer], reporter: Reporter, workers
) -> NodeRunner:
    """Wrap the process run in each layer's around_node, innermost first."""

    def base(ctx: NodeContext) -> ProcessResult:
        # Innermost of all: only reached when a real process is about to
        # spawn or a worker request is about to dispatch (a replaying layer
        # returns before this), so the reporter's liveness signal is exact.
        # Runs on a worker thread.
        reporter.process_started(node)
        return _run_process_node(node, ctx, workers)

    call = base
    for layer in layers:
        call = _wrap(layer, node, call)
    return call


def _wrap(layer: BuildLayer, node: ExecutionNode, inner: NodeRunner) -> NodeRunner:
    def wrapped(ctx: NodeContext) -> ProcessResult:
        return layer.around_node(node, ctx, inner)

    return wrapped


def _collection_members(action: ExpandAll, ctx: NodeContext) -> list[str]:
    """Realize the collection (through the overlay) and return sorted
    unique element bindings."""
    members: list[str] = []
    seen: set[str] = set()
    for atom in action.collection.atoms():
        if isinstance(atom, GlobData):
            found = [(logical, bindings) for logical, _, bindings in ctx.find(atom.pattern)]
        elif isinstance(atom, RegexData):
            found = [
                (logical, bindings)
                for logical, _ in ctx.iter_files()
                if (bindings := atom.match(logical)) is not None
            ]
        else:
            continue
        for _path, bindings in found:
            value = bindings.get(action.element_name)
            if value is None:
                raise RenderError(
                    f"collection {atom!r} does not bind {action.element_name!r}"
                )
            if value not in seen:
                seen.add(value)
                members.append(value)
    return sorted(members)


def _expand(
    plan: ExecutionPlan, node: ExecutionNode, ctx: NodeContext
) -> tuple[list[str], list[ExecutionNode]]:
    """Expand an ExpandAll node into concrete sibling process nodes."""
    action = node.action
    assert isinstance(action, ExpandAll)
    members = _collection_members(action, ctx)
    pred_ids = [p.id for p in plan.predecessors(node.id)]
    succ_ids = [s.id for s in plan.successors(node.id)]
    children: list[ExecutionNode] = []
    for value in members:
        concrete = action.child.substitute({action.element_name: value})
        # A nested ExpandAll becomes another expansion point, which the
        # scheduler will expand in turn once it is ready.
        kind = NodeKind.EXPAND if isinstance(concrete, ExpandAll) else NodeKind.PROCESS
        child_node = plan.add_node(kind, concrete.label, concrete)
        for pid in pred_ids:
            plan.add_edge(pid, child_node.id, f"expansion of [{node.id}]")
        for sid in succ_ids:
            plan.add_edge(child_node.id, sid, f"expanded member {value!r}")
        children.append(child_node)
    return members, children


def execute(
    plan: ExecutionPlan,
    jobs: int | None = None,
    layers: list[BuildLayer] | None = None,
    reporter: Reporter | None = None,
):
    from .builders import BuildResult
    from .workers import WorkerRegistry

    layers = list(layers or [])
    reporter = reporter if reporter is not None else Reporter()
    states = NodeStateTable()
    ctx = NodeContext.for_root(plan.root)
    # Scrub is a build-wide policy carried by the incremental layer (the parallel
    # of the Rust engine reading it from the incremental layer spec).
    ctx.scrub = any(getattr(layer, "scrub", False) for layer in layers)
    workers = WorkerRegistry()
    max_workers = jobs or min(32, os.cpu_count() or 4)
    pending: set[int] = {n.id for n in plan.nodes}
    running: dict[Future[ProcessResult], int] = {}
    results: list[ProcessResult] = []
    errors: list[str] = []

    def ready(node_id: int) -> bool:
        for edge in plan.incoming(node_id):
            status = states.status(edge.from_id)
            if edge.trigger is EdgeTrigger.ON_COMPLETION:
                if status not in (NodeState.DONE, NodeState.FAILED):
                    return False
            elif status is not NodeState.DONE:
                return False
        return True

    def _success_successor_ids(node_id: int) -> list[int]:
        return [
            e.to_id
            for e in plan.edges
            if e.from_id == node_id and e.trigger is EdgeTrigger.ON_SUCCESS
        ]

    def cascade_skip(from_id: int, why: str) -> None:
        # Only on-success edges cascade; on-completion successors still run.
        stack = _success_successor_ids(from_id)
        while stack:
            nid = stack.pop()
            if states.status(nid) is not NodeState.PENDING:
                continue
            states.advance(nid, NodeState.SKIPPED)
            pending.discard(nid)
            reporter.node_skipped(plan.nodes[nid], why)
            stack.extend(_success_successor_ids(nid))

    def insert_proposals(source: ExecutionNode, proposals) -> None:
        """The insertion channel: the scheduler inserts and wires proposed
        nodes, ordered before the source expansion's consumers."""
        consumer_ids = [s.id for s in plan.successors(source.id)]
        for proposal in proposals:
            label = proposal.label or getattr(proposal.action, "label", repr(proposal.action))
            new_node = plan.add_node(NodeKind.PROCESS, label, proposal.action)
            reason = f"cleanup proposed at [{source.id}]"
            for cid in consumer_ids:
                plan.add_edge(new_node.id, cid, reason)
            pending.add(new_node.id)
            reporter.node_inserted(new_node, reason)

    reporter.build_begin(plan)
    for layer in layers:
        layer.on_build_begin(plan, ctx)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or running:
            scheduled = False
            for nid in sorted(pending):
                node = plan.nodes[nid]
                if not ready(nid):
                    continue
                pending.discard(nid)
                scheduled = True
                if node.kind is NodeKind.EXPAND:
                    try:
                        members, children = _expand(plan, node, ctx)
                    except RenderError as exc:
                        states.advance(nid, NodeState.FAILED)
                        errors.append(f"{node.label}: {exc}")
                        reporter.node_completed(node, NodeState.FAILED, None, error=str(exc))
                        cascade_skip(nid, f"upstream failed: {node.label}")
                        continue
                    states.advance(nid, NodeState.DONE)
                    pending.update(c.id for c in children)
                    reporter.collection_realized(node, members, children)
                    for layer in layers:
                        proposals = layer.on_collection_realized(node, members, ctx)
                        if proposals:
                            insert_proposals(node, proposals)
                else:
                    states.advance(nid, NodeState.RUNNING)
                    reporter.node_started(node)
                    future = pool.submit(
                        _stacked_runner(node, layers, reporter, workers), ctx
                    )
                    running[future] = nid

            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    nid = running.pop(future)
                    node = plan.nodes[nid]
                    result = future.result()
                    results.append(result)
                    if result.ok:
                        states.advance(nid, NodeState.DONE, result)
                        reporter.node_completed(node, NodeState.DONE, result)
                    else:
                        states.advance(nid, NodeState.FAILED, result)
                        errors.append(result.summary())
                        reporter.node_completed(node, NodeState.FAILED, result)
                        cascade_skip(nid, f"upstream failed: {node.label}")
            elif not scheduled and pending:
                # Judge every stuck node against the same snapshot: advancing
                # one to SKIPPED must not soften the verdict on the next.
                verdicts = {
                    nid: all(
                        states.status(e.from_id) in _TERMINAL for e in plan.incoming(nid)
                    )
                    for nid in pending
                }
                for nid in sorted(pending):
                    node = plan.nodes[nid]
                    states.advance(nid, NodeState.SKIPPED)
                    if verdicts[nid]:
                        # e.g. an on-completion successor of a skipped node
                        reporter.node_skipped(node, "upstream skipped")
                    else:
                        errors.append(f"{node.label}: unreachable (dependency cycle?)")
                        reporter.node_skipped(node, "unreachable — dependency cycle?")
                pending.clear()

    # Scheduler owns worker lifecycles: all terminated at build end (v1).
    workers.terminate_all()
    success = not errors
    build_result = BuildResult(
        success=success, plan=plan, states=states, process_results=results, errors=errors
    )
    for layer in layers:
        layer.on_build_end(build_result, ctx)
    reporter.build_end(build_result)
    return build_result
