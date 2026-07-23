"""Test-impact selection — the language-agnostic core (design:
docs/test-impact-selection.md).

Given a change set and a language *provider*'s already-discovered dependency
graphs, decide the set of test units to run that is **provably equivalent** to
running the full suite — or fail open (run everything) whenever equivalence
cannot be proven. This is the tests analogue of `affected.py`'s package
selection, and inherits its invariant verbatim:

    over-selection is a wasted test run (harmless);
    under-selection is a silently-missed regression (fatal).

**Correctness is not a knob.** There is no speed-vs-safety dial in the default;
the only variable is how *often* we can prove a safe subset (how much speedup).
Every source of uncertainty is a named, enumerable **fail-open trigger**; if any
fires we run all. Keeping the trigger set first-class is what would make an
*optional* future speed/correctness dial a controlled relaxation (disable named
triggers) rather than a rewrite — see the design note. Day-one policy is simply
"every trigger active, no dial exposed".

The core is language-free: it operates on a `ProviderView` (a pure snapshot a
real provider builds from artifacts brix already produces — `jvm-abi` refs, C++
`-MMD` + link graph, `tsc --listFiles`, cargo — and the tests build by hand).
The soundness model is uniform across providers: coarse binding-closure
selection over-selects safely; a unit that performs runtime dynamic loading
(`dlopen`/reflection/dynamic-`import`) makes any test reaching it unprovable, so
that test is always run (a local fail-open); the only *sound* recovery is
dynamic coverage, a later increment.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

# Opaque identities. A "unit" is one binding-closure element (a class /
# object-file / module / crate, per provider); a "test unit" names a runnable
# test grouping the provider can subset the runner by.
Unit = str
TestUnit = str


@dataclass(frozen=True)
class ProviderView:
    """A pure snapshot of what a provider discovered for one selection.

    Everything the core needs, as plain data, so it stays language-free and
    trivially testable:

    * ``code_deps`` — ``unit -> units it (statically) depends on``.
    * ``test_edges`` — ``test unit -> code units it (statically) references``.
    * ``dynamic_units`` — units whose code performs runtime dynamic loading, so
      their real dependencies are not statically knowable.
    * ``uncovered`` — units the provider could not analyze (a coverage gap in
      the graph); non-empty forces a full run.
    * ``binding_unit`` — a label for diagnostics (e.g. ``"class"``, ``"object
      file"``).
    """

    code_deps: Mapping[Unit, frozenset[Unit]]
    test_edges: Mapping[TestUnit, frozenset[Unit]]
    dynamic_units: frozenset[Unit] = frozenset()
    uncovered: frozenset[Unit] = frozenset()
    binding_unit: str = "unit"

    @property
    def all_tests(self) -> frozenset[TestUnit]:
        return frozenset(self.test_edges)


class Provider(Protocol):
    """The per-language seam. A concrete provider (Java `jvm-abi`, C++, …) builds
    a `ProviderView` from graphs brix already discovers and turns a chosen test
    set into runner arguments. The core needs only `view()` to *decide*; the
    subsetter is used later, at run time."""

    def view(self) -> ProviderView: ...

    def select_args(self, tests: Iterable[TestUnit]) -> list[str]:
        """Runner flags to run exactly ``tests`` (e.g. JUnit ``--select-class``,
        CTest ``-R``). Only meaningful for an actual subset run."""
        ...


# --- the enumerable fail-open trigger set ------------------------------------
#
# Each trigger is a named predicate over the selection inputs. The policy is the
# active set (all, by default). If any fires, the whole suite runs. This is the
# first-class set a future optional dial would relax.


@dataclass(frozen=True)
class SelectionInput:
    view: ProviderView
    changed: frozenset[Unit]
    known: frozenset[Unit]  # units/tests the last recorded selection knew about
    prior_failed: bool = False  # the previous run failed -> rerun until green


@dataclass(frozen=True)
class Trigger:
    name: str
    fires: Callable[[SelectionInput], bool]
    describe: Callable[[SelectionInput], str]


def _new_or_renamed(inp: SelectionInput) -> bool:
    # A changed unit the prior index never knew: new or renamed code, whose
    # edges we cannot trust (a rename orphans the old name's edges; new code has
    # none). New tests arrive the same way and must run at least once.
    return bool(inp.changed - inp.known)


def _incomplete_graph(inp: SelectionInput) -> bool:
    # The provider could not analyze some unit -> the graph is not a complete
    # picture, so a subset cannot be proven.
    return bool(inp.view.uncovered)


def _prior_failure(inp: SelectionInput) -> bool:
    # A failed run has unfinished work regardless of what changed since; rerun
    # everything until green (mirrors affected.py's recovery stance).
    return inp.prior_failed


DEFAULT_TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        "new-or-renamed-unit",
        _new_or_renamed,
        lambda i: f"changed units not in the last index: {sorted(i.changed - i.known)}",
    ),
    Trigger(
        "incomplete-graph",
        _incomplete_graph,
        lambda i: f"provider could not analyze: {sorted(i.view.uncovered)}",
    ),
    Trigger(
        "prior-failure",
        _prior_failure,
        lambda i: "the previous run failed; rerunning the full suite until green",
    ),
)


# --- the result --------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """What to run, and why. ``tests`` is always the concrete set to run — the
    full suite when ``run_all``. ``reasons`` records which fail-open triggers
    fired (empty for a proven subset); ``always_run`` records the
    dynamic-unsafe tests pulled in unconditionally — both for observability
    (e.g. a future `status`/`--json` explanation), never for correctness."""

    tests: frozenset[TestUnit]
    run_all: bool
    reasons: tuple[str, ...] = ()
    always_run: frozenset[TestUnit] = field(default_factory=frozenset)


def _reachable(starts: Iterable[Unit], deps: Mapping[Unit, frozenset[Unit]]) -> set[Unit]:
    """Transitive closure over ``deps`` from ``starts`` (inclusive)."""
    seen: set[Unit] = set()
    stack = list(starts)
    while stack:
        u = stack.pop()
        if u in seen:
            continue
        seen.add(u)
        stack.extend(deps.get(u, frozenset()))
    return seen


def select_tests(
    inp: SelectionInput,
    *,
    triggers: tuple[Trigger, ...] = DEFAULT_TRIGGERS,
) -> Selection:
    """The core decision. Returns the provably-equivalent set of tests to run.

    Whole-run fail-open first: if any active trigger fires, run everything.
    Otherwise, per test, take its static code closure (its references plus their
    transitive dependencies) and:

    * if that closure touches a **dynamic-loading** unit, the test's real
      dependencies are unknowable — run it unconditionally (local fail-open);
    * else run it iff the closure intersects the **changed** set — i.e. the test
      exercises code that (transitively) changed.

    A test whose closure touches neither is provably unaffected and skipped.
    """
    view = inp.view
    fired = tuple(t.describe(inp) for t in triggers if t.fires(inp))
    if fired:
        return Selection(tests=view.all_tests, run_all=True, reasons=fired)

    selected: set[TestUnit] = set()
    always: set[TestUnit] = set()
    for test, edges in view.test_edges.items():
        closure = _reachable(edges, view.code_deps)
        if closure & view.dynamic_units:
            selected.add(test)
            always.add(test)
        elif closure & inp.changed:
            selected.add(test)
    return Selection(
        tests=frozenset(selected),
        run_all=False,
        always_run=frozenset(always),
    )
