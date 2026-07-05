"""The cache layer: reuse results across workspaces, keyed by fingerprint.

Incremental answers "did *this workspace* already do it?"; cache answers
"did *anyone* already do it?". The key is the same `action_fingerprint`
the incremental layer records — everything that determines an action's
outputs, hashed — so a hit is exactly as trustworthy as a replay.

The layer sits innermost: a node the incremental layer proved fresh never
reaches the cache, and a cache hit writes its restored outputs through
``ctx.output_root``, so with the atomic layer outermost they route through
staging like any process output. Restored results carry
``source="cache"``. Failures with a real exit code are cached and
replayed too (same inputs, same failure); signal-killed or never-started
runs are not — those aren't the action's deterministic answer.

Store layout (safe for concurrent builds sharing one directory):

    <cache_dir>/<fp[:2]>/<fp>/entry.json   result + logical output paths
    <cache_dir>/<fp[:2]>/<fp>/files/<n>    output content blobs

Entries are assembled in a temp directory and renamed into place; a lost
race just discards the duplicate.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

from .actions import ExternalProcessAction
from .data import FileData, GlobData
from .layers import BuildLayer, NodeContext
from .metadata import TimeAndHashChangeDetector
from .process import ProcessResult

_ENTRY = "entry.json"
_FILES = "files"


def _output_files(action: ExternalProcessAction, ctx: NodeContext) -> dict[str, Path]:
    """The action's produced output files as {logical path: physical path}.

    Directory outputs (out-root declarations) hold no content of their own
    and are skipped; the files inside them are declared separately.
    """
    files: dict[str, Path] = {}
    for expr in action.outputs.values():
        for atom in expr.atoms():
            if isinstance(atom, FileData) and atom.pattern.is_concrete:
                physical = ctx.read_path(atom.path)
                if physical.is_file():
                    files[atom.path] = physical
            elif isinstance(atom, (FileData, GlobData)):
                for logical, physical, _ in ctx.find(atom.pattern):
                    if physical.is_file():
                        files[logical] = physical
    return files


class CacheStore:
    """Content-addressed result store under one directory."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _entry_dir(self, fingerprint: str) -> Path:
        return self.directory / fingerprint[:2] / fingerprint

    def load(self, fingerprint: str) -> dict | None:
        try:
            return json.loads((self._entry_dir(fingerprint) / _ENTRY).read_text())
        except (OSError, ValueError):
            return None

    def restore(self, fingerprint: str, entry: dict, ctx: NodeContext) -> bool:
        """Copy the entry's outputs to ``ctx.output_root``. False (and no
        further copies) if the entry is corrupt — the caller falls back to
        running the process, which overwrites anything partial."""
        files_dir = self._entry_dir(fingerprint) / _FILES
        copies: list[tuple[Path, Path]] = []
        for logical, blob in entry.get("outputs", {}).items():
            source = files_dir / blob
            if not source.is_file():
                return False
            copies.append((source, ctx.output_root / logical))
        for source, dest in copies:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)  # copy2: linked binaries keep their mode
        return True

    def store(self, fingerprint: str, result: ProcessResult, outputs: dict[str, Path]) -> None:
        final = self._entry_dir(fingerprint)
        if final.exists():
            return
        tmp = self.directory / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        (tmp / _FILES).mkdir(parents=True)
        names: dict[str, str] = {}
        for index, (logical, physical) in enumerate(sorted(outputs.items())):
            names[logical] = str(index)
            shutil.copy2(physical, tmp / _FILES / str(index))
        (tmp / _ENTRY).write_text(
            json.dumps({"result": asdict(result), "outputs": names}, sort_keys=True)
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.rename(final)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)  # another build won the race


class CacheLayer(BuildLayer):
    """Answer stale nodes from the store; populate it after real runs."""

    def __init__(self, store: CacheStore, detector: TimeAndHashChangeDetector) -> None:
        self.store = store
        self.detector = detector

    def around_node(self, node, ctx, inner):
        from .incremental import action_fingerprint

        action = node.action
        if not isinstance(action, ExternalProcessAction):
            return inner(ctx)
        fingerprint = action_fingerprint(action, ctx, self.detector)
        entry = self.store.load(fingerprint)
        if entry is not None and self.store.restore(fingerprint, entry, ctx):
            restored = dict(entry["result"])
            restored["source"] = "cache"
            return ProcessResult(**restored)
        result = inner(ctx)
        cacheable = (
            result.source == "process"
            and result.signal is None
            and result.exit_code is not None
        )
        if cacheable:
            outputs = _output_files(action, ctx) if result.ok else {}
            self.store.store(fingerprint, result, outputs)
        return result
