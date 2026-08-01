"""The asset store (Python engine half).

An ``asset`` atom names a file brix itself ships — a compiler-worker source, a
``--listFiles`` recorder — by a stable logical ``name`` (POSIX, brix-relative)
instead of a machine path. This module is the engine-side authority the name
resolves against: it owns the bytes, hands render an absolute path, and hands
fingerprinting a **content digest that is a function of the bytes alone** — never
the resolved path, so it is identical across machines and across the Python/TS
frontends (docs/asset-engine.md, docs/boundary-schema.md).

The bytes come from one of two places, in order:

1. the top-level ``assets/`` tree on disk — present in a source checkout, and the
   dev's live copy (edit a helper, iterate without rebuilding the extension);
2. otherwise, the copy **embedded in the ``brix_py`` extension** (the engine owns
   the bytes) — this is the installed-wheel path, where no ``assets/`` tree ships.

Both yield the same sha256-of-bytes digest, so a build is byte-identical either
way. The Rust engine mirrors this store exactly
(``rust/brix-core/src/assets.rs``); the two must agree on both the set of shipped
names and each name's digest — the soundness-critical invariant asserted by the
differential test.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

# src/brix/assets.py -> parents[2] is the repo root that holds ``assets/``.
_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"


class UnknownAssetError(ValueError):
    """A name the store does not ship. Raised before anything runs, on both the
    fingerprint and the render path — a graph can never silently reference a
    helper the engine cannot produce."""


def _is_safe_name(name: str) -> bool:
    # Names are logical POSIX paths under assets/; reject anything that would
    # escape the tree (empty, absolute, ``..``, backslash) before use.
    return bool(name) and not (
        name.startswith("/") or "\\" in name or ".." in name.split("/")
    )


def _disk_path(name: str) -> Path | None:
    """The on-disk path under ``assets/`` if that tree is present and holds the
    file; ``None`` otherwise (installed wheel, or unknown name)."""
    if not _is_safe_name(name):
        raise UnknownAssetError(f"asset not shipped: {name!r}")
    path = _ASSETS_DIR.joinpath(*name.split("/"))
    return path if path.is_file() else None


def _embedded_bytes(name: str) -> bytes:
    """The bytes embedded in the brix_py extension. The fallback when no on-disk
    ``assets/`` tree is present."""
    if not _is_safe_name(name):
        raise UnknownAssetError(f"asset not shipped: {name!r}")
    from . import engine

    try:
        return bytes(engine._extension().asset_bytes(name))
    except ValueError as exc:  # the extension raises for an unshipped name
        raise UnknownAssetError(f"asset not shipped: {name!r}") from exc


def digest(name: str) -> str:
    """sha256 hex of the asset's bytes — the same content hash the change
    detector computes for a ``file`` atom, so both are directly comparable and an
    asset edit re-stales exactly like a source edit. Byte-only: no path, no
    mtime; identical on every machine and in both engines."""
    disk = _disk_path(name)
    data = disk.read_bytes() if disk is not None else _embedded_bytes(name)
    return hashlib.sha256(data).hexdigest()


def resolve_path(name: str) -> str:
    """The absolute filesystem path a command line names for this asset. Not
    fingerprint material (the path differs per machine) — for render only. From a
    source checkout this is the on-disk ``assets/`` file directly; from an
    installed wheel the embedded bytes are materialized to a content-addressed
    location outside any workspace (so a materialized helper can't be swept into a
    source glob)."""
    disk = _disk_path(name)
    if disk is not None:
        return str(disk)
    data = _embedded_bytes(name)
    root = Path(tempfile.gettempdir()) / "brix-assets"
    dest = root / hashlib.sha256(data).hexdigest() / name
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    return str(dest)
