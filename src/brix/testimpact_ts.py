"""The TypeScript/JavaScript provider for test-impact selection (design:
docs/test-impact-selection.md).

Fills the `testimpact.Provider` seam at TS's binding unit: the **module (file)**.
The graph is the static `import` graph — explicit in the source, so it is read
directly (the same shape as C++'s file-level `#include` graph):

* ``code_deps`` — ``file -> the workspace files it imports`` (relative specifiers
  resolved to real files; bare specifiers like ``react`` are external, treated as
  inert, exactly as the Java provider treats JDK platform classes).
* ``test_edges`` — a test file (``*.test.ts`` / ``*.spec.ts`` …) -> the files it
  imports. A test's closure is those imports plus everything they transitively
  import; a change anywhere in that closure selects the test.
* **dynamic** — a file with a ``import(<expr>)`` or ``require(<expr>)`` whose
  specifier is *computed* (not a string literal) loads a module by a name known
  only at run time — the reflection analogue — so it is marked dynamic and every
  test reaching it always runs. A dynamic import with a **literal** specifier is
  statically resolvable and is treated as an ordinary edge.
* **uncovered** — a *relative* import that resolves to no known workspace file
  (an extension/alias we cannot follow) is a hole in the graph, so it forces a
  full run rather than a guess.

Resolution is deliberately simple (relative paths + the common extension/index
fallbacks); tsconfig ``paths``/``baseUrl`` aliases are not followed yet and would
surface as ``uncovered`` (fail open), never as a missed edge.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .testimpact import ProviderView, TestUnit, Unit, _reachable

# Static imports that create a real edge: `import ... from 'x'`, a side-effect
# `import 'x'`, and `export ... from 'x'`. The specifier is always a literal.
_FROM = re.compile(r"""(?:^|[\s;])(?:import|export)\b[^'"()]*?\bfrom\s*['"]([^'"]+)['"]""")
_SIDE_EFFECT = re.compile(r"""(?:^|[\s;])import\s*['"]([^'"]+)['"]""")
# Dynamic import()/require(): a *string-literal* argument is a resolvable edge; a
# non-literal argument (a variable, a template) is a runtime-computed load.
_CALL_LITERAL = re.compile(r"""\b(?:import|require)\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_CALL_COMPUTED = re.compile(r"""\b(?:import|require)\s*\(\s*(?!['"])""")

_TS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
                  ".test.js", ".test.jsx", ".spec.js", ".spec.jsx")


def parse_imports(text: str) -> tuple[frozenset[str], bool]:
    """Return ``(specifiers, has_computed_dynamic)`` for one source file:
    every statically-named import/require specifier, and whether the file
    performs a dynamic ``import()``/``require()`` on a computed specifier."""
    specs: set[str] = set()
    for pat in (_FROM, _SIDE_EFFECT, _CALL_LITERAL):
        specs.update(pat.findall(text))
    return frozenset(specs), bool(_CALL_COMPUTED.search(text))


def _is_relative(spec: str) -> bool:
    return spec.startswith(".") or spec.startswith("/")


def resolve_import(spec: str, from_file: str, known: frozenset[str]) -> str | None:
    """Resolve a *relative* specifier to a known workspace file, or None.

    Returns None for bare specifiers (external packages — not workspace units)
    *and* for relative specifiers that resolve to no known file; the caller
    distinguishes them (bare = ignore, unresolved-relative = coverage gap)."""
    if not _is_relative(spec):
        return None
    base = PurePosixPath(from_file).parent
    target = (base / spec) if not spec.startswith("/") else PurePosixPath(spec.lstrip("/"))
    # Normalize `..` / `.` segments.
    parts: list[str] = []
    for part in target.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    stem = "/".join(parts)
    candidates = [stem, *(stem + ext for ext in _TS_EXTS),
                  *(f"{stem}/index{ext}" for ext in _TS_EXTS)]
    for cand in candidates:
        if cand in known:
            return cand
    return None


@dataclass
class TsProvider:
    """Builds a `ProviderView` from a TypeScript workspace's import graph.

    ``files`` are the workspace-relative source files to analyze (``.ts``/``.tsx``
    and friends). A file is a *test* unit if its name ends in one of
    ``test_suffixes``. Paths are POSIX and match what the change detector reports."""

    files: Sequence[str]
    root: str | Path = "."
    test_suffixes: tuple[str, ...] = _TEST_SUFFIXES

    def _is_test(self, unit: str) -> bool:
        return unit.endswith(self.test_suffixes)

    def view(self) -> ProviderView:
        root = Path(self.root)
        known = frozenset(_posix(f) for f in self.files)
        code_deps: dict[Unit, frozenset[Unit]] = {}
        test_edges: dict[TestUnit, frozenset[Unit]] = {}
        dynamic: set[Unit] = set()
        uncovered: set[Unit] = set()

        for f in self.files:
            unit = _posix(f)
            try:
                text = (root / f).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                code_deps[unit] = frozenset()
                uncovered.add(unit)  # unreadable -> incomplete graph -> full run
                continue
            specs, has_dynamic = parse_imports(text)
            edges: set[Unit] = set()
            for spec in specs:
                resolved = resolve_import(spec, unit, known)
                if resolved is not None:
                    edges.add(resolved)
                elif _is_relative(spec):
                    uncovered.add(unit)  # relative import we could not follow
                # bare specifier -> external package -> inert, no edge
            if has_dynamic:
                dynamic.add(unit)
            code_deps[unit] = frozenset(edges)
            if self._is_test(unit):
                test_edges[unit] = frozenset(edges)

        return ProviderView(
            code_deps=code_deps,
            test_edges=test_edges,
            dynamic_units=frozenset(dynamic),
            uncovered=frozenset(uncovered),
            binding_unit="module",
        )

    def select_args(self, tests: Iterable[TestUnit]) -> list[str]:
        """`vitest run <files>` arguments running exactly ``tests``."""
        names = sorted(tests)
        return ["run", *names] if names else []

    def test_input_sets(self) -> dict[TestUnit, frozenset[Unit]]:
        """For each test file, the workspace files that make up its byte-lens
        footprint — the test plus its transitive import closure.

        This is what wires per-file selection into a real build with **no**
        eval-time selector: declare each test's `RunTestAction` over exactly
        these files, and the incremental engine re-runs a test only when a file
        in its closure changes. A test whose closure reaches a computed
        `import()`/`require()` or an unresolved import gets the **whole** source
        set instead — the run-on-any-change fail-open, the footprint form of the
        core's dynamic/uncovered triggers."""
        view = self.view()
        all_sources = frozenset(view.code_deps)
        fail_open = view.dynamic_units | view.uncovered
        out: dict[TestUnit, frozenset[Unit]] = {}
        for test, edges in view.test_edges.items():
            closure = _reachable(edges, view.code_deps) | {test}
            out[test] = all_sources if (closure & fail_open) else frozenset(closure)
        return out


def _posix(path: str | Path) -> str:
    return PurePosixPath(str(path)).as_posix()
