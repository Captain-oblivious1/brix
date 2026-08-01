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

The store is pluggable. `CacheStore` is the built-in local one; any object
with the three-method protocol (`load` / `restore` / `store`, see
`CacheStoreProtocol`) can back the layer instead — a remote team cache, or
a `TwoTierCacheStore` putting a fast local store in front of a slow shared
one. Robustness against an unreachable store is the *layer's* job, not each
store's: `CacheLayer` treats any store exception as a cache miss (or a
skipped populate), so a down remote degrades to running the work, never a
failed build. A leaf store may therefore let its I/O errors propagate.

Store layout of the built-in `CacheStore` (safe for concurrent builds
sharing one directory):

    <cache_dir>/<fp[:2]>/<fp>/entry.json   result + logical output paths
    <cache_dir>/<fp[:2]>/<fp>/files/<n>    output content blobs

Entries are assembled in a temp directory and renamed into place; a lost
race just discards the duplicate.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .actions import ExternalProcessAction
from .data import FileData, GlobData
from .layers import BuildLayer, NodeContext
from .metadata import TimeAndHashChangeDetector
from .process import ProcessResult

_ENTRY = "entry.json"
_FILES = "files"

# A cache hit refreshes its entry's recency (mtime) so eviction is LRU by
# *use*, not by creation — a hot dependency compiled long ago but restored
# every build must outlive a cold one. Refreshing is coarsened to this many
# seconds so repeated hits inside one window don't each write the inode.
_TOUCH_COARSENING_SECONDS = 3600


class CacheStoreProtocol(Protocol):
    """What `CacheLayer` requires of a store. Structural — a store need not
    subclass this, only match the three methods.

    An ``entry`` is an opaque dict a store returns from `load` and receives
    back in `restore`; the layer only reads ``entry["result"]`` (a
    ``ProcessResult`` as a dict) from it. Everything else in the entry is
    the store's private business. `restore` copies the entry's cached
    outputs to ``ctx.output_root`` and returns whether it fully succeeded;
    a store may let I/O errors propagate — the layer catches them.
    """

    def load(self, fingerprint: str) -> dict | None: ...
    def restore(self, fingerprint: str, entry: dict, ctx: NodeContext) -> bool: ...
    def store(
        self, fingerprint: str, result: ProcessResult, outputs: dict[str, Path]
    ) -> None: ...


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


def _dir_size(path: Path) -> int:
    """Total size of the files under an entry dir (entry.json + blobs)."""
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            pass
    return total


def _evict(entry_dir: Path, size: int) -> int:
    """Delete one entry dir; return the bytes reclaimed (0 on failure).
    Rename-then-remove so a concurrent `load` sees it vanish atomically
    rather than reading a half-deleted entry."""
    victim = entry_dir.with_name(f".tmp-evict-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        entry_dir.rename(victim)
    except OSError:
        return 0  # another build already evicted or is writing it
    shutil.rmtree(victim, ignore_errors=True)
    return size


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
        self._touch(fingerprint)
        return True

    def _touch(self, fingerprint: str) -> None:
        """Refresh the entry's recency so `prune` sees it as recently used.
        Best-effort and coarsened: skip the write if it was touched within
        `_TOUCH_COARSENING_SECONDS` (and never fail a hit over a stat/utime
        error)."""
        entry = self._entry_dir(fingerprint) / _ENTRY
        try:
            now = time.time()
            if now - entry.stat().st_mtime < _TOUCH_COARSENING_SECONDS:
                return
            os.utime(entry, (now, now))
        except OSError:
            pass

    def prune(self, *, max_bytes: int | None = None, max_age: float | None = None) -> int:
        """Bound the store: delete entries by TTL then by size, oldest (least
        recently *used*) first. Returns the number of bytes reclaimed.

        Eviction is always safe — every entry is reproducible, so removing one
        only ever turns a future hit into a recompute, never a wrong answer.
        A victim a concurrent build is mid-restore of degrades that build to a
        cache miss (handled in `CacheLayer`), never a failure. Half-written
        ``.tmp-*`` staging dirs are ignored (another build owns them).
        """
        entries: list[tuple[float, int, Path]] = []  # (mtime, size, dir)
        for prefix in self.directory.iterdir() if self.directory.is_dir() else []:
            if not prefix.is_dir() or prefix.name.startswith(".tmp-"):
                continue
            for entry_dir in prefix.iterdir():
                if not entry_dir.is_dir():
                    continue
                try:
                    mtime = (entry_dir / _ENTRY).stat().st_mtime
                except OSError:
                    continue  # no entry.json → mid-write or corrupt; leave it
                entries.append((mtime, _dir_size(entry_dir), entry_dir))
        entries.sort()  # oldest first

        now = time.time()
        reclaimed = 0
        # TTL pass: anything older than max_age goes regardless of size.
        if max_age is not None:
            survivors: list[tuple[float, int, Path]] = []
            for mtime, size, entry_dir in entries:
                if now - mtime > max_age:
                    reclaimed += _evict(entry_dir, size)
                else:
                    survivors.append((mtime, size, entry_dir))
            entries = survivors
        # Size pass: evict oldest survivors until the total is under max_bytes.
        if max_bytes is not None:
            total = sum(size for _, size, _ in entries)
            for mtime, size, entry_dir in entries:
                if total <= max_bytes:
                    break
                total -= size
                reclaimed += _evict(entry_dir, size)
        return reclaimed

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


class TwoTierCacheStore:
    """A fast local store in front of a slower shared one.

    A read checks the ``front`` first, so a local hit costs no round-trip to
    the ``back`` (typically a remote/team cache). A ``back`` hit is served
    and then *promoted* into the front, so the next build on this machine
    hits locally. A write populates the front and best-effort the back — a
    write to an unreachable back never blocks local caching.

    Both tiers must share the entry shape used here: ``entry["result"]`` (a
    ``ProcessResult`` dict) and ``entry["outputs"]`` (an iterable of the
    logical output paths). The built-in `CacheStore` satisfies this; so does
    any store modeled on it. Promotion never touches a tier's internal blob
    layout — it re-stores from the files `restore` just wrote — so the two
    tiers need not share physical storage, only that entry shape.
    """

    def __init__(self, front: CacheStoreProtocol, back: CacheStoreProtocol) -> None:
        self.front = front
        self.back = back

    def load(self, fingerprint: str) -> dict | None:
        entry = self.front.load(fingerprint)
        if entry is not None:
            entry["_tier"] = "front"
            return entry
        entry = self.back.load(fingerprint)  # may raise; the layer degrades
        if entry is not None:
            entry["_tier"] = "back"
        return entry

    def restore(self, fingerprint: str, entry: dict, ctx: NodeContext) -> bool:
        tier = self.back if entry.get("_tier") == "back" else self.front
        if not tier.restore(fingerprint, entry, ctx):
            return False
        if tier is self.back:
            self._promote(fingerprint, entry, ctx)
        return True

    def store(self, fingerprint: str, result: ProcessResult, outputs: dict[str, Path]) -> None:
        self.front.store(fingerprint, result, outputs)
        try:
            self.back.store(fingerprint, result, outputs)
        except Exception:
            pass  # a down back must never stop local caching

    def _promote(self, fingerprint: str, entry: dict, ctx: NodeContext) -> None:
        """Copy a back-tier hit into the front, using the files `restore`
        just wrote to the output root. Best-effort: a promotion failure must
        not turn a valid back hit into a miss."""
        try:
            result = ProcessResult(**entry["result"])
            outputs = {
                logical: ctx.output_root / logical
                for logical in entry.get("outputs", {})
            }
            self.front.store(fingerprint, result, outputs)
        except Exception:
            pass


class CacheLayer(BuildLayer):
    """Answer stale nodes from the store; populate it after real runs.

    All three store operations are guarded: an exception from `load`,
    `restore`, or `store` — an unreachable remote, a permission error, a
    malformed entry — degrades to a cache miss or a skipped populate, never
    a failed build. This is why a leaf store may stay dumb and let its I/O
    errors propagate; the resilience lives here.
    """

    def __init__(self, store: CacheStoreProtocol, detector: TimeAndHashChangeDetector) -> None:
        self.store = store
        self.detector = detector

    def around_node(self, node, ctx, inner):
        from .incremental import action_fingerprint

        action = node.action
        if not isinstance(action, ExternalProcessAction):
            return inner(ctx)
        fingerprint = action_fingerprint(action, ctx, self.detector)
        hit = self._try_hit(fingerprint, ctx)
        if hit is not None:
            return hit
        result = inner(ctx)
        cacheable = (
            result.source == "process"
            and result.signal is None
            and result.exit_code is not None
        )
        if cacheable:
            outputs = _output_files(action, ctx) if result.ok else {}
            try:
                self.store.store(fingerprint, result, outputs)
            except Exception:
                pass  # best-effort populate; a store failure never fails the build
        return result

    def _try_hit(self, fingerprint: str, ctx: NodeContext) -> ProcessResult | None:
        """A cache hit as a ready ProcessResult, or None to run the work.
        Any store error (unreachable, corrupt entry) is a miss."""
        try:
            entry = self.store.load(fingerprint)
            if entry is None or not self.store.restore(fingerprint, entry, ctx):
                return None
            restored = dict(entry["result"])
            restored["source"] = "cache"
            return ProcessResult(**restored)
        except Exception:
            return None
