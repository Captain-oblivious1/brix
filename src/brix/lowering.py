"""Lower a logical Graph to the language-neutral wire spec.

The spec format is pinned by ``docs/boundary-schema.md`` — the one surface
the Rust core, this Python skin, and the future JS/TS skin all build
against. The golden-fixture generator (``rust/golden/generate.py``) imports
these functions so the fixtures and the live engine boundary can never
drift apart.

``supports_rust_engine`` reports whether a graph can cross the boundary
*today*: content-identity plugins live on the Python side until their port
lands, so a graph using them stays on the Python engine.
"""

from __future__ import annotations

from .actions import Action, DeleteFilesAction, ExpandAll, ExternalProcessAction
from .data import DataAtom, DataExpr, FileData, GlobData, RegexData, ToolData, ValueData
from .graph import Graph

SPEC_VERSION = 1


def atom_to_spec(atom: DataAtom) -> dict:
    if isinstance(atom, FileData):
        d = {"kind": "file", "path": atom.path}
        if atom.identity is not None:
            d["identity"] = atom.identity
        return d
    if isinstance(atom, GlobData):
        d = {"kind": "glob", "template": atom.pattern.template}
        if atom.identity is not None:
            d["identity"] = atom.identity
        return d
    if isinstance(atom, RegexData):
        return {"kind": "regex", "pattern": atom.regex.pattern}
    if isinstance(atom, ToolData):
        return {"kind": "tool", "name": atom.name}
    if isinstance(atom, ValueData):
        return {"kind": "value", "text": atom.text}
    raise TypeError(atom)


def expr_to_spec(expr: DataExpr) -> list[dict]:
    return [atom_to_spec(a) for a in expr.atoms()]


def action_to_spec(action: Action) -> dict:
    if isinstance(action, ExternalProcessAction):
        d = {
            "kind": "process",
            "command_line": action.command_line,
            "inputs": {k: expr_to_spec(v) for k, v in action.inputs.items()},
            "outputs": {k: expr_to_spec(v) for k, v in action.outputs.items()},
        }
        if action.name is not None:
            d["name"] = action.name
        if action.env:
            d["env"] = {k: expr_to_spec(v) for k, v in action.env.items()}
        if action.depfile is not None:
            d["depfile"] = action.depfile
        if action.worker is not None:
            d["worker"] = {
                "name": action.worker.name,
                "command_line": action.worker.command_line,
                "inputs": {k: expr_to_spec(v) for k, v in action.worker.inputs.items()},
            }
        return d
    if isinstance(action, ExpandAll):
        return {
            "kind": "expand",
            "element": action.element_name,
            "collection": expr_to_spec(action.collection),
            "child": action_to_spec(action.child),
        }
    if isinstance(action, DeleteFilesAction):
        return {"kind": "delete", "paths": list(action.paths)}
    raise TypeError(action)


def graph_to_spec(graph: Graph) -> dict:
    return {"version": SPEC_VERSION, "actions": [action_to_spec(a) for a in graph.actions]}


def _atoms_of(action: Action):
    if isinstance(action, ExternalProcessAction):
        exprs = [action.inputs, action.outputs, action.env]
        if action.worker is not None:
            exprs.append(action.worker.inputs)
        for group in exprs:
            for expr in group.values():
                yield from expr.atoms()
    elif isinstance(action, ExpandAll):
        yield from action.collection.atoms()
        yield from _atoms_of(action.child)


def supports_rust_engine(graph: Graph) -> bool:
    """Can this graph cross the boundary today? `identity=` content-identity
    plugins are still Python-side (they need the plugin registry); a graph
    using them builds on the Python engine until that port lands."""
    for action in graph.actions:
        stack = [action]
        while stack:
            current = stack.pop()
            if isinstance(current, ExpandAll):
                stack.append(current.child)
                if any(getattr(a, "identity", None) for a in current.collection.atoms()):
                    return False
                continue
            if isinstance(current, ExternalProcessAction):
                if any(getattr(a, "identity", None) for a in _atoms_of(current)):
                    return False
    return True
