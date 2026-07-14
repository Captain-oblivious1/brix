"""Planning: lower a logical graph into an execution plan.

Dependency inference: an edge is added from action A to action B when any
output data atom of A can name the same path as any input data atom of B.
Pattern-vs-pattern overlap is decided conservatively (see `patterns.overlaps`),
with one refinement: when A and B are expansions iterating structurally-equal
collections under the same element name, that binding holds the same value on
both sides and is compared rigidly. Without this, `combine {video}` producing
`out/{video}.raw.mpg` and `finalize {video}` producing `out/{video}.mpg` look
mutually dependent (`out/a/b.mpg` reads as finalize's video `a/b` *or* as
combine's scene `b` of video `a`) and the planner reports a false cycle.
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


def _atoms_overlap(
    out_atom: PathAtom, in_atom: PathAtom, shared: frozenset[str] = frozenset()
) -> bool:
    # Regex atoms are match-only; compare them against concrete paths only.
    if isinstance(out_atom, RegexData) and isinstance(in_atom, RegexData):
        return False
    if isinstance(out_atom, RegexData):
        pattern = in_atom.pattern
        return pattern.is_concrete and out_atom.match(pattern.template) is not None
    if isinstance(in_atom, RegexData):
        pattern = out_atom.pattern
        return pattern.is_concrete and in_atom.match(pattern.template) is not None
    return overlaps(out_atom.pattern, in_atom.pattern, shared)


def _expansion_bindings(action: Action) -> dict[str, tuple[str, ...]]:
    """Element name -> structural key of the collection it iterates, for the
    expansion spine of `action` (nested expansions contribute each level)."""
    binds: dict[str, tuple[str, ...]] = {}
    current = action
    while isinstance(current, ExpandAll):
        binds[current.element_name] = tuple(repr(a) for a in current.collection.atoms())
        current = current.child
    return binds


def shared_bindings(producer: Action, consumer: Action) -> frozenset[str]:
    """Binding names that hold the same value on both sides: element names of
    expansions iterating structurally-equal collections. Both actions expand
    the same member set under the same name, so in any realized pair of nodes
    the binding is one value — `patterns.overlaps` compares it rigidly."""
    pa = _expansion_bindings(producer)
    if not pa:
        return frozenset()
    pb = _expansion_bindings(consumer)
    return frozenset(name for name, key in pa.items() if pb.get(name) == key)


def infer_dependency(producer: Action, consumer: Action) -> str | None:
    """Return an edge reason if `consumer` depends on `producer`, else None."""
    shared = shared_bindings(producer, consumer)
    for out_atom in output_atoms(producer):
        for in_atom in input_atoms(consumer):
            if _atoms_overlap(out_atom, in_atom, shared):
                out_text = _atom_text(out_atom)
                in_text = _atom_text(in_atom)
                if out_text == in_text:
                    return f"data overlap: {out_text}"
                return f"data overlap: {out_text} ~ {in_text}"
    return None


def validate_output_ownership(actions: list[Action]) -> None:
    """Reject two actions whose declared output sets can name one path.

    Output ownership must be unique: otherwise parallel execution makes the
    final contents depend on which writer finishes last.  Use the same
    conservative overlap relation as dependency inference, including its
    same-collection binding refinement.
    """
    for left_index, left in enumerate(actions):
        for right in actions[left_index + 1 :]:
            shared = shared_bindings(left, right)
            for left_atom in output_atoms(left):
                for right_atom in output_atoms(right):
                    exact_regex = (
                        isinstance(left_atom, RegexData)
                        and isinstance(right_atom, RegexData)
                        and left_atom.regex.pattern == right_atom.regex.pattern
                    )
                    if exact_regex or _atoms_overlap(left_atom, right_atom, shared):
                        left_text = _atom_text(left_atom)
                        right_text = _atom_text(right_atom)
                        overlap = (
                            left_text
                            if left_text == right_text
                            else f"{left_text} ~ {right_text}"
                        )
                        raise ValueError(
                            "conflicting outputs: "
                            f"{_node_label(left)!r} and {_node_label(right)!r} "
                            f"may both write {overlap}; every output path must "
                            "have exactly one owner"
                        )


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
    validate_output_ownership(actions)
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
