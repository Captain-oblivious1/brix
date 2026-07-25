#!/usr/bin/env python3
"""How much does test-impact selection actually save on real jackson-databind?

`tested_lib` shows selection narrowing to a single test class. This script is
the honest counterweight at real scale: it measures the blast radius of a
class-level edit across Jackson's *actual* 668-class test suite and reports what
selection buys here — which, it turns out, is almost nothing, and correctly so.

databind is a tightly-coupled monolith: `ObjectMapper` statically references
most of the module, and nearly every test constructs an `ObjectMapper`, so a
typical class edit's static closure reaches ~the whole suite. Selection is still
100% *correct* (it never under-selects — that invariant is spot-checked below);
it simply has little to skip. The payoff of class-level selection is a function
of the target's coupling: a decoupled framework like JUnit 5 skips ~96% of its
suite on a median edit; a monolith like databind skips ~0%. That contrast is the
case for per-method (runtime-coverage) selection, which stays selective even
inside a coupled module because it observes what each test *runs*, not what it
*could* reach.

Run `python databind_testimpact.py build` first (this reads its compiled
classes). Then: `python characterize_databind_selection.py`.
"""

import copy
import importlib.util
import random
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

_spec = importlib.util.spec_from_file_location("dbti", HERE / "databind_testimpact.py")
assert _spec and _spec.loader
dbti = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dbti
_spec.loader.exec_module(dbti)

from brix.testimpact import (  # noqa: E402
    SelectionInput,
    _vocabulary,
    known_units,
    record_index,
    select_tests,
)

ROOT = dbti.ROOT
TEST_SRC_ROOT = ROOT / dbti.TEST_ROOT


def _dotted(unit: str) -> str:
    return unit.replace("/", ".")


def _simple(unit: str) -> str:
    return unit.rsplit("/", 1)[-1]


def _static_only(view):
    """A copy of the view with reflection/config escapes cleared, to isolate the
    *intrinsic* static coupling from the reflection fail-open."""
    v = copy.copy(view)
    object.__setattr__(v, "dynamic_units", frozenset())
    object.__setattr__(v, "name_addressed", frozenset())
    return v


def _blast(view, known, changed_unit) -> int:
    sel = select_tests(SelectionInput(view, frozenset([changed_unit]), known, False))
    return len(view.all_tests) if sel.run_all else len(sel.tests)


def _source_referencers(simple_name: str) -> set[str]:
    """Test classes whose SOURCE names ``simple_name`` — an independent (grep,
    not provider) lower bound on who must be selected when it changes."""
    try:
        out = subprocess.run(
            ["grep", "-rlw", simple_name, str(TEST_SRC_ROOT), "--include=*.java"],
            capture_output=True,
            text=True,
        ).stdout
    except OSError:
        return set()
    refs = set()
    for line in out.splitlines():
        p = Path(line)
        rel = p.relative_to(TEST_SRC_ROOT).with_suffix("")
        refs.add(rel.as_posix())
    return refs


def main() -> None:
    provider = dbti.make_provider()
    view = provider.view()
    if not view.all_tests:
        sys.exit(
            "No compiled test classes found. Run:\n"
            "  python examples/jackson/databind_testimpact.py build"
        )
    total = len(view.all_tests)
    known = known_units(record_index(view))

    # 1. The reflection floor: how many tests are selected even when NOTHING
    #    changed. Every such test's static closure reaches a reflection-using
    #    unit (ObjectMapper & friends), so it must always run.
    always = len(select_tests(SelectionInput(view, frozenset(), known, False)).tests)
    print(f"Real jackson-databind suite: {total} test classes, "
          f"{len(view.dynamic_units)} reflection-using (dynamic) units.")
    print(f"Reflection floor (tests selected with an EMPTY change set): "
          f"{always}/{total} = {always / total * 100:.0f}% of the suite.")
    print("  => with reflection modeled for safety, selection skips ~nothing here.\n")

    # 2. Peel reflection away to expose the *intrinsic* static coupling: even if
    #    we tracked reflection perfectly, how selective could class-level edits
    #    be? Sample top-level databind main classes; measure static blast radius.
    vs = _static_only(view)
    knowns = known_units(record_index(vs))
    mains = sorted(
        u for u in _vocabulary(view)
        if u.startswith("com/fasterxml/jackson/databind/")
        and "$" not in u and "Test" not in u
    )
    random.seed(7)
    sample = random.sample(mains, min(80, len(mains)))
    radii = sorted(_blast(vs, knowns, c) for c in sample)
    frac = [r / total for r in radii]
    buckets = {"<5%": 0, "5–25%": 0, "25–75%": 0, ">75%": 0}
    for f in frac:
        key = "<5%" if f < 0.05 else "5–25%" if f < 0.25 else "25–75%" if f < 0.75 else ">75%"
        buckets[key] += 1
    print(f"Intrinsic coupling (reflection ignored), blast radius over n={len(sample)} "
          "random class edits:")
    print(f"  median {statistics.median(radii):.0f}/{total} tests "
          f"({statistics.median(frac) * 100:.0f}%); mean {statistics.mean(frac) * 100:.0f}%.")
    print(f"  distribution: {buckets}")
    print("  => even ignoring reflection, the mass of core classes still reach "
          "~the whole suite; only peripheral leaves narrow.\n")

    # 3. The invariant that holds regardless of coupling: NO under-selection.
    #    On the static-only view a peripheral class genuinely narrows, so this is
    #    a meaningful teeth-check: every test whose SOURCE names it must be in the
    #    selection (independent grep lower bound, not the provider's own graph).
    peripheral = min(
        (c for c in sample if 0 < _blast(vs, knowns, c) < total),
        key=lambda c: _blast(vs, knowns, c),
        default=None,
    )
    if peripheral is not None:
        sel = select_tests(SelectionInput(vs, frozenset([peripheral]), knowns, False))
        refs = _source_referencers(_simple(peripheral)) & set(view.all_tests)
        missed = refs - set(sel.tests)
        print(f"Soundness spot-check ({_dotted(peripheral)}, static-only so it narrows):")
        print(f"  selects {len(sel.tests)}/{total} tests; "
              f"source-level referencers {len(refs)}, missed {len(missed)}.")
        if missed:
            raise AssertionError(
                f"UNDER-SELECTION: tests naming {_simple(peripheral)} not selected: "
                f"{sorted(_dotted(u) for u in missed)}"
            )
        print("  no under-selection: every test that names it is selected. OK\n")
    else:
        print("No peripheral class in the sample to spot-check.\n")

    print("Conclusion: on databind, class-level selection is correct but skips "
          "~nothing — reflection forces a 100% floor, and even without it the "
          "coupling keeps the median edit near the whole suite. This is the "
          "motivating evidence for per-method (runtime-coverage) selection, which "
          "stays selective inside a coupled module (see this example's README).")


if __name__ == "__main__":
    main()
