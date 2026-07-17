"""Content-identity plugins: fingerprint a file by *meaning*, not bytes.

A `ContentIdentity` turns a file's bytes into a digest of some semantic
identity — e.g. ``jvm-abi`` hashes a jar's API surface, not its bytes — so a
dependent's fingerprint changes only when the meaning it depends on changes.
This is the Python reference engine's lens computation; the lens is selected
by an atom's ``content_key`` (set by a typed subclass like
``brix.java.AbiFile`` — the author writes ``java.abi(expr)``, never the
name). It is opt-in per atom; the default is byte hashing. It affects
**fingerprint material only**: what is stored, staged, restored, and compared
for output-intactness still uses bytes (see `docs/java-fast-builds.md` and
`docs/content-key.md`).

The versioned tag ``<name>@<version>`` is part of the fingerprint payload, so
bumping a plugin's ``version`` cleanly invalidates everything it fingerprinted.
Bump it on **any** change to extraction or normalization.

The built-ins (``jvm-abi``, ``ts-dts``) are also implemented in the Rust core
(`rust/brix-core/src/identity.rs`) so every skin gets them; the digests are
cross-engine cache keys, so the Python code here remains the **reference
implementation**, and a version bump in one implementation must land in both.
A *custom* plugin registered here crosses the boundary as the
``content_identity`` extension trait: the engine calls its ``digest`` back
per file (from build worker threads), and its digests cache in
metadata.json under the same versioned tag either engine would use.
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


class KotlinAbiIdentity:
    """Kotlin ABI hashing: jvm-abi semantics, except classes whose Kotlin
    metadata declares inline functions/accessors (whose implementation —
    bodies, line tables, nested synthetics — compiles into consumers) count
    by whole-class bytes. Delegates to the pure-Python parser in
    `brix.kotlinabi`."""

    name = "kotlin-abi"
    version = "1"

    def digest(self, path: Path) -> str:
        from . import kotlinabi

        return kotlinabi.digest(path)


register(KotlinAbiIdentity())


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


# The identities built into the Rust core (rust/brix-core/src/content_key.rs) —
# i.e. exactly what is registered above, at import, before any skin registers a
# custom plugin. Derived from `_REGISTRY` so there is one source of truth (the
# module-level `register(...)` calls): adding a built-in here — and porting it
# to the Rust core — makes it core-native automatically, with no parallel list
# to keep in sync. The boundary uses this to tell core-native content keys from
# custom ones (`custom_identity_names` in lowering.py).
_BUILTINS: frozenset[str] = frozenset(_REGISTRY)


def builtins() -> frozenset[str]:
    """The content-identity names the core builds in, as opposed to custom
    skin plugins registered later. A graph's content keys outside this set
    cross the engine boundary as `content_identity` digest callbacks."""
    return _BUILTINS
