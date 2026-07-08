"""Internal path-pattern representation.

User-facing ``file``/``glob`` declarations lower into :class:`PathPattern`.
A pattern is a workspace-relative path template with ``{name}`` placeholders.
A bare ``{name}`` is **recursive** — it matches across ``/`` (like make's ``%``
in ``obj/%.o: src/%.cpp``), so ``build/{rel}.class`` matches both
``build/App.class`` (``rel=App``) and ``build/com/foo/Bar.class``
(``rel=com/foo/Bar``). Write ``{name:*}`` to restrict a placeholder to a single
path segment; ``{name:**}`` is the explicit form of the recursive default. Globs
carry only these two flavors — for anything richer (character classes,
quantifiers) use ``data.regex`` and write a full regex.

This is the simple prototype implementation: correctness and clear semantics
over speed. The indexing structures described in the design doc (prefix tries,
matcher caches) belong to the eventual Rust engine.
"""

from __future__ import annotations

import re
from pathlib import Path

# Group 1 is the name; group 2 is the optional constraint (`*` = one segment,
# `**` = recursive). A bare `{name}` (no constraint) is recursive by default.
_BINDING_RE = re.compile(r"\{(\w+)(?::(\*\*|\*))?\}")

# Probe token used for conservative pattern-vs-pattern overlap checks.
# Contains no "/" so it matches a single-segment binding.
_PROBE = "\x00brix\x00"


class PatternError(Exception):
    pass


class PathPattern:
    """A path template such as ``src/{name}.cpp`` or a concrete path."""

    def __init__(self, template: str) -> None:
        if not template:
            raise PatternError("empty path pattern")
        self.template = template
        self.binding_names: tuple[str, ...] = tuple(
            m.group(1) for m in _BINDING_RE.finditer(template)
        )
        self._regex = self._compile(template)

    @property
    def is_concrete(self) -> bool:
        return not self.binding_names

    @staticmethod
    def _compile(template: str) -> re.Pattern[str]:
        parts: list[str] = ["^"]
        pos = 0
        seen: set[str] = set()
        for m in _BINDING_RE.finditer(template):
            parts.append(re.escape(template[pos : m.start()]))
            name = m.group(1)
            recursive = m.group(2) != "*"  # bare or `**` => recursive; `*` => one segment
            if name in seen:
                # Repeated binding must match the same text.
                parts.append(f"(?P={name})")
            else:
                seen.add(name)
                # `{name}`/`{name:**}` cross / (.+); `{name:*}` is one segment.
                parts.append(f"(?P<{name}>{'.+' if recursive else '[^/]+'})")
            pos = m.end()
        parts.append(re.escape(template[pos:]))
        parts.append("$")
        return re.compile("".join(parts))

    def match(self, path: str) -> dict[str, str] | None:
        """Return extracted bindings if ``path`` matches, else ``None``."""
        m = self._regex.match(path)
        if m is None:
            return None
        return dict(m.groupdict())

    def substitute(self, bindings: dict[str, str]) -> str:
        """Replace known bindings; unknown placeholders are left intact."""

        def repl(m: re.Match[str]) -> str:
            return bindings.get(m.group(1), m.group(0))

        return _BINDING_RE.sub(repl, self.template)

    @property
    def literal_prefix_dir(self) -> str:
        """Deepest directory that is literal (no bindings). May be ''."""
        prefix = self.template.split("{", 1)[0]
        if "/" not in prefix:
            return ""
        return prefix.rsplit("/", 1)[0]

    def find(self, root: Path) -> list[tuple[str, dict[str, str]]]:
        """Realize this pattern against the filesystem.

        Returns sorted ``(workspace-relative path, bindings)`` pairs.
        """
        if self.is_concrete:
            return [(self.template, {})] if (root / self.template).is_file() else []
        base = root / self.literal_prefix_dir if self.literal_prefix_dir else root
        if not base.is_dir():
            return []
        results: list[tuple[str, dict[str, str]]] = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            bindings = self.match(rel)
            if bindings is not None:
                results.append((rel, bindings))
        return results

    def __repr__(self) -> str:
        return f"PathPattern({self.template!r})"


def overlaps(a: PathPattern, b: PathPattern) -> bool:
    """Conservatively decide whether two patterns can name the same path.

    Concrete-vs-concrete and concrete-vs-pattern are exact. Pattern-vs-pattern
    uses a probe substitution: each side's bindings are replaced with a token
    that matches a single path segment, then tested against the other side.
    This can under-approximate exotic overlaps (mixed literal/binding segments
    that interleave), which is acceptable for the prototype.
    """
    if a.is_concrete and b.is_concrete:
        return a.template == b.template
    if a.is_concrete:
        return b.match(a.template) is not None
    if b.is_concrete:
        return a.match(b.template) is not None
    probe_a = a.substitute({name: _PROBE for name in a.binding_names})
    probe_b = b.substitute({name: _PROBE for name in b.binding_names})
    return b.match(probe_a) is not None or a.match(probe_b) is not None
