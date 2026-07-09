"""Content-identity plugins: fingerprint a file by *meaning*, not bytes.

A `ContentIdentity` turns a file's bytes into a digest of some semantic
identity — e.g. ``jvm-abi`` hashes a jar's API surface, not its bytes — so a
dependent's fingerprint changes only when the meaning it depends on changes.
An identity is named on a data expression:

    data.file("build/lib/util.jar", identity="jvm-abi")

and resolved here at fingerprint time. Identity is opt-in per data expression;
the default is byte hashing. It affects **fingerprint material only**: what is
stored, staged, restored, and compared for output-intactness still uses bytes
(see `docs/java-fast-builds.md`).

The versioned tag ``<name>@<version>`` is part of the fingerprint payload, so
bumping a plugin's ``version`` cleanly invalidates everything it fingerprinted.
Bump it on **any** change to extraction or normalization.

The built-ins (``jvm-abi``, ``ts-dts``) are also implemented in the Rust core
(`rust/brix-core/src/identity.rs`) so every skin gets them; the digests are
cross-engine cache keys, so the Python code here remains the **reference
implementation**, and a version bump in one implementation must land in both.
A *custom* plugin registered here exists only in this skin — graphs using one
stay on the Python engine (`lowering.supports_rust_engine`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ContentIdentity(Protocol):
    """A registered content-identity plugin."""

    name: str
    version: str  # bump on ANY change to extraction/normalization

    def digest(self, path: Path) -> str:
        """The identity digest of the file. Raises on a file it cannot
        interpret (the engine falls back to byte hashing under a ``:raw``
        tag)."""
        ...


_REGISTRY: dict[str, ContentIdentity] = {}


def register(identity: ContentIdentity) -> None:
    """Register a plugin under its ``name`` (replacing any existing one)."""
    _REGISTRY[identity.name] = identity


def get(name: str) -> ContentIdentity:
    """Resolve a name to its plugin. Unknown names are loud — a typo in
    ``identity=`` must not silently degrade to byte hashing."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(
            f"unknown content identity {name!r}; registered: {known}"
        ) from None


def tag(identity: ContentIdentity) -> str:
    """The versioned fingerprint tag, e.g. ``jvm-abi@1``."""
    return f"{identity.name}@{identity.version}"


class JvmAbiIdentity:
    """Java ABI hashing: a dependent recompiles on API changes, not on
    implementation-only changes. Delegates to the pure-Python parser in
    `brix.jvmabi`."""

    name = "jvm-abi"
    version = "1"

    def digest(self, path: Path) -> str:
        from . import jvmabi

        return jvmabi.digest(path)


register(JvmAbiIdentity())


class TsDtsIdentity:
    """TypeScript declaration (`.d.ts`) hashing: a consumer recompiles on an
    API-surface change, not on an implementation-only change or a doc-comment
    edit. Delegates to the pure-Python normalizer in `brix.tsdts`."""

    name = "ts-dts"
    version = "1"

    def digest(self, path: Path) -> str:
        from . import tsdts

        return tsdts.digest(path)


register(TsDtsIdentity())
