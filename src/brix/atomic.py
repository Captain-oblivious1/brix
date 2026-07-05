"""The atomic layer: either the whole build lands, or nothing changes.

Staging layout, under the workspace:

    build/staging/add/<logical path>   outputs written by this build
    build/staging/del/<logical path>   deletion markers
    build/staging/COMMIT               commit began (recovery journal)
    build/staging/HELD                 successful build held for review

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

**Held staging** (review before commit): with ``hold=True`` a successful
build writes the HELD marker instead of committing — the workspace stays
untouched until an explicit `commit_staging` (or is thrown away by
`discard_staging`). Held staging is *kept* at the next build begin and
built on top of: the overlay already reads it as if committed, so further
builds — held or not — compose; a plain successful build commits
everything, held work included. The HELD marker is *consumed* when a new
build starts on top: the new build's own outcome decides what staging
becomes (re-held, committed, or failed-build debris). That keeps the
atomicity promise — a failed build on top of held staging leaves a mixed
tree that must never commit, so it is discarded at the next build like
any debris (the cache layer makes the held work cheap to reproduce).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .actions import DeleteFilesAction
from .layers import BuildLayer, NodeContext
from .process import ProcessResult

COMMIT_MARKER = "COMMIT"
HELD_MARKER = "HELD"


def _staged_counts(staging: Path) -> tuple[int, int]:
    files = sum(1 for p in (staging / "add").rglob("*") if p.is_file()) if (staging / "add").exists() else 0
    deletions = sum(1 for p in (staging / "del").rglob("*") if p.is_file()) if (staging / "del").exists() else 0
    return files, deletions


def _apply_commit(workspace: Path, staging: Path) -> None:
    """The journaled commit: copy add/, apply del/, remove staging.
    Idempotent — safe to rerun after a crash mid-copy."""
    add_dir = staging / "add"
    del_dir = staging / "del"
    if add_dir.exists():
        for staged in sorted(p for p in add_dir.rglob("*") if p.is_file()):
            target = workspace / staged.relative_to(add_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, target)
    if del_dir.exists():
        for marker in sorted(p for p in del_dir.rglob("*") if p.is_file()):
            (workspace / marker.relative_to(del_dir)).unlink(missing_ok=True)
    shutil.rmtree(staging)


def commit_staging(workspace: Path, staging: Path) -> tuple[int, int] | None:
    """Commit held (or interrupted) staging. None if nothing is staged;
    (files, deletions) applied otherwise. Refuses failed-build debris —
    only a *successful* build's staging may reach the workspace."""
    if not staging.exists():
        return None
    held = (staging / HELD_MARKER).exists()
    interrupted = (staging / COMMIT_MARKER).exists()
    if not held and not interrupted:
        raise ValueError(
            "staged outputs are from a failed build; `build` to redo them or `discard`"
        )
    counts = _staged_counts(staging)
    (staging / COMMIT_MARKER).touch()
    _apply_commit(workspace, staging)
    return counts


def discard_staging(staging: Path) -> int | None:
    """Throw staging away (held or failed-build debris). None if nothing
    is staged; the number of staged files discarded otherwise. Refuses an
    interrupted commit — that build already committed and must finish."""
    if not staging.exists():
        return None
    if (staging / COMMIT_MARKER).exists():
        raise ValueError(
            "an interrupted commit is pending; it finishes when the next build starts"
        )
    files, _ = _staged_counts(staging)
    shutil.rmtree(staging)
    return files


class AtomicLayer(BuildLayer):
    def __init__(self, staging_dir: str | Path = "build/staging", hold: bool = False) -> None:
        self.staging_dir = staging_dir
        self.hold = hold
        self.staging: Path | None = None  # resolved at build begin

    # -- lifecycle -----------------------------------------------------------

    def on_build_begin(self, plan, ctx: NodeContext) -> None:
        self.staging = ctx.workspace_root / self.staging_dir
        if (self.staging / COMMIT_MARKER).exists():
            # Crashed mid-commit last time; copying is idempotent — finish it.
            _apply_commit(ctx.workspace_root, self.staging)
        elif self.staging.exists() and not (self.staging / HELD_MARKER).exists():
            # Leftovers of a failed/interrupted build; a new build supersedes them.
            shutil.rmtree(self.staging)
        # Held staging survives and is built on top of: the overlay reads
        # it as if committed, so this build composes with the held one.
        # Consume the marker — from here the tree mixes held and new
        # outputs, and only this build's success may re-mark or commit it.
        (self.staging / HELD_MARKER).unlink(missing_ok=True)
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
        if self.hold:
            (self.staging / HELD_MARKER).touch()
            return  # held for review; commit_staging applies it later
        (self.staging / COMMIT_MARKER).touch()
        _apply_commit(ctx.workspace_root, self.staging)
