"""Internal path-pattern representation.

User-facing ``file``/``glob`` declarations lower into `PathPattern`.
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
from fnmatch import fnmatchcase
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
    """A path template such as ``src/{name}.cpp`` or a concrete path.

    ``exclude`` (fnmatch patterns — ``*``, ``?``, ``[...]`` — matched against
    the workspace-relative POSIX path; ``*`` crosses ``/``) narrows
    *membership* only: `find` drops matching files. Matching (`match`),
    substitution, and overlap checks ignore excludes — an exclude can only
    shrink a set, so ignoring it keeps edge inference conservative
    (docs/content-key.md).
    """

    def __init__(self, template: str, exclude: tuple[str, ...] = ()) -> None:
        if not template:
            raise PatternError("empty path pattern")
        self.template = template
        self.exclude = tuple(exclude)
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
            if self._excluded(self.template):
                return []
            return [(self.template, {})] if (root / self.template).is_file() else []
        base = root / self.literal_prefix_dir if self.literal_prefix_dir else root
        if not base.is_dir():
            return []
        results: list[tuple[str, dict[str, str]]] = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if self._excluded(rel):
                continue
            bindings = self.match(rel)
            if bindings is not None:
                results.append((rel, bindings))
        return results

    def _excluded(self, rel: str) -> bool:
        return any(fnmatchcase(rel, pat) for pat in self.exclude)

    def __repr__(self) -> str:
        return f"PathPattern({self.template!r})"


def overlaps(a: PathPattern, b: PathPattern, shared: frozenset[str] = frozenset()) -> bool:
    """Conservatively decide whether two patterns can name the same path.

    Concrete-vs-concrete and concrete-vs-pattern are exact. Pattern-vs-pattern
    uses a probe substitution: each side's bindings are replaced with a token
    that matches a single path segment, then tested against the other side.
    This can under-approximate exotic overlaps (mixed literal/binding segments
    that interleave), which is acceptable for the prototype.

    ``shared`` names bindings that hold the *same value* on both sides — the
    planner passes the element names of expansions that iterate the same
    collection (`combine {video}` and `finalize {video}` over one `videos`
    glob). A shared binding consumes the identical span in any path both
    patterns could name, so the matching prefix (identical literals + shared
    bindings) is stripped and the ordinary conservative check runs on the
    tails: ``out/{video}.mpg`` vs ``out/{video}/{scene}.mpg`` can then never
    collide (`.mpg` vs `/{scene}.mpg`), while ``out/{video}.raw.mpg`` vs
    itself still does. Stripping only ever *removes* provably-impossible
    overlaps; anything not aligned falls through to the conservative check.
    """
    if a.is_concrete and b.is_concrete:
        return a.template == b.template
    if a.is_concrete:
        return b.match(a.template) is not None
    if b.is_concrete:
        return a.match(b.template) is not None
    if shared:
        rest_a, rest_b = _strip_shared_prefix(a.template, b.template, shared)
        if (rest_a, rest_b) != (a.template, b.template):
            if not rest_a or not rest_b:
                # One side fully consumed: the paths are equal iff both are.
                return rest_a == rest_b
            return overlaps(PathPattern(rest_a), PathPattern(rest_b))
    probe_a = a.substitute({name: _PROBE for name in a.binding_names})
    probe_b = b.substitute({name: _PROBE for name in b.binding_names})
    return b.match(probe_a) is not None or a.match(probe_b) is not None


def _tokens(template: str) -> list[tuple[str, ...]]:
    """A template as ``("lit", text)`` / ``("bind", name, "{name...}")``."""
    out: list[tuple[str, ...]] = []
    pos = 0
    for m in _BINDING_RE.finditer(template):
        if m.start() > pos:
            out.append(("lit", template[pos : m.start()]))
        out.append(("bind", m.group(1), m.group(0)))
        pos = m.end()
    if pos < len(template):
        out.append(("lit", template[pos:]))
    return out


def _strip_shared_prefix(a: str, b: str, shared: frozenset[str]) -> tuple[str, str]:
    """Strip the longest common prefix where both templates carry identical
    literal text or the same shared binding. In any path both patterns could
    name under shared-binding equality, that prefix occupies the same span on
    both sides, so overlap of the full patterns reduces to overlap of the
    tails. Stops at the first divergence (differing literals, unshared or
    differently-named bindings) and returns the remaining templates."""
    ta, tb = _tokens(a), _tokens(b)
    i = j = 0
    while i < len(ta) and j < len(tb):
        ka, kb = ta[i], tb[j]
        if ka[0] == "lit" and kb[0] == "lit":
            la, lb = ka[1], kb[1]
            n = 0
            while n < len(la) and n < len(lb) and la[n] == lb[n]:
                n += 1
            if n < len(la) and n < len(lb):
                # Diverging characters: the tails start with different
                # literals, which the ordinary check will reject.
                ta[i] = ("lit", la[n:])
                tb[j] = ("lit", lb[n:])
                break
            if n == len(la):
                i += 1
            else:
                ta[i] = ("lit", la[n:])
            if n == len(lb):
                j += 1
            else:
                tb[j] = ("lit", lb[n:])
        elif ka[0] == "bind" and kb[0] == "bind" and ka[1] == kb[1] and ka[1] in shared:
            # The same value on both sides (whatever the `:*` constraint says;
            # constraints only narrow which values exist, never the span one
            # occupies), so the same span: strip and keep aligning.
            i += 1
            j += 1
        else:
            break
    rebuild = lambda tokens: "".join(t[2] if t[0] == "bind" else t[1] for t in tokens)  # noqa: E731
    return rebuild(ta[i:]), rebuild(tb[j:])
