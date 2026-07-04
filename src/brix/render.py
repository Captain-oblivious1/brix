"""Command-line placeholder rendering for realized actions.

Rendering is where logical names become physical places: inputs are read and
collections realized against `ctx.workspace_root`, while declared outputs are
mapped through `ctx.output_root` (the two are the same unless a layer, such
as atomic staging, redirects outputs). Actions themselves are never rewritten.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .actions import ExternalProcessAction
from .data import DataExpr, FileData, GlobData, RegexData, ToolData, ValueData
from .layers import NodeContext

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class RenderError(Exception):
    pass


def _context(where: NodeContext | Path | str) -> NodeContext:
    if isinstance(where, NodeContext):
        return where
    return NodeContext.for_root(Path(where))


def resolve_tool(tool: ToolData) -> str:
    if "/" in tool.name:
        return tool.name
    resolved = shutil.which(tool.name)
    if resolved is None:
        raise RenderError(f"tool not found: {tool.name}")
    return resolved


def _output_path(ctx: NodeContext, logical: str) -> str:
    """Map a logical output path to the physical path used in the command.

    Commands run with cwd=workspace_root, so the physical path is expressed
    relative to the workspace. With output_root == workspace_root this is
    the logical path unchanged.
    """
    if ctx.output_root == ctx.workspace_root:
        return logical
    return os.path.relpath(ctx.output_root / logical, ctx.workspace_root)


def _render_expr(name: str, expr: DataExpr, ctx: NodeContext, is_output: bool) -> str:
    from .data import DataUnion

    if isinstance(expr, DataUnion):
        parts = [_render_expr(name, p, ctx, is_output) for p in expr.parts]
        return " ".join(p for p in parts if p)
    if isinstance(expr, ValueData):
        return expr.text
    if isinstance(expr, ToolData):
        return resolve_tool(expr)
    if isinstance(expr, FileData):
        if not expr.pattern.is_concrete:
            raise RenderError(f"{name}: unresolved bindings in path {expr.path!r}")
        if is_output:
            return _output_path(ctx, expr.path)
        return _input_path(ctx, expr.path)
    if isinstance(expr, GlobData):
        if is_output:
            raise RenderError(
                f"{name}: output collection {expr.pattern.template!r} cannot be "
                "rendered into a command line; pass a directory value instead"
            )
        return " ".join(
            _relative(ctx, physical) for _, physical, _ in ctx.find(expr.pattern)
        )
    if isinstance(expr, RegexData):
        if is_output:
            raise RenderError(f"{name}: output regex collection cannot be rendered")
        return " ".join(
            _relative(ctx, physical)
            for logical, physical in ctx.iter_files()
            if expr.match(logical) is not None
        )
    raise RenderError(f"{name}: cannot render {expr!r}")


def _relative(ctx: NodeContext, physical: Path) -> str:
    """Express a physical path relative to the command's cwd (workspace)."""
    return os.path.relpath(physical, ctx.workspace_root)


def _input_path(ctx: NodeContext, logical: str) -> str:
    physical = ctx.read_path(logical)
    if physical == ctx.workspace_root / logical:
        return logical
    return _relative(ctx, physical)


def render_command(action: ExternalProcessAction, where: NodeContext | Path | str) -> str:
    """Substitute input/output placeholders in the action's command line.

    Only placeholders the command line references are rendered: declared
    outputs such as unrealized collections need not appear in the command.
    """
    ctx = _context(where)
    for name in action.inputs:
        if name in action.outputs:
            raise RenderError(f"{name!r} is both an input and an output of {action.label!r}")

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in action.inputs:
            return _render_expr(key, action.inputs[key], ctx, is_output=False)
        if key in action.outputs:
            return _render_expr(key, action.outputs[key], ctx, is_output=True)
        raise RenderError(f"{action.label!r}: unknown placeholder {{{key}}}")

    return _PLACEHOLDER_RE.sub(repl, action.command_line)


def prepare_output_dirs(action: ExternalProcessAction, where: NodeContext | Path | str) -> None:
    """Create parent directories for declared outputs under the output root."""
    ctx = _context(where)
    out_root = ctx.output_root
    for expr in action.outputs.values():
        for atom in expr.atoms():
            if isinstance(atom, FileData) and atom.pattern.is_concrete:
                (out_root / atom.path).parent.mkdir(parents=True, exist_ok=True)
            elif isinstance(atom, GlobData):
                prefix = atom.pattern.literal_prefix_dir
                if prefix:
                    (out_root / prefix).mkdir(parents=True, exist_ok=True)
