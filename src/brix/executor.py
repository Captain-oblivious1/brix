"""The scheduler: runs an execution plan.

The scheduler owns readiness. Nodes execute and report results; expansion
logic responds to realized collections by adding concrete nodes to the plan.
Nodes never mutate graph structure themselves.
"""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from .actions import ExpandAll, ExternalProcessAction
from .data import GlobData, RegexData
from .plan import ExecutionNode, ExecutionPlan, NodeKind, NodeState
from .process import ProcessResult, run_process
from .render import RenderError, prepare_output_dirs, render_command


def _run_process_node(node: ExecutionNode, root: Path) -> ProcessResult:
    action = node.action
    assert isinstance(action, ExternalProcessAction)
    try:
        prepare_output_dirs(action, root)
        command = render_command(action, root)
    except RenderError as exc:
        return ProcessResult(action_name=action.label, command="", stderr=f"brix: {exc}")
    return run_process(action.label, command, cwd=root)


def _collection_members(action: ExpandAll, root: Path) -> list[str]:
    """Realize the collection and return sorted unique element bindings."""
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


def _expand(plan: ExecutionPlan, node: ExecutionNode) -> list[ExecutionNode]:
    """Expand an ExpandAll node into concrete sibling process nodes."""
    action = node.action
    assert isinstance(action, ExpandAll)
    pred_ids = [p.id for p in plan.predecessors(node.id)]
    succ_ids = [s.id for s in plan.successors(node.id)]
    children: list[ExecutionNode] = []
    for value in _collection_members(action, plan.root):
        concrete = action.child.substitute({action.element_name: value})
        child_node = plan.add_node(NodeKind.PROCESS, concrete.label, concrete)
        for pid in pred_ids:
            plan.add_edge(pid, child_node.id, f"expansion of [{node.id}]")
        for sid in succ_ids:
            plan.add_edge(child_node.id, sid, f"expanded member {value!r}")
        children.append(child_node)
    return children


def execute(plan: ExecutionPlan, jobs: int | None = None, echo: bool = True):
    from .builders import BuildResult

    max_workers = jobs or min(32, os.cpu_count() or 4)
    pending: set[int] = {n.id for n in plan.nodes if n.state is NodeState.PENDING}
    running: dict[Future[ProcessResult], int] = {}
    results: list[ProcessResult] = []
    errors: list[str] = []

    def say(message: str) -> None:
        if echo:
            print(message)

    def ready(node_id: int) -> bool:
        return all(p.state is NodeState.DONE for p in plan.predecessors(node_id))

    def cascade_skip(from_id: int, why: str) -> None:
        stack = [s.id for s in plan.successors(from_id)]
        while stack:
            nid = stack.pop()
            node = plan.nodes[nid]
            if node.state is not NodeState.PENDING:
                continue
            node.state = NodeState.SKIPPED
            pending.discard(nid)
            say(f"skip: {node.label} ({why})")
            stack.extend(s.id for s in plan.successors(nid))

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
                        children = _expand(plan, node)
                    except RenderError as exc:
                        node.state = NodeState.FAILED
                        errors.append(f"{node.label}: {exc}")
                        say(f"FAILED: {node.label}: {exc}")
                        cascade_skip(nid, f"upstream failed: {node.label}")
                        continue
                    node.state = NodeState.DONE
                    pending.update(c.id for c in children)
                    say(f"{node.label} -> {len(children)} action(s)")
                else:
                    node.state = NodeState.RUNNING
                    future = pool.submit(_run_process_node, node, plan.root)
                    running[future] = nid

            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    nid = running.pop(future)
                    node = plan.nodes[nid]
                    result = future.result()
                    node.result = result
                    results.append(result)
                    say(result.summary())
                    if result.ok:
                        node.state = NodeState.DONE
                        if result.stdout:
                            say(result.stdout.rstrip("\n"))
                    else:
                        node.state = NodeState.FAILED
                        errors.append(result.summary())
                        for stream in (result.stdout, result.stderr):
                            if stream:
                                say(stream.rstrip("\n"))
                        cascade_skip(nid, f"upstream failed: {node.label}")
            elif not scheduled and pending:
                for nid in sorted(pending):
                    node = plan.nodes[nid]
                    node.state = NodeState.SKIPPED
                    errors.append(f"{node.label}: unreachable (dependency cycle?)")
                    say(f"skip: {node.label} (unreachable — dependency cycle?)")
                pending.clear()

    success = not errors
    return BuildResult(success=success, plan=plan, process_results=results, errors=errors)
