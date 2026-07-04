"""Logical actions.

Actions consume named data expressions and produce named data expressions.
They are clean-build truth; build capabilities (incremental, cache, atomic)
are applied by planning layers, never declared here.
"""

from __future__ import annotations

from .data import DataAtom, DataExpr, DataUnion


class Action:
    """Base class for all logical actions."""


class ExternalProcessAction(Action):
    """A generic external process.

    ``command_line`` is a template whose ``{placeholder}`` names refer to
    entries in ``inputs`` and ``outputs``. Binding placeholders such as
    ``{name}`` inside data-expression paths belong to a separate namespace
    and are substituted at expansion time, not at command rendering time.

    ``name`` is diagnostic identity: node labels, echo lines, and process
    results. It is optional; unnamed actions are labeled by their command
    template. Named actions will fingerprint/debug better once incremental
    metadata and caching exist.
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        command_line: str,
        inputs: dict[str, DataExpr] | None = None,
        outputs: dict[str, DataExpr] | None = None,
    ) -> None:
        self.name = name
        self.command_line = command_line
        self.inputs: dict[str, DataExpr] = dict(inputs or {})
        self.outputs: dict[str, DataExpr] = dict(outputs or {})

    @property
    def label(self) -> str:
        return self.name if self.name is not None else self.command_line

    def substitute(self, bindings: dict[str, str]) -> ExternalProcessAction:
        """Return a concrete copy with bindings applied to name and data paths."""

        def sub_expr(expr: DataExpr) -> DataExpr:
            if isinstance(expr, DataUnion):
                return DataUnion(*(sub_expr(p) for p in expr.parts))
            assert isinstance(expr, DataAtom)
            return expr.substitute(bindings)

        name = self.name
        if name is not None:
            for key, value in bindings.items():
                name = name.replace("{" + key + "}", value)
        return ExternalProcessAction(
            name=name,
            command_line=self.command_line,
            inputs={k: sub_expr(v) for k, v in self.inputs.items()},
            outputs={k: sub_expr(v) for k, v in self.outputs.items()},
        )

    def __repr__(self) -> str:
        return f"ExternalProcessAction({self.label!r})"


class DeleteFilesAction(Action):
    """Deletion of stale outputs, executed natively by the scheduler.

    Never user-authored: these nodes are proposed by capability layers
    (e.g. incremental deleted-source cleanup) and inserted through the
    scheduler's insertion channel. Paths are logical, workspace-relative,
    and resolve through ``ctx.output_root`` like any declared output.
    """

    def __init__(self, paths: list[str]) -> None:
        self.paths = list(paths)

    @property
    def label(self) -> str:
        return "rm " + " ".join(self.paths)

    def __repr__(self) -> str:
        return f"DeleteFilesAction({self.paths!r})"


class ExpandAll(Action):
    """Repeated work over a data collection.

    Not an opaque loop: this is an unrealized expansion point in the
    execution plan. Once collection membership is known, it expands into
    concrete sibling action nodes, one per binding of ``element_name``.
    """

    def __init__(
        self,
        element_name: str,
        collection: DataExpr,
        child: ExternalProcessAction,
    ) -> None:
        if not isinstance(child, ExternalProcessAction):
            raise TypeError("ExpandAll child must be an ExternalProcessAction")
        self.element_name = element_name
        self.collection = collection
        self.child = child

    def __repr__(self) -> str:
        return f"ExpandAll({self.element_name!r} over {self.collection!r})"
