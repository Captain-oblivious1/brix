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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from .actions import ExpandAll, ExternalProcessAction
from .data import GlobData, RegexData
from .layers import BuildLayer, NodeContext, NodeRunner
from .plan import ExecutionNode, ExecutionPlan, NodeKind, NodeState, NodeStateTable
from .process import ProcessResult, run_process
from .render import RenderError, prepare_output_dirs, render_command


def _run_process_node(node: ExecutionNode, ctx: NodeContext) -> ProcessResult:
    action = node.action
    assert isinstance(action, ExternalProcessAction)
    try:
        prepare_output_dirs(action, ctx)
        command = render_command(action, ctx)
    except RenderError as exc:
        return ProcessResult(action_name=action.label, command="", stderr=f"brix: {exc}")
    return run_process(action.label, command, cwd=ctx.workspace_root)


def _stacked_runner(node: ExecutionNode, layers: list[BuildLayer]) -> NodeRunner:
    """Wrap the process run in each layer's around_node, innermost first."""

    def base(ctx: NodeContext) -> ProcessResult:
        return _run_process_node(node, ctx)

    call = base
    for layer in layers:
        call = _wrap(layer, node, call)
    return call


def _wrap(layer: BuildLayer, node: ExecutionNode, inner: NodeRunner) -> NodeRunner:
    def wrapped(ctx: NodeContext) -> ProcessResult:
        return layer.around_node(node, ctx, inner)

    return wrapped


def _collection_members(action: ExpandAll, ctx: NodeContext) -> list[str]:
    """Realize the collection and return sorted unique element bindings."""
    root = ctx.workspace_root
    members: list[str] = []
    seen: set[str] = set()
    for atom in action.collection.atoms():
        if isinstance(atom, GlobData):
            found = atom.pattern.find(root)
        elif isinstance(atom, RegexData):
            found = [
                (rel, bindings)
                for rel in sorted(
                    p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
                )
                if (bindings := atom.match(rel)) is not None
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
        child_node = plan.add_node(NodeKind.PROCESS, concrete.label, concrete)
        for pid in pred_ids:
            plan.add_edge(pid, child_node.id, f"expansion of [{node.id}]")
        for sid in succ_ids:
            plan.add_edge(child_node.id, sid, f"expanded member {value!r}")
        children.append(child_node)
    return members, children


def execute(
    plan: ExecutionPlan,
    jobs: int | None = None,
    echo: bool = True,
    layers: list[BuildLayer] | None = None,
):
    from .builders import BuildResult

    layers = list(layers or [])
    states = NodeStateTable()
    ctx = NodeContext.for_root(plan.root)
    max_workers = jobs or min(32, os.cpu_count() or 4)
    pending: set[int] = {n.id for n in plan.nodes}
    running: dict[Future[ProcessResult], int] = {}
    results: list[ProcessResult] = []
    errors: list[str] = []

    def say(message: str) -> None:
        if echo:
            print(message)

    def ready(node_id: int) -> bool:
        return all(states.status(p.id) is NodeState.DONE for p in plan.predecessors(node_id))

    def cascade_skip(from_id: int, why: str) -> None:
        stack = [s.id for s in plan.successors(from_id)]
        while stack:
            nid = stack.pop()
            if states.status(nid) is not NodeState.PENDING:
                continue
            states.advance(nid, NodeState.SKIPPED)
            pending.discard(nid)
            say(f"skip: {plan.nodes[nid].label} ({why})")
            stack.extend(s.id for s in plan.successors(nid))

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
                        say(f"FAILED: {node.label}: {exc}")
                        cascade_skip(nid, f"upstream failed: {node.label}")
                        continue
                    states.advance(nid, NodeState.DONE)
                    for layer in layers:
                        layer.on_collection_realized(node, members, ctx)
                    pending.update(c.id for c in children)
                    say(f"{node.label} -> {len(children)} action(s)")
                else:
                    states.advance(nid, NodeState.RUNNING)
                    future = pool.submit(_stacked_runner(node, layers), ctx)
                    running[future] = nid

            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    nid = running.pop(future)
                    node = plan.nodes[nid]
                    result = future.result()
                    results.append(result)
                    say(result.summary())
                    if result.ok:
                        states.advance(nid, NodeState.DONE, result)
                        if result.stdout:
                            say(result.stdout.rstrip("\n"))
                    else:
                        states.advance(nid, NodeState.FAILED, result)
                        errors.append(result.summary())
                        for stream in (result.stdout, result.stderr):
                            if stream:
                                say(stream.rstrip("\n"))
                        cascade_skip(nid, f"upstream failed: {node.label}")
            elif not scheduled and pending:
                for nid in sorted(pending):
                    node = plan.nodes[nid]
                    states.advance(nid, NodeState.SKIPPED)
                    errors.append(f"{node.label}: unreachable (dependency cycle?)")
                    say(f"skip: {node.label} (unreachable — dependency cycle?)")
                pending.clear()

    success = not errors
    build_result = BuildResult(
        success=success, plan=plan, states=states, process_results=results, errors=errors
    )
    for layer in layers:
        layer.on_build_end(build_result, ctx)
    return build_result
