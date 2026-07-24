"""The C++ provider for test-impact selection (design:
docs/test-impact-selection.md).

Fills the `testimpact.Provider` seam at C++'s sound static binding unit. For a
test, that unit is the **test executable**, not the individual ``TEST()`` case:
a gtest binary links a fixed set of object files, and every case in it links the
same objects, so nothing static can attribute one case to a subset of them.
Finer per-case selection needs runtime coverage (gcov) — a later increment. The
executable is the finest granularity a *sound* static analysis can offer, and
overshooting it (running a whole binary when one case was affected) is the safe
direction.

Everything here re-points a graph brix already builds; there is no new analysis:

* each object's ``-MMD`` **depfile** (``<obj>.d``, a Make rule brix already emits
  and the incremental layer already scans) gives ``object -> {source, headers}``.
  Because ``-MMD`` records the full *user*-header set a translation unit pulls in
  (system headers excluded), an edit to any header re-stales exactly the objects
  that include it — transitively.
* the **link graph** (which objects a binary links) gives ``test binary ->
  {objects}``. The caller supplies it, because that is what its build file
  already spells out in the link step.
* an object whose compiled symbols reference ``dlopen``/``dlsym``/``dlmopen``
  binds code **by name at run time** — the C++ analogue of reflection — so it is
  marked *dynamic* and every test linking it always runs. This is read straight
  from the object with ``nm``, the way the Java provider reads bytecode.

The closure of a test binary is therefore its objects plus every source and
header those objects compile from; a change to any of them selects the binary,
and a change to nothing it links leaves it provably unaffected and skipped.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .testimpact import ProviderView, TestUnit, Unit

# Runtime dynamic-linking entry points. An object with an undefined reference to
# any of these can load a shared object by a name computed at run time, so its
# real dependencies are not statically knowable — the reflection analogue.
_DL_SYMBOLS: tuple[str, ...] = ("dlopen", "dlmopen", "dlsym", "dlvsym")


def parse_depfile(text: str) -> tuple[str | None, frozenset[str]]:
    """Parse a Make-format ``-MMD`` depfile into ``(target, prerequisites)``.

    The rule may span lines via ``\\``-continuations: ``obj: a.cpp foo.h \\`` /
    ``  bar.h``. Prerequisites are the source and every user header the compiler
    recorded. Returns ``(None, frozenset())`` for anything that is not a rule."""
    joined = text.replace("\\\n", " ")
    if ":" not in joined:
        return None, frozenset()
    target, _, rest = joined.partition(":")
    # `-MMD` does not emit escaped spaces for ordinary paths; split on whitespace.
    return target.strip(), frozenset(p for p in rest.split() if p)


def _posix(path: str | Path) -> str:
    """A path as a stable POSIX unit string (the identity the core compares)."""
    return PurePosixPath(str(path)).as_posix()


def references_dl(obj: Path, *, nm: str = "nm") -> bool:
    """Whether an object file references a runtime dynamic-linking symbol.

    Reads undefined symbols with ``nm -u``. Fails **safe**: an object we cannot
    scan is treated as dynamic (its tests always run) rather than assumed inert."""
    try:
        proc = subprocess.run(
            [nm, "-u", str(obj)], capture_output=True, text=True
        )
    except OSError:
        return True  # cannot scan -> assume it may load code by name
    if proc.returncode != 0:
        return True
    return any(sym in proc.stdout for sym in _DL_SYMBOLS)


@dataclass
class CppProvider:
    """Builds a `ProviderView` from compiled C++ artifacts.

    ``tests`` maps each test executable's name (the test unit the runner
    subsets) to the object files linked into it — including library objects it
    pulls in, since those carry real dependencies. Object paths are as brix
    writes them (repo-relative); each object's depfile is ``<obj>.d`` and its
    source/headers come from there."""

    tests: Mapping[str, Sequence[str | Path]]
    root: str | Path = "."
    nm: str = "nm"
    depfile_suffix: str = ".d"

    def view(self) -> ProviderView:
        root = Path(self.root)
        code_deps: dict[Unit, frozenset[Unit]] = {}
        test_edges: dict[TestUnit, frozenset[Unit]] = {}
        dynamic: set[Unit] = set()
        uncovered: set[Unit] = set()

        for test, objects in self.tests.items():
            units: set[Unit] = set()
            for obj in objects:
                unit = _posix(obj)
                units.add(unit)
                if unit in code_deps:
                    continue  # shared object file: analyze once
                obj_path = root / obj
                depfile = Path(str(obj_path) + self.depfile_suffix)
                try:
                    _, prereqs = parse_depfile(depfile.read_text())
                except OSError:
                    # No depfile -> we cannot know what this object compiles
                    # from -> the graph is incomplete -> force a full run.
                    code_deps[unit] = frozenset()
                    uncovered.add(unit)
                else:
                    code_deps[unit] = frozenset(_posix(p) for p in prereqs)
                if references_dl(obj_path, nm=self.nm):
                    dynamic.add(unit)
            test_edges[test] = frozenset(units)

        return ProviderView(
            code_deps=code_deps,
            test_edges=test_edges,
            dynamic_units=frozenset(dynamic),
            uncovered=frozenset(uncovered),
            binding_unit="object file",
        )

    def select_args(self, tests: Iterable[TestUnit]) -> list[str]:
        """A CTest ``-R`` filter running exactly ``tests`` (test names anchored,
        OR-joined). Empty when nothing is selected — the caller runs no tests."""
        names = sorted(tests)
        if not names:
            return []
        pattern = "^(" + "|".join(re.escape(n) for n in names) + ")$"
        return ["-R", pattern]
