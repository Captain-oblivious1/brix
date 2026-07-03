"""Logical data expressions.

These are the user-facing declarations of *what data exists*: files, file
collections, tools, and literal values. They are clean-build truth — they say
nothing about caching, staging, or incremental state.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from .patterns import PathPattern


class DataExpr:
    """Base class for all logical data expressions."""

    def __add__(self, other: DataExpr) -> DataUnion:
        if not isinstance(other, DataExpr):
            return NotImplemented
        return DataUnion(self, other)

    def atoms(self) -> Iterator[DataAtom]:
        raise NotImplementedError


class DataAtom(DataExpr):
    """A leaf data expression."""

    def atoms(self) -> Iterator[DataAtom]:
        yield self

    def substitute(self, bindings: dict[str, str]) -> DataAtom:
        """Return a copy with ``{name}`` bindings applied where meaningful."""
        return self


class FileData(DataAtom):
    """One logical path. It need not exist yet."""

    def __init__(self, path: str) -> None:
        self.pattern = PathPattern(path)

    @property
    def path(self) -> str:
        return self.pattern.template

    def substitute(self, bindings: dict[str, str]) -> FileData:
        return FileData(self.pattern.substitute(bindings))

    def __repr__(self) -> str:
        return f"data.file({self.path!r})"


class GlobData(DataAtom):
    """A path set with optional named bindings, e.g. ``src/{name}.cpp``."""

    def __init__(self, pattern: str) -> None:
        self.pattern = PathPattern(pattern)

    def substitute(self, bindings: dict[str, str]) -> GlobData:
        return GlobData(self.pattern.substitute(bindings))

    def __repr__(self) -> str:
        return f"data.glob({self.pattern.template!r})"


class RegexData(DataAtom):
    """A path set matched by a regular expression with named groups.

    Prototype support is match-only: a regex collection can drive membership
    but cannot be used as a substitutable output template.
    """

    def __init__(self, pattern: str) -> None:
        self.regex = re.compile(pattern)
        self.binding_names = tuple(self.regex.groupindex)

    def match(self, path: str) -> dict[str, str] | None:
        m = self.regex.fullmatch(path)
        if m is None:
            return None
        return dict(m.groupdict())

    def __repr__(self) -> str:
        return f"data.regex({self.regex.pattern!r})"


class ToolData(DataAtom):
    """A tool identity, e.g. ``c++`` or an absolute path."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"data.tool({self.name!r})"


class ValueData(DataAtom):
    """Literal data that affects an output (options, flags, settings)."""

    def __init__(self, text: str) -> None:
        self.text = text

    def substitute(self, bindings: dict[str, str]) -> ValueData:
        return ValueData(PathPattern(self.text).substitute(bindings)) if self.text else self

    def __repr__(self) -> str:
        return f"data.value({self.text!r})"


class DataUnion(DataExpr):
    """A union of data expressions."""

    def __init__(self, *parts: DataExpr) -> None:
        flat: list[DataExpr] = []
        for part in parts:
            if isinstance(part, DataUnion):
                flat.extend(part.parts)
            else:
                flat.append(part)
        self.parts: tuple[DataExpr, ...] = tuple(flat)

    def atoms(self) -> Iterator[DataAtom]:
        for part in self.parts:
            yield from part.atoms()

    def __repr__(self) -> str:
        return " + ".join(repr(p) for p in self.parts)


class DataFactory:
    """Constructor namespace exposed as ``graph.data``."""

    def file(self, path: str) -> FileData:
        return FileData(path)

    def glob(self, pattern: str) -> GlobData:
        return GlobData(pattern)

    def regex(self, pattern: str) -> RegexData:
        return RegexData(pattern)

    def tool(self, name: str) -> ToolData:
        return ToolData(name)

    def value(self, text: str) -> ValueData:
        return ValueData(text)
