"""Command-line placeholder rendering for realized actions."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .actions import ExternalProcessAction
from .data import DataExpr, FileData, GlobData, RegexData, ToolData, ValueData

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class RenderError(Exception):
    pass


def resolve_tool(tool: ToolData) -> str:
    if "/" in tool.name:
        return tool.name
    resolved = shutil.which(tool.name)
    if resolved is None:
        raise RenderError(f"tool not found: {tool.name}")
    return resolved


def _render_expr(name: str, expr: DataExpr, root: Path, is_output: bool) -> str:
    from .data import DataUnion

    if isinstance(expr, DataUnion):
        parts = [_render_expr(name, p, root, is_output) for p in expr.parts]
        return " ".join(p for p in parts if p)
    if isinstance(expr, ValueData):
        return expr.text
    if isinstance(expr, ToolData):
        return resolve_tool(expr)
    if isinstance(expr, FileData):
        if not expr.pattern.is_concrete:
            raise RenderError(f"{name}: unresolved bindings in path {expr.path!r}")
        return expr.path
    if isinstance(expr, GlobData):
        if is_output:
            raise RenderError(
                f"{name}: output collection {expr.pattern.template!r} cannot be "
                "rendered into a command line; pass a directory value instead"
            )
        return " ".join(path for path, _ in expr.pattern.find(root))
    if isinstance(expr, RegexData):
        if is_output:
            raise RenderError(f"{name}: output regex collection cannot be rendered")
        matches = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and expr.match(p.relative_to(root).as_posix()) is not None
        )
        return " ".join(matches)
    raise RenderError(f"{name}: cannot render {expr!r}")


def render_command(action: ExternalProcessAction, root: Path) -> str:
    """Substitute input/output placeholders in the action's command line.

    Only placeholders the command line references are rendered: declared
    outputs such as unrealized collections need not appear in the command.
    """
    for name in action.inputs:
        if name in action.outputs:
            raise RenderError(f"{name!r} is both an input and an output of {action.label!r}")

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in action.inputs:
            return _render_expr(key, action.inputs[key], root, is_output=False)
        if key in action.outputs:
            return _render_expr(key, action.outputs[key], root, is_output=True)
        raise RenderError(f"{action.label!r}: unknown placeholder {{{key}}}")

    return _PLACEHOLDER_RE.sub(repl, action.command_line)


def prepare_output_dirs(action: ExternalProcessAction, root: Path) -> None:
    """Create parent directories for declared outputs."""
    for expr in action.outputs.values():
        for atom in expr.atoms():
            if isinstance(atom, FileData) and atom.pattern.is_concrete:
                (root / atom.path).parent.mkdir(parents=True, exist_ok=True)
            elif isinstance(atom, GlobData):
                prefix = atom.pattern.literal_prefix_dir
                if prefix:
                    (root / prefix).mkdir(parents=True, exist_ok=True)
