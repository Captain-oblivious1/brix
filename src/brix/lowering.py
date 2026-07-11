"""Lower a logical Graph to the language-neutral wire spec.

The spec format is pinned by ``docs/boundary-schema.md`` — the one surface
the Rust core, this Python skin, and the future JS/TS skin all build
against. The golden-fixture generator (``rust/golden/generate.py``) imports
these functions so the fixtures and the live engine boundary can never
drift apart.

``custom_identity_names`` collects the identity names a graph uses that are
not built into the core (``jvm-abi``, ``ts-dts``): the engine boundary
resolves them against the skin's registry and passes their ``digest``
callables across as the ``content_identity`` extension trait — every graph
crosses; an unregistered name raises before the build starts.
"""

from __future__ import annotations

from .actions import Action, DeleteFilesAction, ExpandAll, ExternalProcessAction
from .data import DataAtom, DataExpr, FileData, GlobData, RegexData, ToolData, ValueData
from .graph import Graph

SPEC_VERSION = 1


def atom_to_spec(atom: DataAtom) -> dict:
    if isinstance(atom, FileData):
        d = {"kind": "file", "path": atom.path}
        if atom.content_key is not None:
            d["content_key"] = atom.content_key
        return d
    if isinstance(atom, GlobData):
        d = {"kind": "glob", "template": atom.pattern.template}
        if atom.content_key is not None:
            d["content_key"] = atom.content_key
        if atom.exclude:
            d["exclude"] = list(atom.exclude)
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


# The content identities ported into the Rust core (`rust/brix-core/src/
# identity.rs`). Adding a built-in means porting it there and listing it here.
_RUST_IDENTITIES = frozenset({"jvm-abi", "ts-dts"})


def custom_identity_names(graph: Graph) -> set[str]:
    """The content-key names this graph uses that the core does not build in.
    The engine boundary resolves each against the skin's registry (loud on a
    typo) and hands the core their digest callables — the `content_identity`
    extension trait of docs/boundary-schema.md."""
    names: set[str] = set()

    def collect(atoms):
        names.update(
            name
            for name in (getattr(a, "content_key", None) for a in atoms)
            if name is not None and name not in _RUST_IDENTITIES
        )

    for action in graph.actions:
        stack = [action]
        while stack:
            current = stack.pop()
            if isinstance(current, ExpandAll):
                stack.append(current.child)
                collect(current.collection.atoms())
                continue
            if isinstance(current, ExternalProcessAction):
                collect(_atoms_of(current))
    return names
