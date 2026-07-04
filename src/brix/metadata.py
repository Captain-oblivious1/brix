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
    return {"version": _VERSION, "files": {}, "actions": {}, "memberships": {}}


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

    def cached_hash(self, path_key: str, signature: list[int]) -> str | None:
        entry = self._data["files"].get(path_key)
        if entry and entry["sig"] == signature:
            return entry["sha256"]
        return None

    def store_hash(self, path_key: str, signature: list[int], digest: str) -> None:
        with self._lock:
            self._data["files"][path_key] = {"sig": signature, "sha256": digest}

    # -- action records ----------------------------------------------------

    def action_record(self, key: str) -> dict | None:
        return self._data["actions"].get(key)

    def record_action(self, key: str, record: dict) -> None:
        with self._lock:
            self._data["actions"][key] = record

    def forget_action(self, key: str) -> None:
        with self._lock:
            self._data["actions"].pop(key, None)

    def action_records(self) -> dict[str, dict]:
        return dict(self._data["actions"])

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
