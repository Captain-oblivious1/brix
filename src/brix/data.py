"""Logical data expressions.

These are the user-facing declarations of *what data exists*: files, file
collections, tools, and literal values. They are clean-build truth — they say
nothing about caching, staging, or incremental state.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from .patterns import PathPattern


# Public API surface (curated for documentation and as a contract).
__all__ = [
    'DataExpr',
    'DataAtom',
    'FileData',
    'GlobData',
    'RegexData',
    'ToolData',
    'ValueData',
    'DataUnion',
    'DataFactory',
]


class DataExpr:
    """Base class for all logical data expressions."""

    def __add__(self, other: DataExpr) -> DataUnion:
        if not isinstance(other, DataExpr):
            return NotImplemented
        return DataUnion(self, other)

    def atoms(self) -> Iterator[DataAtom]:
        raise NotImplementedError


class DataAtom(DataExpr):
    """A leaf data expression.

    ``content_key`` is the atom's *comparison semantics*: the name of the
    content lens fingerprinting compares it through (e.g. ``"jvm-abi"``).
    ``None`` — the default for a plain file/glob — is byte hashing. It is
    intrinsic to *what the datum is* (like the File/Tool/Value distinction),
    so a lens is a typed subclass (``brix.java``'s `AbiFile`/`AbiGlob`), not a
    string the author types. See docs/content-key.md.
    """

    content_key: str | None = None

    def atoms(self) -> Iterator[DataAtom]:
        yield self

    def substitute(self, bindings: dict[str, str]) -> DataAtom:
        """Return a copy with ``{name}`` bindings applied where meaningful."""
        return self


class FileData(DataAtom):
    """One logical path. It need not exist yet.

    Byte-hashed by default. A skin's typed subclass (``brix.java.AbiFile``)
    overrides ``content_key`` to fingerprint by meaning instead.
    """

    def __init__(self, path: str) -> None:
        self.pattern = PathPattern(path)

    @property
    def path(self) -> str:
        return self.pattern.template

    def substitute(self, bindings: dict[str, str]) -> FileData:
        # type(self) preserves a lens subclass (AbiFile stays an AbiFile).
        return type(self)(self.pattern.substitute(bindings))

    def __repr__(self) -> str:
        if self.content_key is not None:
            return f"{type(self).__name__}({self.path!r})"
        return f"data.file({self.path!r})"


class GlobData(DataAtom):
    """A path set with optional named bindings, e.g. ``src/{name}.cpp``.

    Byte-hashed by default; a skin's typed subclass (``brix.java.AbiGlob``)
    overrides ``content_key`` to apply a content lens to every matched file.

    Subtraction narrows membership: ``data.glob("{name}.cc") - "*_test.cc"``
    returns a copy whose `find` drops files matching the fnmatch pattern(s)
    (``*``/``?``/``[...]``, matched against the workspace-relative path, ``*``
    crossing ``/``). Membership only — substitution carries excludes through
    untouched, and the planner's edge inference ignores them
    (docs/content-key.md).
    """

    def __init__(
        self,
        pattern: str,
        exclude: tuple[str, ...] = (),
    ) -> None:
        self.pattern = PathPattern(pattern, tuple(exclude))

    @property
    def exclude(self) -> tuple[str, ...]:
        return self.pattern.exclude

    def __sub__(self, other: str | list[str] | tuple[str, ...]) -> GlobData:
        if isinstance(other, str):
            added: tuple[str, ...] = (other,)
        elif isinstance(other, (list, tuple)) and all(isinstance(p, str) for p in other):
            added = tuple(other)
        else:
            return NotImplemented
        # type(self) preserves a lens subclass through narrowing.
        return type(self)(self.pattern.template, self.exclude + added)

    def substitute(self, bindings: dict[str, str]) -> GlobData:
        return type(self)(self.pattern.substitute(bindings), self.exclude)

    def __repr__(self) -> str:
        if self.content_key is not None:
            text = f"{type(self).__name__}({self.pattern.template!r})"
        else:
            text = f"data.glob({self.pattern.template!r})"
        if self.exclude:
            text += f" - {list(self.exclude)!r}"
        return text


def _fill_named_groups(pattern: str, values: dict[str, str]) -> tuple[str | None, bool]:
    """Replace each ``(?P<name>...)`` whose name is in ``values`` with the
    (escaped) value, leaving groups whose names are not in ``values`` intact.

    Unlike a group-shaped regex, this scans the body with a paren-depth counter
    (and stays out of ``[...]`` character classes), so a body that itself
    contains groups — ``(?:a)|(?!b)c`` — is handled. That is the whole point:
    the naive ``\\(\\?P<name>(?:[^()\\\\]|\\\\.)*\\)`` silently declines a
    paren-bearing body, leaving the pattern unnarrowed. Returns
    ``(filled, all_pinned)``; ``filled`` is None on a malformed (unbalanced)
    group, and ``all_pinned`` is False if any named group was left unresolved.
    Shared by `RegexData.substitute` and target selection's membership check."""
    out: list[str] = []
    i, n = 0, len(pattern)
    all_pinned = True
    while i < n:
        head = re.match(r"\(\?P<([A-Za-z_]\w*)>", pattern[i:])
        if head:
            name = head.group(1)
            depth, k, in_class = 1, i + head.end(), False
            while k < n and depth:
                c = pattern[k]
                if c == "\\":
                    k += 2
                    continue
                if in_class:
                    in_class = c != "]"
                    k += 1
                    continue
                if c == "[":
                    in_class, k = True, k + 1
                    if k < n and pattern[k] == "^":  # negated class
                        k += 1
                    if k < n and pattern[k] == "]":  # a leading ] is literal
                        in_class, k = True, k + 1
                    continue
                depth += 1 if c == "(" else -1 if c == ")" else 0
                k += 1
            if depth != 0:
                return None, False  # unbalanced — do not guess
            if name in values:
                out.append(re.escape(values[name]))
            else:
                all_pinned = False
                out.append(pattern[i:k])
            i = k
            continue
        if pattern[i] == "\\":
            out.append(pattern[i : i + 2])
            i += 2
            continue
        out.append(pattern[i])
        i += 1
    return "".join(out), all_pinned


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

    def substitute(self, bindings: dict[str, str]) -> RegexData:
        """Narrow the regex under expansion: replace each named group whose
        name is bound with the escaped value, e.g.
        ``(?P<video>[^/]+)/(?P<scene>[0-9]\\S*)`` with video=intro becomes
        ``intro/(?P<scene>[0-9]\\S*)``. Group bodies *may* contain nested
        groups — a bound ``{video}`` narrows a collection whose video segment
        is a lookahead alternation just as well as a plain ``[^/]+``. Names not
        bound here are left as groups; names not present as groups are ignored
        (bindings are commonly a superset)."""
        filled, _ = _fill_named_groups(self.regex.pattern, bindings)
        if filled is None:
            # The scanner only reports None on an unbalanced group, which
            # `re.compile` in __init__ already rejects — so it is unreachable
            # for a constructed RegexData. Degrade to unchanged (identically in
            # the Rust engine) rather than diverge the two engines here.
            return self
        return RegexData(filled) if filled != self.regex.pattern else self

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


class AssetData(DataAtom):
    """A file brix itself ships (a compiler-worker source, a recorder script),
    named by a stable logical path (POSIX, brix-relative) rather than a machine
    path. The engine owns the bytes and resolves the name to a real path at run
    time; the fingerprint is the name + the file's content, never the resolved
    path — so it is identical across machines and frontends (docs/boundary-schema.md).
    Skin-internal: skins reference their own shipped helpers with it; it is not a
    `data.*` constructor an author calls."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"AssetData({self.name!r})"


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
