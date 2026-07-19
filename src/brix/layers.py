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
    physical places. With no layers both roots are the workspace. The
    atomic layer points ``output_root`` at its staging tree and
    ``deletions_root`` at its deletion markers — reads then see the
    workspace *as it will look after commit*: staged outputs shadow
    workspace files and marked deletions hide them (the overlay).
    """

    workspace_root: Path
    output_root: Path
    deletions_root: Path | None = None
    # Scrub mode: run each process with an *authoritative* environment (only the
    # plumbing base plus the action's declared allowlist), not the inherited one.
    # Set once per build from the incremental layer; see `process.scrub_env`.
    scrub: bool = False

    @classmethod
    def for_root(cls, root: Path) -> "NodeContext":
        return cls(workspace_root=root, output_root=root)

    # -- overlay reads -------------------------------------------------------

    def read_roots(self) -> tuple[Path, ...]:
        if self.output_root == self.workspace_root:
            return (self.workspace_root,)
        return (self.output_root, self.workspace_root)

    def _hidden(self, logical: str) -> bool:
        """Deleted this build (marker present) and not re-created since."""
        return (
            self.deletions_root is not None
            and (self.deletions_root / logical).exists()
            and not (self.output_root / logical).exists()
        )

    def read_path(self, logical: str) -> Path:
        """The physical location a logical path currently reads from."""
        if self._hidden(logical):
            return self.output_root / logical  # resolves to a missing file
        for root in self.read_roots():
            candidate = root / logical
            if candidate.exists():
                return candidate
        return self.workspace_root / logical

    def find(self, pattern) -> list[tuple[str, Path, dict[str, str]]]:
        """Realize a PathPattern across the overlay.

        Returns (logical path, physical path, bindings), staged copies
        shadowing workspace copies, marked deletions excluded.
        """
        merged: dict[str, tuple[Path, dict[str, str]]] = {}
        for root in self.read_roots():
            for logical, bindings in pattern.find(root):
                if logical not in merged and not self._hidden(logical):
                    merged[logical] = (root / logical, bindings)
        return [(k, *merged[k]) for k in sorted(merged)]

    def iter_files(self) -> list[tuple[str, Path]]:
        """Every (logical, physical) file visible through the overlay,
        excluding brix's own storage (staging tree, metadata dirs)."""
        exclude: list[Path] = []
        if self.output_root != self.workspace_root:
            exclude.append(self.output_root.parent)  # the staging tree
        merged: dict[str, Path] = {}
        for root in self.read_roots():
            for path in root.rglob("*"):
                # ".brix"-prefixed directories are brix's own storage
                # (metadata, cache) — never part of the workspace.
                if not path.is_file() or any(p.startswith(".brix") for p in path.parts):
                    continue
                if any(path.is_relative_to(x) for x in exclude):
                    continue
                logical = path.relative_to(root).as_posix()
                if logical not in merged and not self._hidden(logical):
                    merged[logical] = path
        return sorted(merged.items())


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
