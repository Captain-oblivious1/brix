"""Logical actions.

Actions consume named data expressions and produce named data expressions.
They are clean-build truth; build capabilities (incremental, cache, atomic)
are applied by planning layers, never declared here.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from .data import DataAtom, DataExpr, DataUnion

if TYPE_CHECKING:
    from .workers import WorkerSpec

# The input key under which `env_used=` is materialized. Reserved (leading
# underscore, brix-namespaced) so it cannot collide with an author's input key
# and is never referenced by a command template. See `_resolve_env_used`.
_ENV_USED_KEY = "_brix_env_used"


def _resolve_env_used(names) -> "DataExpr":
    """Snapshot the named environment variables into one fingerprint-only
    `ValueData` — the phase-2 *guard* of docs/environment-recording.md.

    Each declared name is read from `os.environ` (``None`` when unset, which is
    kept distinct from set-to-empty) and encoded as sorted canonical JSON. The
    result rides as an **unreferenced** input (the `classpath_track` pattern):
    it is hashed into the action's fingerprint by both engines — so changing a
    named variable re-stales the action — but it is never substituted into the
    command and never set in the process environment. Resolving here, on the
    Python side, keeps this off the Rust neutral path entirely: only the
    resolved *value* crosses the boundary, as ordinary `ValueData`."""
    from .data import ValueData

    snapshot = {name: os.environ.get(name) for name in sorted(set(names))}
    return ValueData(json.dumps(snapshot, sort_keys=True, ensure_ascii=False))


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
        env_used: tuple[str, ...] | list[str] | None = None,
        env_unset: tuple[str, ...] | list[str] | None = None,
        worker: "WorkerSpec | None" = None,
        depfile: str | None = None,
        self_resolved_inputs: tuple[str, ...] = (),
        _env_used_snapshot: DataExpr | None = None,
    ) -> None:
        from .data import ValueData

        self.name = name
        self.command_line = command_line
        self.inputs: dict[str, DataExpr] = dict(inputs or {})
        self.outputs: dict[str, DataExpr] = dict(outputs or {})
        # The reserved key may only be materialized by brix itself (below, or
        # carried through `substitute` via `_env_used_snapshot`). An author
        # supplying it directly would silently *replace* the live snapshot
        # with a constant — a guard bypass in the stale direction — so it is
        # rejected loudly instead.
        if _ENV_USED_KEY in self.inputs:
            raise ValueError(
                f"input key {_ENV_USED_KEY!r} is reserved: it is materialized "
                f"by env_used=, never supplied directly"
            )
        # An `env` value may be a literal (set + fingerprint, below) or the
        # `SYSTEM` sentinel — "take it from the ambient environment and track
        # it, don't pin it". `SYSTEM` desugars to `env_used`: the process still
        # inherits the ambient value, brix just fingerprints it. Being
        # fingerprint-only, `SYSTEM` is also safe on the warm-worker path, where
        # `env=` setting is ignored. See `brix.env` and docs/environment-recording.md.
        from .env import SYSTEM, UNSET

        _env_items = env or {}
        _system_tracked = tuple(k for k, v in _env_items.items() if v is SYSTEM)
        # `UNSET` forces a variable absent from the process environment. It is part
        # of the action's identity (below), so flipping a var between UNSET and any
        # other intent re-stales. Sorted for a deterministic, cross-engine order.
        self.env_unset: tuple[str, ...] = tuple(
            sorted(
                {k for k, v in _env_items.items() if v is UNSET} | set(env_unset or ())
            )
        )
        self.env: dict[str, DataExpr] = {
            k: v if isinstance(v, DataExpr) else ValueData(v)
            for k, v in _env_items.items()
            if v is not SYSTEM and v is not UNSET
        }
        # `env_used` names ambient environment variables this action's output
        # depends on but does not *set* (unlike `env=`). They are snapshotted
        # into a single fingerprint-only input so a change re-stales the action
        # — the guard, not a declaration of process env. Materialized once, at
        # first construction; `substitute` passes the frozen snapshot back via
        # `_env_used_snapshot`, so it is never re-read during expansion.
        # See `_resolve_env_used`. `SYSTEM` entries fold in here (order-preserving,
        # de-duplicated).
        self.env_used: tuple[str, ...] = tuple(
            dict.fromkeys(tuple(env_used or ()) + _system_tracked)
        )
        if self.env_used:
            self.inputs[_ENV_USED_KEY] = (
                _env_used_snapshot
                if _env_used_snapshot is not None
                else _resolve_env_used(self.env_used)
            )
        # A worker executes this action as a request instead of spawning it;
        # None spawns a process. Purely an execution transport (see workers.py).
        self.worker = worker
        # Logical path of a Makefile-format depfile this action emits (e.g. a
        # compiler's `-MMD -MF`). If set, the incremental layer scans it after a
        # run and folds the reported inputs (headers) into the fingerprint, so
        # editing one re-stales this action. Not part of identity/fingerprint
        # itself — just a marker telling the scanner where to look.
        self.depfile = depfile
        # Input keys whose paths this action's *tool* resolves itself (a foreign
        # resolver: a TS `import`, a generated `#include`) rather than brix
        # substituting them into a `{placeholder}`, AND which are intra-build
        # artifacts of another step. Such an input is invisible to the atomic
        # staging overlay — the tool reads the real workspace, not staging — so
        # a build carrying one is unsound under atomic (docs/capability-audit.md,
        # "the one true mismatch"). Build-time validation only (the atomic
        # builder refuses it); not part of the wire spec or the fingerprint.
        self.self_resolved_inputs = tuple(self_resolved_inputs)

    @property
    def label(self) -> str:
        return self.name if self.name is not None else self.command_line

    def substitute(self, bindings: dict[str, str]) -> ExternalProcessAction:
        """Return a concrete copy with bindings applied to name and data paths."""
        name = self.name
        if name is not None:
            for key, value in bindings.items():
                name = name.replace("{" + key + "}", value)
        depfile = self.depfile
        if depfile is not None:
            for key, value in bindings.items():
                depfile = depfile.replace("{" + key + "}", value)
        # The materialized _brix_env_used snapshot is immutable — it is handed
        # to the copy verbatim via `_env_used_snapshot`, never through
        # placeholder substitution, so an env value that happens to contain a
        # `{binding}`-shaped substring cannot be rewritten during expansion,
        # and the snapshot is never re-read (taken once, at construction).
        inputs = {
            k: _substitute_expr(v, bindings)
            for k, v in self.inputs.items()
            if k != _ENV_USED_KEY
        }
        return ExternalProcessAction(
            name=name,
            command_line=self.command_line,
            inputs=inputs,
            outputs={k: _substitute_expr(v, bindings) for k, v in self.outputs.items()},
            env={k: _substitute_expr(v, bindings) for k, v in self.env.items()},
            env_used=self.env_used,
            env_unset=self.env_unset,
            worker=self.worker,
            depfile=depfile,
            self_resolved_inputs=self.self_resolved_inputs,
            _env_used_snapshot=self.inputs.get(_ENV_USED_KEY),
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
