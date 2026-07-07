"""Remote HTTP cache backend — a `CacheStoreProtocol` store over plain
GET/PUT, so a shared team/CI cache can front or back the local one.

Layout mirrors the local `CacheStore` (see cache.py): an entry lives at
``<base>/<fp[:2]>/<fp>/entry.json`` with content blobs at
``<base>/<fp[:2]>/<fp>/files/<n>``. Content-addressed, so a write is
idempotent and a stale read can only miss, never make a false hit. Keeping the
same path layout means a local cache directory and a remote one are the same
shape — one can be synced into the other.

The one field the remote carries that the local layout doesn't need is
``file_modes``. The local store gets executable bits for free (it ``copy2``s
the blob, which is itself a mode-preserving file); bytes over HTTP carry no
mode, so modes travel in the entry and are re-applied on restore — otherwise a
restored binary would come back non-executable.

Transport is deliberately generic (stdlib ``urllib``, no new dependency): any
server honoring GET/PUT works — Nexus, Artifactory, an S3-compatible gateway,
or a plain file server. Auth is a pluggable header hook. All resilience lives
in `CacheLayer`, so this store returns a miss on a read failure and may let a
write error propagate; the layer turns either into "just run the work."
"""

from __future__ import annotations

import json
import os
import stat as stat_mod
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .cache import _ENTRY, _FILES  # the shared layout constants
from .layers import NodeContext
from .process import ProcessResult

#: Returns request headers (e.g. ``{"Authorization": "Bearer ..."}``). Called
#: per request so short-lived tokens can refresh.
AuthHook = Callable[[], "dict[str, str]"]


class HttpCacheStore:
    """Content-addressed result store backed by an HTTP GET/PUT endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthHook | None = None,
        retries: int = 2,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.retries = retries
        self.timeout = timeout

    # -- URLs (mirror the local CacheStore layout) -------------------------

    def _prefix(self, fingerprint: str) -> str:
        return f"{self.base_url}/{fingerprint[:2]}/{fingerprint}"

    def _entry_url(self, fingerprint: str) -> str:
        return f"{self._prefix(fingerprint)}/{_ENTRY}"

    def _blob_url(self, fingerprint: str, name: str) -> str:
        return f"{self._prefix(fingerprint)}/{_FILES}/{name}"

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, url: str, data: bytes | None = None) -> bytes | None:
        """Perform one request with retries. ``None`` on 404 (a definite
        absence, not retried); the response body on 2xx; raises on a transport
        failure that outlived the retries (callers decide how to degrade)."""
        headers = dict(self.auth()) if self.auth is not None else {}
        last_exc: Exception | None = None
        for _ in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None  # definite miss — do not retry
                last_exc = exc
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
        raise last_exc if last_exc is not None else RuntimeError("unreachable")

    # -- CacheStoreProtocol ------------------------------------------------

    def load(self, fingerprint: str) -> dict | None:
        try:
            body = self._request("GET", self._entry_url(fingerprint))
        except Exception:
            return None  # transport failure -> miss; CacheLayer degrades
        if body is None:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None  # corrupt entry -> miss

    def restore(self, fingerprint: str, entry: dict, ctx: NodeContext) -> bool:
        """Fetch every blob first, then write — so a missing/failed blob is a
        clean miss (``False``) with no partial outputs left behind, matching
        the local store."""
        modes = entry.get("file_modes", {})
        pending: list[tuple[Path, bytes, int | None]] = []
        for logical, blob in entry.get("outputs", {}).items():
            try:
                body = self._request("GET", self._blob_url(fingerprint, blob))
            except Exception:
                return False
            if body is None:
                return False
            pending.append((ctx.output_root / logical, body, modes.get(logical)))
        for dest, body, mode in pending:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            if mode is not None:
                os.chmod(dest, mode)
        return True

    def store(self, fingerprint: str, result: ProcessResult, outputs: dict[str, Path]) -> None:
        """Upload blobs, then the entry last — so an entry is only ever visible
        once all its blobs are (a partial upload reads back as a miss)."""
        names: dict[str, str] = {}
        modes: dict[str, int] = {}
        for index, (logical, physical) in enumerate(sorted(outputs.items())):
            name = str(index)
            names[logical] = name
            modes[logical] = stat_mod.S_IMODE(os.stat(physical).st_mode)
            self._request("PUT", self._blob_url(fingerprint, name), physical.read_bytes())
        entry = {"result": asdict(result), "outputs": names, "file_modes": modes}
        self._request("PUT", self._entry_url(fingerprint), json.dumps(entry, sort_keys=True).encode())
