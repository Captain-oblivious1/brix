"""Build-layer seams: `NodeContext` and the `BuildLayer` hook interface.

Capabilities (incremental, cache, atomic) attach to execution as per-node
middleware plus build-scoped events — never as static plan transforms, which
would miss nodes created by runtime expansion. See docs/build-layers.html.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .process import ProcessResult

if TYPE_CHECKING:
    from .actions import Action
    from .builders import BuildResult
    from .plan import ExecutionNode, ExecutionPlan

NodeRunner = Callable[["NodeContext"], ProcessResult]


@dataclass
class NodeProposal:
    """Work a layer proposes; the scheduler inserts and wires it.

    This is the insertion channel from the layer side: layers never touch
    plan structure. Proposals returned from ``on_collection_realized`` are
    ordered before the collection's consumers.
    """

    action: "Action"
    label: str | None = None


@dataclass
class NodeContext:
    """Where one node's execution reads and writes.

    Declared paths on actions are logical names; the context maps them to
    physical places at render time. With no layers both roots are the
    workspace; the atomic layer points `output_root` at its staging tree.
    """

    workspace_root: Path
    output_root: Path

    @classmethod
    def for_root(cls, root: Path) -> "NodeContext":
        return cls(workspace_root=root, output_root=root)


class BuildLayer:
    """Hook interface for build capabilities. All hooks are optional.

    Layers never touch plan structure or node state directly; they obtain
    results (`around_node`), adjust the context, and propose work through
    the scheduler.
    """

    def on_build_begin(self, plan: "ExecutionPlan", ctx: NodeContext) -> None:
        """Build is starting (e.g. prepare staging, recover a crashed build)."""

    def around_node(
        self, node: "ExecutionNode", ctx: NodeContext, inner: NodeRunner
    ) -> ProcessResult:
        """Obtain the node's result: answer without calling `inner` (skip /
        cache hit), adjust `ctx` before passing the call inward, or both."""
        return inner(ctx)

    def on_collection_realized(
        self, node: "ExecutionNode", members: list[str], ctx: NodeContext
    ) -> "list[NodeProposal] | None":
        """An expansion point's membership just became known.

        May return node proposals (e.g. stale-output deletions); the
        scheduler inserts them and orders them before the collection's
        consumers.
        """
        return None

    def on_build_end(self, result: "BuildResult", ctx: NodeContext) -> None:
        """Build finished (e.g. commit staged outputs if everything succeeded)."""
