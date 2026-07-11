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
    """An ordered set of resolved artifact paths (jars, and later ``.a``/``.so``)
    plus any flags they require. ``flags`` is empty for a JVM classpath (the
    classpath *is* the artifacts); it carries e.g. C++ ``-I``/``-l`` when the
    C++ adapters land."""

    artifacts: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()

    def __add__(self, other: object) -> "DepSet":
        if isinstance(other, DepSet):
            merged = _dedup(self.artifacts + other.artifacts)
            return DepSet(merged, self.flags + other.flags)
        if isinstance(other, FileData):
            return DepSet(_dedup(self.artifacts + (other.path,)), self.flags)
        if isinstance(other, str):
            return DepSet(_dedup(self.artifacts + (other,)), self.flags)
        return NotImplemented

    def __sub__(self, patterns: str | list[str] | tuple[str, ...]) -> "DepSet":
        pats = [patterns] if isinstance(patterns, str) else list(patterns)

        def excluded(path: str) -> bool:
            name = PurePath(path).name
            return any(fnmatch(path, p) or fnmatch(name, p) for p in pats)

        return DepSet(tuple(a for a in self.artifacts if not excluded(a)), self.flags)

    def files(self) -> list[FileData]:
        """The artifacts as byte-lens file atoms (the consumer re-lenses)."""
        return [FileData(a) for a in self.artifacts]

    def __bool__(self) -> bool:
        return bool(self.artifacts)
