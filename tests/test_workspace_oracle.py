"""The equivalence oracle for lazy loading — the safety gate for tier 3.

`assert_selective_equals_full` replays an edit sequence two ways: a **full** load
(every package) and a **selective** load driven by a *selector* — the function
that decides which packages to load given the changed paths. After each edit it
asserts the two workspaces' build outputs are byte-identical. A selector that
loads too few packages leaves a dependent stale, the outputs diverge, and the
oracle fails.

This is deliberately *mechanical scaffolding*: the harness and the subset-load
primitive are Opus work; the real selector (the affected-set completeness logic
in the lazy-loading tier) is authored against this oracle. The tests below prove
the oracle is a real gate — it **passes** a correct selector and **catches** a
stale-producing one.
"""

import shutil

import pytest

from brix import IncrementalBuilder
from brix.workspace import find_packages, load_index, load_workspace, owner

from test_workspace import UPPER_TOOL, _pkg_source


# --- the harness ------------------------------------------------------------

def _snapshot(root):
    """All build outputs as {relpath: bytes}, excluding brix's own metadata."""
    out = {}
    build = root / "build"
    if build.is_dir():
        for path in sorted(build.rglob("*")):
            rel = path.relative_to(root)
            if path.is_file() and not any(part.startswith(".brix") for part in rel.parts):
                out[rel.as_posix()] = path.read_bytes()
    return out


def _full_build(root):
    result = IncrementalBuilder(lambda: load_workspace(root).graph, root=root).build()
    assert result.success, result.errors
    load_workspace(root).save_index()  # keep the persisted index current for selectors


def _selective_build(root, changed, selector):
    packages = find_packages(root)
    chosen = selector(root, changed, packages)
    result = IncrementalBuilder(
        lambda: load_workspace(root, only=chosen).graph, root=root
    ).build()
    assert result.success, result.errors


def assert_selective_equals_full(setup, edits, selector):
    """setup() -> a fresh, identical workspace root. edits: list of edit(root)
    -> list[str] changed repo-relative paths. selector(root, changed, packages)
    -> set of package dirs to load."""
    full_root = setup()
    lazy_root = setup()
    _full_build(full_root)
    _full_build(lazy_root)
    assert _snapshot(full_root) == _snapshot(lazy_root)

    for edit in edits:
        changed_full = edit(full_root)
        changed_lazy = edit(lazy_root)
        assert changed_full == changed_lazy
        _full_build(full_root)
        _selective_build(lazy_root, changed_lazy, selector)
        assert _snapshot(full_root) == _snapshot(lazy_root), (
            "selective build diverged from full — the selector loaded too few packages"
        )


# --- a workspace where app depends on lib -----------------------------------

def _setup_factory(tmp_path):
    counter = {"n": 0}

    def setup():
        counter["n"] += 1
        root = tmp_path / f"ws{counter['n']}"
        root.mkdir()
        (root / "tools").mkdir()
        (root / "tools/upper.py").write_text(UPPER_TOOL)
        (root / "services/lib").mkdir(parents=True)
        (root / "services/lib/src.txt").write_text("lib-v1")
        (root / "services/lib/brix_build.py").write_text(
            _pkg_source("lib", "services/lib/src.txt", "build/lib/out.txt")
        )
        (root / "services/app").mkdir(parents=True)
        (root / "services/app/brix_build.py").write_text(
            _pkg_source("app", "build/lib/out.txt", "build/app/final.txt")
        )
        return root

    return setup


def _edit_lib(root):
    (root / "services/lib/src.txt").write_text("lib-v2-changed")
    return ["services/lib/src.txt"]


# --- selectors --------------------------------------------------------------

def _select_all(root, changed, packages):
    return set(packages)


def _select_owners_and_dependents(root, changed, packages):
    """A correct reference selector: the packages owning the changed files, plus
    their transitive dependents from the persisted index. (The real tier-3 core
    generalizes this — footprints, deletions, etc. — and is Fable's to author;
    this is only here to exercise the oracle.)"""
    index = load_index(root)  # package -> depends_on
    dependents = {}
    for pkg, deps in index.items():
        for dep in deps:
            dependents.setdefault(dep, set()).add(pkg)

    chosen = set()
    frontier = {owner(root / c, packages) for c in changed}
    frontier.discard(None)
    while frontier:
        pkg = frontier.pop()
        if pkg in chosen:
            continue
        chosen.add(pkg)
        frontier |= dependents.get(pkg, set())
    return chosen


def _select_owner_only_BROKEN(root, changed, packages):
    """Deliberately incomplete: loads only the changed package, never its
    dependents — so editing lib leaves app stale. The oracle must catch this."""
    chosen = {owner(root / c, packages) for c in changed}
    chosen.discard(None)
    return chosen


# --- the proofs -------------------------------------------------------------

def test_select_all_passes(tmp_path):
    # The trivial correct selector: load everything. Must always equal full.
    assert_selective_equals_full(_setup_factory(tmp_path), [_edit_lib], _select_all)


def test_correct_owners_and_dependents_selector_passes(tmp_path):
    # lib changes -> load lib and its dependent app -> equals full.
    assert_selective_equals_full(
        _setup_factory(tmp_path), [_edit_lib], _select_owners_and_dependents
    )


def test_oracle_catches_a_stale_producing_selector(tmp_path):
    # lib changes but the selector omits app -> app's output goes stale ->
    # the oracle MUST fail. This is what proves it is a real gate.
    with pytest.raises(AssertionError, match="diverged from full"):
        assert_selective_equals_full(
            _setup_factory(tmp_path), [_edit_lib], _select_owner_only_BROKEN
        )
