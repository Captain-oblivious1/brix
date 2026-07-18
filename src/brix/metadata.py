"""Persistent build metadata: the incremental layer's private storage.

One JSON file per workspace (default ``build/.brix/metadata.json``) holding:

- a file-hash cache keyed by absolute path, validated by mtime/size, so
  unchanged files are never re-read;
- one record per action (fingerprint, last result, output hashes);
- collection membership from the previous build, which is what makes
  deleted-source cleanup detectable at all.

Metadata is written in agreement with what actually happened: records are
stored on success, forgotten on failure, and the file is saved at build end.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

_VERSION = 1


def _fresh() -> dict:
    return {"version": _VERSION, "files": {}, "actions": {}, "memberships": {}, "deps": {}}


class BuildMetadata:
    """Load/store for the metadata file. Thread-safe for concurrent nodes."""

    def __init__(self, root: Path, directory: str | Path = "build/.brix") -> None:
        self.root = Path(root)
        self.directory = self.root / directory
        self.path = self.directory / "metadata.json"
        self._lock = threading.Lock()
        self._data = _fresh()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._data = _fresh()
            return
        if not isinstance(loaded, dict) or loaded.get("version") != _VERSION:
            self._data = _fresh()
            return
        self._data = loaded

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with self._lock:
            text = json.dumps(self._data, sort_keys=True, indent=1)
        tmp.write_text(text)
        tmp.replace(self.path)

    # -- file-hash cache ---------------------------------------------------
    #
    # One entry per absolute path, validated by (mtime_ns, size). It holds the
    # byte hash under "sha256" and any content-identity digests under their
    # versioned tag (e.g. "jvm-abi@1") — same stat validation, so an unchanged
    # file costs one stat whether it is byte- or identity-hashed.

    def _entry_for(self, path_key: str, signature: list[int]) -> dict:
        """The sig-keyed entry for a path, reset if the signature changed
        (a changed file invalidates its byte hash and every identity digest)."""
        entry = self._data["files"].get(path_key)
        if entry is None or entry.get("sig") != signature:
            entry = {"sig": signature}
            self._data["files"][path_key] = entry
        return entry

    def cached_hash(self, path_key: str, signature: list[int]) -> str | None:
        entry = self._data["files"].get(path_key)
        if entry and entry.get("sig") == signature:
            return entry.get("sha256")
        return None

    def store_hash(self, path_key: str, signature: list[int], digest: str) -> None:
        with self._lock:
            self._entry_for(path_key, signature)["sha256"] = digest

    def cached_identity(self, path_key: str, signature: list[int], tag: str) -> str | None:
        entry = self._data["files"].get(path_key)
        if entry and entry.get("sig") == signature:
            return entry.get(tag)
        return None

    # Signature-free lookups: the *last-build* digest for a path regardless of
    # its current stat signature. Only sound when a caller has independently
    # established the file is unchanged since that build — the git detector,
    # which vouches a tracked+unmodified file without stat-ing it.

    def hash_by_path(self, path_key: str) -> str | None:
        entry = self._data["files"].get(path_key)
        return entry.get("sha256") if entry else None

    def identity_by_path(self, path_key: str, tag: str) -> str | None:
        entry = self._data["files"].get(path_key)
        return entry.get(tag) if entry else None

    # -- git change-detection baseline -------------------------------------
    #
    # The HEAD commit sha as of the last build, so the git detector can ask
    # "what changed since then" (working tree + commits), paired with the
    # working-tree paths that were *dirty at that build*. The dirty set closes
    # the revert hole: a file dirty at build N was hashed with its dirty
    # content, so even if git reports it clean at build N+1 (reverted to HEAD),
    # its cached hash is not trustworthy. Both absent until a git-accelerated
    # build records them; missing means "vouch nothing" — the safe stat scan.

    def git_baseline(self) -> str | None:
        return self._data.get("git_baseline")

    def set_git_baseline(self, sha: str | None) -> None:
        with self._lock:
            if sha is None:
                self._data.pop("git_baseline", None)
            else:
                self._data["git_baseline"] = sha

    def git_dirty(self) -> list[str] | None:
        """Repo-relative paths that were dirty in the working tree at the last
        build, or None if never recorded (pre-dirty-set metadata, or the last
        build could not read git status)."""
        return self._data.get("git_dirty")

    def set_git_dirty(self, paths: list[str] | None) -> None:
        with self._lock:
            if paths is None:
                self._data.pop("git_dirty", None)
            else:
                self._data["git_dirty"] = sorted(paths)

    def store_identity(
        self, path_key: str, signature: list[int], tag: str, digest: str
    ) -> None:
        with self._lock:
            self._entry_for(path_key, signature)[tag] = digest

    # -- action records ----------------------------------------------------

    def action_record(self, key: str) -> dict | None:
        return self._data["actions"].get(key)

    def record_action(self, key: str, record: dict) -> None:
        with self._lock:
            self._data["actions"][key] = record

    def forget_action(self, key: str) -> None:
        with self._lock:
            self._data["actions"].pop(key, None)
            self._data.get("deps", {}).pop(key, None)

    # -- discovered (scanned) dependencies ---------------------------------
    #
    # Extra input paths a tool reported reading at build time (e.g. C/C++
    # headers from a compiler depfile) that the graph did not declare. Keyed by
    # action_key and folded into the action's fingerprint on the next build, so
    # editing one of them re-stales the action.

    def discovered_deps(self, key: str) -> list[str] | None:
        return self._data.get("deps", {}).get(key)

    def set_discovered_deps(self, key: str, paths: list[str]) -> None:
        with self._lock:
            self._data.setdefault("deps", {})[key] = sorted(paths)

    def action_records(self) -> dict[str, dict]:
        return dict(self._data["actions"])

    def has_failed_actions(self) -> bool:
        """Did the last recorded build leave any action in a failed state?
        The lazy-loading selector treats this as "the previous selection's
        work is unfinished" and unions it into the next selection."""
        return any(not r.get("ok", True) for r in self._data["actions"].values())

    def all_discovered_deps(self) -> set[str]:
        """Every scanned dependency path across all actions (repo-relative).
        The deps table is keyed by action_key — expansion keys that cannot be
        attributed to a package without loading it — so the selector can only
        use it as a trip-wire: a change to any scanned path forces a full
        load."""
        return {p for paths in self._data.get("deps", {}).values() for p in paths}

    # -- collection membership ----------------------------------------------

    def membership(self, key: str) -> list[str] | None:
        return self._data["memberships"].get(key)

    def set_membership(self, key: str, members: list[str]) -> None:
        with self._lock:
            self._data["memberships"][key] = sorted(members)


class TimeAndHashChangeDetector:
    """Content hashing with a cheap scan: hash only when mtime/size changed.

    The default change detector. Absolute paths are hashed on demand and the
    (mtime, size) → sha256 mapping is kept in the persistent metadata, so an
    unchanged file costs one stat per build.
    """

    def __init__(self, root: Path | str = ".", metadata: BuildMetadata | None = None) -> None:
        self.root = Path(root)
        self.metadata = metadata if metadata is not None else BuildMetadata(self.root)

    def stat_signature(self, path: Path) -> list[int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return [st.st_mtime_ns, st.st_size]

    def hash_file(self, path: Path) -> str | None:
        """sha256 of the file's content; None if missing or not a regular
        file (directories declared as outputs/inputs hash as None)."""
        if not path.is_file():
            return None
        signature = self.stat_signature(path)
        if signature is None:
            return None
        key = str(path)
        cached = self.metadata.cached_hash(key, signature)
        if cached is not None:
            return cached
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.metadata.store_hash(key, signature, digest)
        return digest

    def identity_digest(self, path: Path, identity) -> str | None:
        """The content-identity digest of a file, cached like `hash_file` and
        validated by the same stat signature. None if missing or not a regular
        file. Propagates the plugin's error on a file it cannot interpret — the
        caller decides the fallback."""
        from .identity import tag as identity_tag

        if not path.is_file():
            return None
        signature = self.stat_signature(path)
        if signature is None:
            return None
        key = str(path)
        tag = identity_tag(identity)
        cached = self.metadata.cached_identity(key, signature, tag)
        if cached is not None:
            return cached
        digest = identity.digest(path)  # may raise; caller falls back to :raw
        self.metadata.store_identity(key, signature, tag, digest)
        return digest
