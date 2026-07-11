"""Resolved third-party dependency sets.

A `DepSet` is what a resolver adapter (``java.pom``, ``java.maven``, and the
future ``cpp.conan`` / ``cpp.pkg_config`` …) hands back: the artifact files a
build compiles or links against, plus any flags they require. brix does not
resolve dependencies itself — it consumes a resolver's output as tracked file
inputs (docs/dependencies.md). Typed compile/link actions consume a `DepSet`;
the *consumer* applies the content lens (a compile classpath is ABI-lensed, a
runtime classpath byte-lensed), so a `DepSet` stays lens-neutral.

It grows with ``+`` (ordered merge, first occurrence wins) and shrinks with
``-`` (exclude by fnmatch), mirroring brix's data algebra. Order is preserved
because for native linking it is load-bearing (``-lfoo -lbar`` ≠ the reverse);
for a JVM classpath it is harmless.

    deps = java.pom("pom.xml") + java.maven(["com.example:extra:1.0"])
    deps = deps - "*/commons-logging-*.jar"   # drop a transitively-pulled jar
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePath

from .data import FileData


def _dedup(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Order-preserving de-dup (first occurrence wins) — overlapping transitive
    closures otherwise repeat jars, and first-wins keeps link order stable."""
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


@dataclass(frozen=True)
class DepSet:
    """An ordered set of resolved artifact paths (jars, ``.a``/``.so``) plus the
    flags they require. Both flag tuples are empty for a JVM classpath (the
    classpath *is* the artifacts); C++ adapters populate ``compile_flags``
    (``-I``/``-D``) and ``link_flags`` (``-L``/``-l``) — separate because a ``-I``
    belongs on the compile and a ``-l`` on the link."""

    artifacts: tuple[str, ...] = ()
    compile_flags: tuple[str, ...] = ()
    link_flags: tuple[str, ...] = ()

    def __add__(self, other: object) -> "DepSet":
        if isinstance(other, DepSet):
            return DepSet(
                _dedup(self.artifacts + other.artifacts),
                self.compile_flags + other.compile_flags,
                self.link_flags + other.link_flags,
            )
        if isinstance(other, FileData):
            return self._with_artifacts(self.artifacts + (other.path,))
        if isinstance(other, str):
            return self._with_artifacts(self.artifacts + (other,))
        return NotImplemented

    def _with_artifacts(self, artifacts: tuple[str, ...]) -> "DepSet":
        return DepSet(_dedup(artifacts), self.compile_flags, self.link_flags)

    def __sub__(self, patterns: str | list[str] | tuple[str, ...]) -> "DepSet":
        pats = [patterns] if isinstance(patterns, str) else list(patterns)

        def excluded(path: str) -> bool:
            name = PurePath(path).name
            return any(fnmatch(path, p) or fnmatch(name, p) for p in pats)

        kept = tuple(a for a in self.artifacts if not excluded(a))
        return DepSet(kept, self.compile_flags, self.link_flags)

    def files(self) -> list[FileData]:
        """The artifacts as byte-lens file atoms (the consumer re-lenses)."""
        return [FileData(a) for a in self.artifacts]

    def __bool__(self) -> bool:
        return bool(self.artifacts or self.compile_flags or self.link_flags)
