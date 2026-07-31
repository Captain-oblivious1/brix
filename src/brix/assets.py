"""The asset store (Python engine half).

An ``asset`` atom names a file brix itself ships — a compiler-worker source, a
``--listFiles`` recorder — by a stable logical ``name`` (POSIX, brix-relative)
instead of a machine path. This module is the engine-side authority the name
resolves against: it owns the bytes, hands render an absolute path, and hands
fingerprinting a **content digest that is a function of the bytes alone** — never
the resolved path, so it is identical across machines and across the Python/TS
frontends (docs/asset-engine.md, docs/boundary-schema.md).

The Rust engine mirrors this exactly (``rust/brix-core/src/assets.rs``); the two
must agree on both the set of shipped names and each name's digest. The
cross-engine identity is the soundness-critical invariant and is asserted by the
differential test (``test_assets.py`` + the Rust ``assets`` unit test).

The canonical bytes live in the top-level ``assets/`` tree; the logical name is a
file's POSIX path under it (e.g. ``java/JavacWorker.java``). Python reads that
tree on disk; Rust embeds it at build time.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# src/brix/assets.py -> parents[2] is the repo root that holds ``assets/``.
_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"


class UnknownAssetError(ValueError):
    """A name the store does not ship. Raised before anything runs, on both the
    fingerprint and the render path — a graph can never silently reference a
    helper the engine cannot produce."""


def _path_for(name: str) -> Path:
    # Names are logical POSIX paths under assets/; reject anything that would
    # escape the tree (absolute, ``..``, backslash) before touching the disk.
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ".." in name.split("/")
    ):
        raise UnknownAssetError(f"asset not shipped: {name!r}")
    path = _ASSETS_DIR.joinpath(*name.split("/"))
    if not path.is_file():
        raise UnknownAssetError(f"asset not shipped: {name!r}")
    return path


def resolve_path(name: str) -> str:
    """The absolute filesystem path a command line names for this asset. Not
    fingerprint material (the path differs per machine) — for render only."""
    return str(_path_for(name))


def digest(name: str) -> str:
    """sha256 hex of the asset's bytes — the same content hash the change
    detector computes for a ``file`` atom, so both are directly comparable and
    an asset edit re-stales exactly like a source edit. Byte-only: no path, no
    mtime; identical on every machine and in both engines."""
    return hashlib.sha256(_path_for(name).read_bytes()).hexdigest()
