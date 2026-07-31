"""The asset store (Python engine half): digest identity + resolution.

The pinned sha256 constants below are the same ones the Rust store's unit test
asserts (`rust/brix-core/src/assets.rs`), computed independently with
`sha256sum`. A three-way match — sha256sum, Python, Rust — is the cross-engine
digest-identity proof that keeps a shared cache valid across frontends and
machines (docs/asset-engine.md). Regenerate when a helper is migrated/edited.
"""

import hashlib
from pathlib import Path

import pytest

from brix import assets

# name -> sha256 hex of the shipped bytes.
PINNED = {
    "java/JavacWorker.java": "f274836d48914a0cb7597076c170aa9ef7904f41d745efe33716af427a6c28e8",
    "java/KotlincWorker.java": "25c088d464f8334b8fa46ac17d50168cbc4c091e61e8d4dd3212844600903989",
    "java/BootRepackager.java": "aa74dfedfb36a713771a5efa3332aaada15c3b991dff373173bf6b0f616c5355",
    "java/junit_classload.py": "897057ad403ad339e330ab7d05062cc46c9800eba904fefd3c79088a4b9914b3",
    "ts/tsc_listfiles.py": "876b5801c278b61af3ead4682d46fea9975ec63305daf843e92fc487fb99dfdf",
}


@pytest.mark.parametrize("name,sha", PINNED.items())
def test_digest_is_pinned_byte_hash(name: str, sha: str) -> None:
    assert assets.digest(name) == sha


def test_digest_is_the_content_hash_of_the_resolved_bytes(name="java/JavacWorker.java") -> None:
    # The digest must be exactly sha256 of what resolve_path serves — no path,
    # no mtime folded in — so it matches a `file` atom's byte hash.
    served = Path(assets.resolve_path(name)).read_bytes()
    assert assets.digest(name) == hashlib.sha256(served).hexdigest()


def test_resolve_path_is_an_existing_file() -> None:
    p = Path(assets.resolve_path("ts/tsc_listfiles.py"))
    assert p.is_file()


def test_unknown_name_is_a_loud_error() -> None:
    with pytest.raises(assets.UnknownAssetError):
        assets.digest("java/Nope.java")
    with pytest.raises(assets.UnknownAssetError):
        assets.resolve_path("java/Nope.java")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../secret", "java/../../x", "a\\b"])
def test_path_traversal_is_rejected(bad: str) -> None:
    with pytest.raises(assets.UnknownAssetError):
        assets.resolve_path(bad)
