"""The atomic layer: either the whole build lands, or nothing changes.

Staging layout, under the workspace:

    build/staging/add/<logical path>   outputs written by this build
    build/staging/del/<logical path>   deletion markers
    build/staging/COMMIT               commit began (recovery journal)

`on_build_begin` points the build context's ``output_root``/``deletions_root``
at the staging tree, so every write lands there and every read sees the
workspace as it will look after commit (the overlay in `NodeContext`).
Deletion actions become staged markers instead of immediate removals.

`on_build_end` commits only if the whole build succeeded: the COMMIT marker
is written first, then staged files move into place and marked deletions
are applied, then staging is removed. A crash mid-commit leaves the marker,
and the next build's `on_build_begin` finishes the commit (copying is
idempotent). A failed or interrupted build leaves staging in place,
inspectable by `brix.py status`, and it is discarded when the next build
starts.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .actions import DeleteFilesAction
from .layers import BuildLayer, NodeContext
from .process import ProcessResult

COMMIT_MARKER = "COMMIT"


class AtomicLayer(BuildLayer):
    def __init__(self, staging_dir: str | Path = "build/staging") -> None:
        self.staging_dir = staging_dir
        self.staging: Path | None = None  # resolved at build begin

    # -- lifecycle -----------------------------------------------------------

    def on_build_begin(self, plan, ctx: NodeContext) -> None:
        self.staging = ctx.workspace_root / self.staging_dir
        if (self.staging / COMMIT_MARKER).exists():
            # Crashed mid-commit last time; copying is idempotent — finish it.
            self._commit(ctx.workspace_root)
        elif self.staging.exists():
            # Leftovers of a failed/interrupted build; a new build supersedes them.
            shutil.rmtree(self.staging)
        (self.staging / "add").mkdir(parents=True, exist_ok=True)
        (self.staging / "del").mkdir(parents=True, exist_ok=True)
        # Build-scoped redirection: all writes stage, all reads overlay.
        ctx.output_root = self.staging / "add"
        ctx.deletions_root = self.staging / "del"

    def around_node(self, node, ctx: NodeContext, inner):
        action = node.action
        if isinstance(action, DeleteFilesAction):
            # Deleting is output too: stage markers, apply at commit.
            for rel in action.paths:
                marker = ctx.deletions_root / rel
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            return ProcessResult(
                action_name=action.label,
                command=action.label,
                exit_code=0,
                stdout="".join(f"staged deletion of {p}\n" for p in action.paths),
            )
        return inner(ctx)

    def on_build_end(self, result, ctx: NodeContext) -> None:
        if not result.success:
            return  # leave staging inspectable; workspace untouched
        (self.staging / COMMIT_MARKER).touch()
        self._commit(ctx.workspace_root)

    # -- commit ----------------------------------------------------------------

    def _commit(self, workspace: Path) -> None:
        add_dir = self.staging / "add"
        del_dir = self.staging / "del"
        if add_dir.exists():
            for staged in sorted(p for p in add_dir.rglob("*") if p.is_file()):
                target = workspace / staged.relative_to(add_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged, target)
        if del_dir.exists():
            for marker in sorted(p for p in del_dir.rglob("*") if p.is_file()):
                (workspace / marker.relative_to(del_dir)).unlink(missing_ok=True)
        shutil.rmtree(self.staging)
