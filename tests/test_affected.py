"""Tier 3's gate: the real affected-set selector against the equivalence oracle.

`test_workspace_oracle` proved the oracle catches an under-selecting selector;
this file runs the *shipping* selector (`brix.affected`) through it across the
full edit alphabet — in-package edit rippling through a multi-hop dependent
chain, an edit to a shared input outside every package's subtree (the case
ownership-based selection gets wrong), a dynamic-config change caught only by
the construction footprint, a deletion matching a glob input, a new package, a
dependency rewired by editing a `brix.py` (a FULL-load fallback), and an
irrelevant edit (empty selection). Every step must build byte-identically to a
full load, and the selection tests prove the lazy side genuinely loads *less*.
"""

import json
import sys

import pytest

from brix import IncrementalBuilder
from brix.affected import load_affected, select_affected
from brix.metadata import BuildMetadata
from brix.workspace import find_packages, load_index_data, load_workspace

from test_workspace import UPPER_TOOL, _pkg_source
from test_workspace_oracle import _full_build, _snapshot

# Behaves differently from UPPER_TOOL (appends "!"), so a tool edit that is
# missed by under-selection produces different bytes and the oracle trips.
UPPER_TOOL_V2 = UPPER_TOOL.replace(".upper())", ".upper() + '!')")

CAT_TOOL = (
    "import sys\n"
    "from pathlib import Path\n"
    "dest = Path(sys.argv[2])\n"
    "dest.parent.mkdir(parents=True, exist_ok=True)\n"
    "dest.write_text(''.join(p.read_text()"
    " for p in sorted(Path(sys.argv[1]).glob('*.txt'))))\n"
)

# A package whose create_graph() reads a config file to decide its input —
# the dynamic-dependency case only the construction footprint can catch.
DYN_PKG = (
    "import sys\n"
    "from pathlib import Path\n"
    "from brix import Graph, ExternalProcessAction\n"
    "def create_graph():\n"
    "    g = Graph(); d = g.data\n"
    "    src = Path(__file__).with_name('deps.txt').read_text().strip()\n"
    "    g.add(ExternalProcessAction(\n"
    "        name='upper dyn',\n"
    "        command_line='{python} {tool} {src} {dest}',\n"
    "        inputs={'python': d.tool(sys.executable),\n"
    "                'tool': d.file('tools/upper.py'), 'src': d.file(src)},\n"
    "        outputs={'dest': d.file('build/dyn/out.txt')},\n"
    "    ))\n"
    "    return g\n"
)

# A package with a glob input, so file additions/deletions in the globbed
# directory must select it via pattern matching.
GLOB_PKG = (
    "import sys\n"
    "from brix import Graph, ExternalProcessAction\n"
    "def create_graph():\n"
    "    g = Graph(); d = g.data\n"
    "    g.add(ExternalProcessAction(\n"
    "        name='cat glob',\n"
    "        command_line='{python} {tool} services/glob/parts {dest}',\n"
    "        inputs={'python': d.tool(sys.executable),\n"
    "                'tool': d.file('tools/cat.py'),\n"
    "                'srcs': d.glob('services/glob/parts/{n:*}.txt')},\n"
    "        outputs={'dest': d.file('build/glob/all.txt')},\n"
    "    ))\n"
    "    return g\n"
)


def _write_workspace(root):
    """lib -> mid -> app (a multi-hop chain), plus dyn (footprint), glob, and
    a shared tools/ script that is an input of every upper package but lives
    in no package's subtree."""
    (root / "tools").mkdir(parents=True)
    (root / "tools/upper.py").write_text(UPPER_TOOL)
    (root / "tools/cat.py").write_text(CAT_TOOL)
    (root / "README.md").write_text("not a build input\n")

    (root / "services/lib").mkdir(parents=True)
    (root / "services/lib/src.txt").write_text("lib-v1")
    (root / "services/lib/brix.py").write_text(
        _pkg_source("lib", "services/lib/src.txt", "build/lib/out.txt")
    )
    (root / "services/mid").mkdir(parents=True)
    (root / "services/mid/brix.py").write_text(
        _pkg_source("mid", "build/lib/out.txt", "build/mid/out.txt")
    )
    (root / "services/app").mkdir(parents=True)
    (root / "services/app/brix.py").write_text(
        _pkg_source("app", "build/mid/out.txt", "build/app/final.txt")
    )

    (root / "services/dyn").mkdir(parents=True)
    (root / "services/dyn/a.txt").write_text("dyn-a")
    (root / "services/dyn/b.txt").write_text("dyn-b")
    (root / "services/dyn/deps.txt").write_text("services/dyn/a.txt")
    (root / "services/dyn/brix.py").write_text(DYN_PKG)

    (root / "services/glob/parts").mkdir(parents=True)
    (root / "services/glob/parts/1.txt").write_text("one\n")
    (root / "services/glob/parts/2.txt").write_text("two\n")
    (root / "services/glob/brix.py").write_text(GLOB_PKG)
    return root


def _setup_factory(tmp_path):
    counter = {"n": 0}

    def setup():
        counter["n"] += 1
        root = tmp_path / f"ws{counter['n']}"
        root.mkdir()
        return _write_workspace(root)

    return setup


# --- the lazy harness: the real load_affected flow ---------------------------

def _lazy_build(root, changed):
    workspace = load_affected(root, changed)
    result = IncrementalBuilder(lambda: workspace.graph, root=root).build()
    assert result.success, result.errors


def assert_lazy_equals_full(setup, edits):
    """Like the oracle's assert_selective_equals_full, but driving the real
    `load_affected` flow — selection, subset load, *and* the index refresh a
    real incremental session performs between edits."""
    full_root = setup()
    lazy_root = setup()
    _full_build(full_root)
    _lazy_build(lazy_root, None)  # bootstrap: no git, no index -> full load
    assert _snapshot(full_root) == _snapshot(lazy_root)

    for edit in edits:
        changed_full = edit(full_root)
        changed_lazy = edit(lazy_root)
        assert changed_full == changed_lazy
        _full_build(full_root)
        _lazy_build(lazy_root, changed_lazy)
        assert _snapshot(full_root) == _snapshot(lazy_root), (
            "lazy-loaded build diverged from full — the selector loaded too few packages"
        )


# --- the edit alphabet --------------------------------------------------------

def _edit_lib(root):
    (root / "services/lib/src.txt").write_text("lib-v2")
    return ["services/lib/src.txt"]


def _edit_irrelevant(root):
    (root / "README.md").write_text("still not a build input\n")
    return ["README.md"]


def _edit_shared_tool(root):
    # tools/ is outside every package subtree: ownership-based selection sees
    # no owner and picks nothing; pattern matching selects every consumer.
    (root / "tools/upper.py").write_text(UPPER_TOOL_V2)
    return ["tools/upper.py"]


def _edit_dyn_config(root):
    # Changes what dyn's create_graph() *constructs* — only the footprint
    # knows deps.txt matters.
    (root / "services/dyn/deps.txt").write_text("services/dyn/b.txt")
    return ["services/dyn/deps.txt"]


def _delete_glob_member(root):
    (root / "services/glob/parts/2.txt").unlink()
    return ["services/glob/parts/2.txt"]


def _add_package(root):
    (root / "services/new").mkdir(parents=True)
    (root / "services/new/src.txt").write_text("new-src")
    (root / "services/new/brix.py").write_text(
        _pkg_source("new", "services/new/src.txt", "build/new/out.txt")
    )
    return ["services/new/brix.py", "services/new/src.txt"]


def _rewire_app(root):
    # A cross-package dependency changed by editing the brix.py itself:
    # app now consumes lib's output directly. Must trigger a FULL load.
    (root / "services/app/brix.py").write_text(
        _pkg_source("app", "build/lib/out.txt", "build/app/final.txt")
    )
    return ["services/app/brix.py"]


def _edit_lib_after_rewire(root):
    # Post-rewire: proves the refreshed index carries the *new* edge.
    (root / "services/lib/src.txt").write_text("lib-v3")
    return ["services/lib/src.txt"]


def test_real_selector_survives_the_full_edit_alphabet(tmp_path):
    assert_lazy_equals_full(
        _setup_factory(tmp_path),
        [
            _edit_lib,
            _edit_irrelevant,
            _edit_shared_tool,
            _edit_dyn_config,
            _delete_glob_member,
            _add_package,
            _rewire_app,
            _edit_lib_after_rewire,
        ],
    )


def test_footprint_blind_selector_is_caught(tmp_path, monkeypatch):
    """Negative control: the lazy harness is a live gate for the *real* flow.
    Blind the selector to footprints and the dynamic-config edit must produce
    a divergent (stale) build that the oracle catches."""
    import brix.affected as affected
    from brix.workspace import load_index_data as real_load_index_data

    def stripped(root, metadata_dir="build/.brix"):
        data = real_load_index_data(root, metadata_dir)
        if data is not None:
            for entry in data.entries.values():
                entry["footprint"] = []
        return data

    monkeypatch.setattr(affected, "load_index_data", stripped)
    with pytest.raises(AssertionError, match="diverged from full"):
        assert_lazy_equals_full(_setup_factory(tmp_path), [_edit_dyn_config])


# --- and it must actually be lazy ---------------------------------------------

def _rel_set(root, packages):
    return {p.relative_to(root).as_posix() for p in packages}


def test_selection_loads_less_than_everything(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    (tmp_path / "ws").mkdir(exist_ok=True)
    _full_build(root)
    packages = find_packages(root)

    # A lib edit implicates the chain, not dyn/glob.
    chosen = select_affected(root, ["services/lib/src.txt"], packages)
    assert _rel_set(root, chosen) == {"services/lib", "services/mid", "services/app"}

    # The dynamic config is nobody's declared input — only dyn's footprint.
    chosen = select_affected(root, ["services/dyn/deps.txt"], packages)
    assert _rel_set(root, chosen) == {"services/dyn"}

    # An irrelevant file selects nothing at all.
    assert select_affected(root, ["README.md"], packages) == set()

    # The shared tool is an input pattern of every upper package.
    chosen = select_affected(root, ["tools/upper.py"], packages)
    assert {"services/lib", "services/mid", "services/app", "services/dyn"} <= _rel_set(
        root, chosen
    )


# --- persisted edges (the O(n^2)-selection fix) ---------------------------------

def _index_json(root):
    return json.loads((root / "build/.brix/workspace-index.json").read_text())


def test_full_build_persists_the_dependency_edges(tmp_path):
    # The fix: edges are derived once on a full build and stored, so selection
    # reads them O(packages) instead of re-deriving O(packages^2). A silent
    # revert to derive-on-load would drop this key and fail here.
    root = _write_workspace(tmp_path / "ws")
    load_workspace(root).save_index()
    index = _index_json(root)
    assert "depends_on" in index
    # The chain is lib -> mid -> app, so each consumer records its producer.
    assert index["depends_on"]["services/mid"] == ["services/lib"]
    assert index["depends_on"]["services/app"] == ["services/mid"]
    # And selection uses them: editing lib pulls in its transitive dependents.
    chosen = select_affected(root, ["services/lib/src.txt"], find_packages(root))
    assert _rel_set(root, chosen) == {"services/lib", "services/mid", "services/app"}


def test_index_without_persisted_edges_forces_a_full_load(tmp_path):
    # An index carrying no edges (a subset save that changed patterns, or a
    # pre-fix index) must degrade to a full load, which re-persists them.
    root = _write_workspace(tmp_path / "ws")
    load_workspace(root).save_index()
    index_path = root / "build/.brix/workspace-index.json"
    payload = json.loads(index_path.read_text())
    del payload["depends_on"]
    index_path.write_text(json.dumps(payload))
    assert load_index_data(root) is None  # no edges -> caller full-loads
    assert select_affected(root, ["services/lib/src.txt"], find_packages(root)) is None


def test_source_edit_carries_edges_forward_but_footprint_change_drops_them(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    load_workspace(root).save_index()

    # A subset load whose patterns are unchanged (an ordinary source edit)
    # keeps the persisted edges.
    load_workspace(root, only={root / "services/lib"}).save_index()
    assert "depends_on" in _index_json(root)

    # dyn's create_graph reads deps.txt to choose its input; flipping it changes
    # dyn's *patterns*, so a subset reload of dyn must drop the edges (they may
    # be stale) — the next build full-loads and heals.
    (root / "services/dyn/deps.txt").write_text("services/dyn/b.txt")
    load_workspace(root, only={root / "services/dyn"}).save_index()
    assert "depends_on" not in _index_json(root)
    assert load_index_data(root) is None


# --- the FULL fallbacks ---------------------------------------------------------

def test_no_index_means_full(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    assert select_affected(root, ["services/lib/src.txt"], find_packages(root)) is None


def test_brix_py_change_means_full(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    _full_build(root)
    packages = find_packages(root)
    assert select_affected(root, ["services/app/brix.py"], packages) is None


def test_out_of_tree_change_means_full(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    _full_build(root)
    assert select_affected(root, ["/somewhere/else.txt"], find_packages(root)) is None


def test_scanned_dep_change_means_full(tmp_path):
    # Discovered deps (compiler-scanned headers) are keyed by action, not
    # package — a change to one cannot be attributed without loading, so FULL.
    root = _write_workspace(tmp_path / "ws")
    _full_build(root)
    metadata = BuildMetadata(root, "build/.brix")
    metadata.load()
    metadata.set_discovered_deps("some-action", ["services/lib/impl.h"])
    metadata.save()
    packages = find_packages(root)
    assert select_affected(root, ["services/lib/impl.h"], packages) is None
    # ...while an unrelated path still selects normally.
    assert select_affected(root, ["README.md"], packages) == set()


def test_new_package_is_always_selected(tmp_path):
    # Belt-and-suspenders: even if the change list somehow omits the new
    # brix.py, a discovered package with no index entry is never provably
    # unaffected, so it is selected.
    root = _write_workspace(tmp_path / "ws")
    _full_build(root)
    (root / "services/new").mkdir(parents=True)
    (root / "services/new/src.txt").write_text("new-src")
    (root / "services/new/brix.py").write_text(
        _pkg_source("new", "services/new/src.txt", "build/new/out.txt")
    )
    chosen = select_affected(root, ["services/new/src.txt"], find_packages(root))
    assert root / "services/new" in chosen


def test_failed_build_recovers_last_selected(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    _full_build(root)
    # Simulate a selective session that loaded only lib, then failed mid-build.
    load_workspace(root, only={root / "services/lib"}).save_index()
    metadata = BuildMetadata(root, "build/.brix")
    metadata.load()
    metadata.record_action("some-action", {"ok": False})
    metadata.save()
    # Even with nothing changed since, the failed selection is re-loaded.
    chosen = select_affected(root, [], find_packages(root))
    assert root / "services/lib" in chosen


def test_failed_build_without_last_selected_means_full(tmp_path):
    root = _write_workspace(tmp_path / "ws")
    _full_build(root)
    index_path = root / "build/.brix/workspace-index.json"
    payload = json.loads(index_path.read_text())
    del payload["last_selected"]
    index_path.write_text(json.dumps(payload))
    metadata = BuildMetadata(root, "build/.brix")
    metadata.load()
    metadata.record_action("some-action", {"ok": False})
    metadata.save()
    assert select_affected(root, [], find_packages(root)) is None
