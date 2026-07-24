#!/usr/bin/env python3
"""Dogfood the Java test-impact provider on real JUnit 5 bytecode.

The unit tests and oracles prove the provider *correct* on small worlds. This
asks a different question — does it hold up on real code, and is the payoff
real? — by pointing `testimpact_java`'s actual machinery (`scan_classes` +
`class_refs`, the constant-pool graph) at JUnit 5's own compiled modules.

Two measurements:

1. **Scale + robustness + the fail-open surface.** How fast it parses real
   class files, whether any are unreadable, and what fraction of classes are
   *dynamic* (reflection) — the surface that makes a reaching test always run.

2. **Blast-radius.** For each class, how many classes transitively depend on it
   — i.e. if you edit it, what share of the codebase could be affected. This is
   a sound proxy for the test-selection payoff: a test of class Y runs iff Y's
   closure reaches the edit iff Y transitively depends on the edited class. A
   small median blast-radius means a full-graph suite skips most tests.

Caveat, stated plainly: this uses JUnit 5's *main* modules as the code graph
(no compiled test suite is wired here), so it measures the provider and a
dependency-blast-radius proxy — not an end-to-end run of a real suite. It is a
provider-scale dogfood, not a selection-ratio benchmark.

Build the JUnit 5 main jars first: `python brix_build.py build`. Classes are
read from `<junit5>/build/classes/*`.
"""

import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix.testimpact_java import scan_classes  # noqa: E402

try:
    from brix_build import ROOT as JUNIT5_ROOT  # the checkout the example builds
except Exception:  # pragma: no cover - fallback if run oddly
    JUNIT5_ROOT = Path.home() / "Projects/brix_test/junit5"

CLASSES = Path(JUNIT5_ROOT) / "build/classes"


def main() -> int:
    if not CLASSES.is_dir():
        sys.exit(f"no compiled classes at {CLASSES} — run `python brix_build.py build` first")
    roots = sorted(p for p in CLASSES.iterdir() if p.is_dir())

    t0 = time.time()
    scan = scan_classes(roots)
    scan_ms = (time.time() - t0) * 1000

    classes = scan.classes
    universe = set(classes)
    n = len(universe)
    dynamic = [u for u, r in classes.items() if r.dynamic]

    print(f"modules            : {len(roots)}")
    print(f"classes scanned    : {n}")
    print(f"scan time          : {scan_ms:.0f} ms  ({n / (scan_ms / 1000):.0f} classes/s)")
    print(f"unreadable         : {len(scan.unreadable)}")
    print(
        f"dynamic (reflective): {len(dynamic)}  = {100 * len(dynamic) / n:.1f}%  "
        f"(a test reaching one of these always runs)"
    )

    # Intra-project dependency edges (references + supertypes), then reverse
    # reachability: who depends on each class.
    deps = {
        u: (r.refs & universe | (r.supers & universe)) - {u}
        for u, r in classes.items()
    }
    rev: dict[str, set[str]] = {u: set() for u in universe}
    for u, ds in deps.items():
        for d in ds:
            rev[d].add(u)

    def dependents(x: str) -> int:
        seen, stack = set(), [x]
        while stack:
            c = stack.pop()
            for p in rev[c]:
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return len(seen)

    radii = sorted(100 * dependents(x) / n for x in universe)
    print()
    print("edit blast-radius — share of classes that (transitively) depend on a given class:")
    print(
        f"  median {statistics.median(radii):.1f}%  |  mean {statistics.mean(radii):.1f}%  "
        f"|  p75 {radii[int(0.75 * n)]:.1f}%  |  p90 {radii[int(0.90 * n)]:.1f}%  "
        f"|  max {max(radii):.1f}%"
    )
    edges = [(0, 1), (1, 5), (5, 10), (10, 25), (25, 50), (50, 101)]
    for lo, hi in edges:
        c = sum(1 for s in radii if lo <= s < hi)
        bar = "#" * (c * 40 // n)
        label = f"<{hi}%" if lo == 0 else f"{lo}-{hi}%" if hi <= 50 else f">{lo}%"
        print(f"  {label:>7}: {bar} {c} ({100 * c / n:.0f}%)")
    print()
    print(
        f"a median edit is depended on by ~{statistics.median(radii):.0f}% of the code, so a "
        f"full-graph suite would skip roughly the other ~{100 - statistics.median(radii):.0f}%."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
