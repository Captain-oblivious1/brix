"""Logical actions.

Actions consume named data expressions and produce named data expressions.
They are clean-build truth; build capabilities (incremental, cache, atomic)
are applied by planning layers, never declared here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .data import DataAtom, DataExpr, DataUnion

if TYPE_CHECKING:
    from .workers import WorkerSpec


class Action:
    """Base class for all logical actions."""


class Expandable(Action):
    """Anything an ExpandAll can instantiate per collection member.

    Requires a diagnostic ``label`` and binding ``substitute``. Both
    `ExternalProcessAction` and `ExpandAll` itself qualify — so expansion
    points nest: an outer expansion over videos can instantiate an inner
    expansion over each video's scenes.
    """

    @property
    def label(self) -> str:
        raise NotImplementedError

    def substitute(self, bindings: dict[str, str]) -> Expandable:
        raise NotImplementedError


def _substitute_expr(expr: DataExpr, bindings: dict[str, str]) -> DataExpr:
    if isinstance(expr, DataUnion):
        return DataUnion(*(_substitute_expr(p, bindings) for p in expr.parts))
    assert isinstance(expr, DataAtom)
    return expr.substitute(bindings)


class ExternalProcessAction(Expandable):
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
        env: dict[str, DataExpr | str] | None = None,
        worker: "WorkerSpec | None" = None,
    ) -> None:
        from .data import ValueData

        self.name = name
        self.command_line = command_line
        self.inputs: dict[str, DataExpr] = dict(inputs or {})
        self.outputs: dict[str, DataExpr] = dict(outputs or {})
        self.env: dict[str, DataExpr] = {
            k: v if isinstance(v, DataExpr) else ValueData(v)
            for k, v in (env or {}).items()
        }
        # A worker executes this action as a request instead of spawning it;
        # None spawns a process. Purely an execution transport (see workers.py).
        self.worker = worker

    @property
    def label(self) -> str:
        return self.name if self.name is not None else self.command_line

    def substitute(self, bindings: dict[str, str]) -> ExternalProcessAction:
        """Return a concrete copy with bindings applied to name and data paths."""
        name = self.name
        if name is not None:
            for key, value in bindings.items():
                name = name.replace("{" + key + "}", value)
        return ExternalProcessAction(
            name=name,
            command_line=self.command_line,
            inputs={k: _substitute_expr(v, bindings) for k, v in self.inputs.items()},
            outputs={k: _substitute_expr(v, bindings) for k, v in self.outputs.items()},
            env={k: _substitute_expr(v, bindings) for k, v in self.env.items()},
            worker=self.worker,
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


class ExpandAll(Expandable):
    """Repeated work over a data collection.

    Not an opaque loop: this is an unrealized expansion point in the
    execution plan. Once collection membership is known, it expands into
    concrete sibling nodes, one per binding of ``element_name``.

    The child is any `Expandable` — a process action, or another
    `ExpandAll` for nested repetition (per video, per scene of that
    video); use distinct element names per level.
    """

    def __init__(
        self,
        element_name: str,
        collection: DataExpr,
        child: Expandable,
    ) -> None:
        if not isinstance(child, Expandable):
            raise TypeError(
                f"ExpandAll child must be Expandable "
                f"(ExternalProcessAction or ExpandAll), got {child!r}"
            )
        self.element_name = element_name
        self.collection = collection
        self.child = child

    @property
    def label(self) -> str:
        return f"expand: {self.child.label}"

    def substitute(self, bindings: dict[str, str]) -> Expandable:
        """Apply outer bindings. If they bind this expansion's *own*
        element (target selection does), the expansion collapses to its
        substituted child — there is nothing left to expand over."""
        if self.element_name in bindings:
            return self.child.substitute(bindings)
        return ExpandAll(
            element_name=self.element_name,
            collection=_substitute_expr(self.collection, bindings),
            child=self.child.substitute(bindings),
        )

    def __repr__(self) -> str:
        return f"ExpandAll({self.element_name!r} over {self.collection!r})"
