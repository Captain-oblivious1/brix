"""Planning: lower a logical graph into an execution plan.

Dependency inference: an edge is added from action A to action B when any
output data atom of A can name the same path as any input data atom of B.
Pattern-vs-pattern overlap is decided conservatively (see `patterns.overlaps`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .actions import Action, ExpandAll, ExternalProcessAction
from .data import FileData, GlobData, RegexData
from .graph import Graph
from .patterns import PathPattern, overlaps
from .plan import ExecutionPlan, NodeKind

PathAtom = FileData | GlobData | RegexData


def _path_atoms(exprs) -> Iterator[PathAtom]:
    for expr in exprs:
        for atom in expr.atoms():
            if isinstance(atom, (FileData, GlobData, RegexData)):
                yield atom


def input_atoms(action: Action) -> list[PathAtom]:
    if isinstance(action, ExternalProcessAction):
        atoms = list(_path_atoms(action.inputs.values()))
        if action.worker is not None:
            # The worker's own inputs (e.g. a bootstrap-built worker jar) are
            # inputs this action needs present, so they create dependency edges
            # like any other input — the worker jar's producer must run first.
            atoms += list(_path_atoms(action.worker.inputs.values()))
        return atoms
    if isinstance(action, ExpandAll):
        # Nested expansions contribute their collections and inputs too.
        return list(_path_atoms([action.collection])) + input_atoms(action.child)
    raise TypeError(f"unknown action type: {action!r}")


def output_atoms(action: Action) -> list[PathAtom]:
    if isinstance(action, ExternalProcessAction):
        return list(_path_atoms(action.outputs.values()))
    if isinstance(action, ExpandAll):
        return output_atoms(action.child)
    raise TypeError(f"unknown action type: {action!r}")


def _atoms_overlap(out_atom: PathAtom, in_atom: PathAtom) -> bool:
    # Regex atoms are match-only; compare them against concrete paths only.
    if isinstance(out_atom, RegexData) and isinstance(in_atom, RegexData):
        return False
    if isinstance(out_atom, RegexData):
        pattern = in_atom.pattern
        return pattern.is_concrete and out_atom.match(pattern.template) is not None
    if isinstance(in_atom, RegexData):
        pattern = out_atom.pattern
        return pattern.is_concrete and in_atom.match(pattern.template) is not None
    return overlaps(out_atom.pattern, in_atom.pattern)


def infer_dependency(producer: Action, consumer: Action) -> str | None:
    """Return an edge reason if `consumer` depends on `producer`, else None."""
    for out_atom in output_atoms(producer):
        for in_atom in input_atoms(consumer):
            if _atoms_overlap(out_atom, in_atom):
                out_text = _atom_text(out_atom)
                in_text = _atom_text(in_atom)
                if out_text == in_text:
                    return f"data overlap: {out_text}"
                return f"data overlap: {out_text} ~ {in_text}"
    return None


def _atom_text(atom: PathAtom) -> str:
    if isinstance(atom, RegexData):
        return atom.regex.pattern
    return atom.pattern.template


def _node_label(action: Action) -> str:
    label = getattr(action, "label", None)
    return label if label is not None else repr(action)


def plan_graph(graph: Graph, root: Path, targets: list[str] | None = None) -> ExecutionPlan:
    actions = graph.actions
    if targets:
        from .targets import select_actions

        actions = select_actions(actions, targets)
    plan = ExecutionPlan(root=root)
    for action in actions:
        kind = NodeKind.EXPAND if isinstance(action, ExpandAll) else NodeKind.PROCESS
        plan.add_node(kind, _node_label(action), action)
    for producer in plan.nodes:
        for consumer in plan.nodes:
            if producer.id == consumer.id:
                continue
            reason = infer_dependency(producer.action, consumer.action)
            if reason is not None:
                plan.add_edge(producer.id, consumer.id, reason)
    return plan
