"""Test-impact selection core + the equivalence oracle (design:
docs/test-impact-selection.md).

The oracle is the ground truth the whole feature is held to: across an edit
alphabet, the *selected* run must reach the same verdict as the *full* run —
concretely, **no test that fails in the full run may be skipped**. A synthetic
world models a test whose TRUE runtime dependencies can exceed its
statically-visible ones (the dynamic-loading case), so the oracle can prove the
core's dynamic-unsafe rule is actually necessary and sufficient.
"""

import random

from brix.testimpact import (
    ProviderView,
    Selection,
    SelectionInput,
    select_tests,
    _reachable,
)


# --- basic core behavior -----------------------------------------------------

def _view(code_deps, test_edges, dynamic=(), uncovered=()):
    return ProviderView(
        code_deps={u: frozenset(d) for u, d in code_deps.items()},
        test_edges={t: frozenset(e) for t, e in test_edges.items()},
        dynamic_units=frozenset(dynamic),
        uncovered=frozenset(uncovered),
    )


def _select(view, changed, known=None, prior_failed=False):
    if known is None:  # by default, everything is already known (no new/renamed)
        known = frozenset(view.code_deps) | frozenset(view.test_edges)
    return select_tests(
        SelectionInput(view, frozenset(changed), frozenset(known), prior_failed)
    )


def test_only_tests_reaching_the_change_run():
    # tA -> A, tB -> B, tC -> C; nothing depends on anything. Edit A.
    view = _view(
        code_deps={"A": [], "B": [], "C": []},
        test_edges={"tA": ["A"], "tB": ["B"], "tC": ["C"]},
    )
    sel = _select(view, {"A"})
    assert not sel.run_all
    assert sel.tests == frozenset({"tA"})


def test_transitive_dependents_run():
    # B depends on A; tB -> B. Editing A must still run tB (B transitively uses A).
    view = _view(
        code_deps={"A": [], "B": ["A"], "C": []},
        test_edges={"tA": ["A"], "tB": ["B"], "tC": ["C"]},
    )
    sel = _select(view, {"A"})
    assert sel.tests == frozenset({"tA", "tB"})  # tC provably unaffected


def test_dynamic_unsafe_test_always_runs():
    # tX statically reaches D, which does dynamic loading. Its real deps are
    # unknowable, so tX runs no matter what changed (even an unrelated edit).
    view = _view(
        code_deps={"A": [], "B": [], "D": []},
        test_edges={"tA": ["A"], "tX": ["D"]},
        dynamic=["D"],
    )
    sel = _select(view, {"A"})  # edit A — unrelated to tX statically
    assert "tX" in sel.tests  # pulled in unconditionally
    assert "tX" in sel.always_run
    assert "tA" in sel.tests


def test_new_or_renamed_unit_forces_full_run():
    view = _view(code_deps={"A": []}, test_edges={"tA": ["A"], "tB": ["B"]})
    sel = _select(view, {"Znew"}, known={"A", "tA", "tB"})  # Znew unknown
    assert sel.run_all
    assert sel.tests == view.all_tests
    assert any("new-or-renamed" in r or "not in the last index" in r for r in sel.reasons)


def test_incomplete_graph_forces_full_run():
    view = _view(code_deps={"A": []}, test_edges={"tA": ["A"]}, uncovered=["B"])
    sel = _select(view, {"A"})
    assert sel.run_all
    assert any("could not analyze" in r for r in sel.reasons)


def test_prior_failure_forces_full_run():
    view = _view(code_deps={"A": [], "B": []}, test_edges={"tA": ["A"], "tB": ["B"]})
    sel = _select(view, {"A"}, prior_failed=True)
    assert sel.run_all
    assert any("previous run failed" in r for r in sel.reasons)


# --- the equivalence oracle --------------------------------------------------

class World:
    """A synthetic project with GROUND-TRUTH test verdicts.

    ``code_deps`` is the static (and, for code-to-code edges, true) dependency
    graph. ``test_static`` is what a provider can *see*; ``test_truth`` is what a
    test *actually* exercises — a superset when it dynamically loads a unit. A
    unit in ``dynamic_units`` performs dynamic loading; the world invariant is
    that any test with a truth-edge the static view misses must statically reach
    such a unit (you cannot dynamically load without a dynamic-load call). A test
    fails iff its true reachable set touches a broken unit.
    """

    def __init__(self, code_deps, test_static, test_truth, dynamic_units):
        self.code_deps = {u: set(d) for u, d in code_deps.items()}
        self.test_static = {t: set(e) for t, e in test_static.items()}
        self.test_truth = {t: set(e) for t, e in test_truth.items()}
        self.dynamic_units = set(dynamic_units)
        self._check_invariant()

    def _check_invariant(self):
        for t, truth in self.test_truth.items():
            static_closure = _reachable(self.test_static[t], _fz(self.code_deps))
            if truth - static_closure:  # a truth edge the static view misses
                assert static_closure & self.dynamic_units, (
                    f"{t} has hidden truth deps but reaches no dynamic unit — "
                    f"an impossible world (dynamic loading needs a dynamic call)"
                )

    def view(self):
        return ProviderView(
            code_deps=_fz(self.code_deps),
            test_edges=_fz(self.test_static),
            dynamic_units=frozenset(self.dynamic_units),
        )

    def true_reachable(self, t):
        return _reachable(self.test_truth[t], _fz(self.code_deps))

    def failing_tests(self, broken):
        return {t for t in self.test_static if self.true_reachable(t) & set(broken)}

    def known(self):
        return frozenset(self.code_deps) | frozenset(self.test_static)


def _fz(graph):
    return {u: frozenset(d) for u, d in graph.items()}


def assert_sound_for_every_break(world: World):
    """Break each unit in turn; the selected run must include every test that
    fails in the full run. This is the equivalence property."""
    for unit in world.code_deps:
        sel = select_tests(
            SelectionInput(world.view(), frozenset({unit}), world.known())
        )
        failing = world.failing_tests({unit})
        missed = failing - sel.tests
        assert not missed, (
            f"UNDER-SELECTION breaking {unit!r}: full run fails {sorted(failing)}, "
            f"selection ran {sorted(sel.tests)} — missed {sorted(missed)}"
        )


def test_oracle_static_world_is_sound_and_selective():
    # A diamond: D->B->A, D->C->A; tests at each level.
    world = World(
        code_deps={"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]},
        test_static={"tA": ["A"], "tB": ["B"], "tC": ["C"], "tD": ["D"]},
        test_truth={"tA": ["A"], "tB": ["B"], "tC": ["C"], "tD": ["D"]},
        dynamic_units=[],
    )
    assert_sound_for_every_break(world)
    # And it actually SELECTS (not a degenerate run-all): editing a leaf C runs
    # only tests reaching C, never tB.
    sel = select_tests(SelectionInput(world.view(), frozenset({"C"}), world.known()))
    assert not sel.run_all
    assert "tB" not in sel.tests and {"tC", "tD"} <= sel.tests


def test_oracle_catches_dynamic_dependency():
    # tX statically references D (a dynamic loader) and, at runtime, D loads U.
    # U appears in tX's truth but NOT its static edges. Breaking U must still run
    # tX — which the dynamic-unsafe rule guarantees. (Remove that rule and this
    # test fails, proving the rule necessary.)
    world = World(
        code_deps={"A": [], "D": [], "U": []},
        test_static={"tA": ["A"], "tX": ["D"]},
        test_truth={"tA": ["A"], "tX": ["D", "U"]},  # tX really uses U via D
        dynamic_units=["D"],
    )
    assert_sound_for_every_break(world)
    # Concretely: breaking U (invisible to static analysis) still selects tX.
    sel = select_tests(SelectionInput(world.view(), frozenset({"U"}), world.known()))
    assert "tX" in sel.tests


def test_oracle_randomized_worlds():
    rng = random.Random(1234)
    for _ in range(300):
        world = _random_world(rng)
        assert_sound_for_every_break(world)


def _random_world(rng):
    """A small random DAG of code units + tests, occasionally with a sound
    dynamic edge (test -> dynamic loader -> hidden unit)."""
    n = rng.randint(2, 6)
    units = [f"u{i}" for i in range(n)]
    code_deps = {}
    for i, u in enumerate(units):
        # depend only on earlier units -> acyclic
        deps = [units[j] for j in range(i) if rng.random() < 0.4]
        code_deps[u] = deps

    dynamic_units = set()
    test_static, test_truth = {}, {}
    for k in range(rng.randint(1, 5)):
        t = f"t{k}"
        anchor = rng.choice(units)
        test_static[t] = [anchor]
        truth = set(_reachable([anchor], _fz(code_deps)))
        # sometimes make the anchor a dynamic loader that pulls in a hidden unit
        if rng.random() < 0.3:
            dynamic_units.add(anchor)
            hidden = rng.choice(units)
            truth.add(hidden)
        test_truth[t] = list(truth)
    return World(code_deps, test_static, test_truth, dynamic_units)
