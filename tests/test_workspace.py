"""Hierarchical build files, tier 1: discovery, ownership, full-merge loading.

The load-bearing test is the **equivalence to the monolith**: a workspace split
into several package `brix.py` files must build identically to one root file with
the same actions. Tier 1 loads every package (no laziness), so this is where the
"the monolith is the degenerate case" contract is pinned.
"""

import sys
from pathlib import Path

from brix import ExternalProcessAction, Graph, IncrementalBuilder
from brix.workspace import find_packages, load_workspace, merge_graphs, owner

UPPER_TOOL = (
    "import sys\n"
    "from pathlib import Path\n"
    "dest = Path(sys.argv[2])\n"
    "dest.parent.mkdir(parents=True, exist_ok=True)\n"
    "dest.write_text(Path(sys.argv[1]).read_text().upper())\n"
)


def _upper_action(name, src, dest):
    g = Graph()
    d = g.data
    g.add(
        ExternalProcessAction(
            name=f"upper {name}",
            command_line="{python} {tool} {src} {dest}",
            inputs={
                "python": d.tool(sys.executable),
                "tool": d.file("tools/upper.py"),
                "src": d.file(src),
            },
            outputs={"dest": d.file(dest)},
        )
    )
    return g


def _pkg_source(name, src, dest):
    """A package brix.py exposing create_graph() for one upper action."""
    return (
        "import sys\n"
        "from brix import Graph, ExternalProcessAction\n"
        "def create_graph():\n"
        "    g = Graph(); d = g.data\n"
        "    g.add(ExternalProcessAction(\n"
        "        name=%r,\n"
        "        command_line='{python} {tool} {src} {dest}',\n"
        "        inputs={'python': d.tool(sys.executable),\n"
        "                'tool': d.file('tools/upper.py'), 'src': d.file(%r)},\n"
        "        outputs={'dest': d.file(%r)},\n"
        "    ))\n"
        "    return g\n"
    ) % ("upper " + name, src, dest)


# The three package slices, as (name, src, dest).
SLICES = [
    ("app", "app/main.txt", "build/app/main.txt"),
    ("foo", "services/foo/data.txt", "build/foo/data.txt"),
    ("bar", "services/bar/data.txt", "build/bar/data.txt"),
]


def _write_common(root):
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools/upper.py").write_text(UPPER_TOOL)
    for _, src, _dest in SLICES:
        p = root / src
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {src}")


def _write_hierarchical(root):
    _write_common(root)
    # A package brix.py at the root (app) and one per service.
    (root / "brix.py").write_text(_pkg_source(*SLICES[0]))
    (root / "services/foo/brix.py").write_text(_pkg_source(*SLICES[1]))
    (root / "services/bar/brix.py").write_text(_pkg_source(*SLICES[2]))


def _monolith(root):
    graph = Graph()
    for name, src, dest in SLICES:
        graph = merge_graphs([graph, _upper_action(name, src, dest)])
    return graph


# --- discovery + ownership --------------------------------------------------

def test_find_packages_discovers_all_and_skips_build(tmp_path):
    _write_hierarchical(tmp_path)
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build/brix.py").write_text("# stray file under build/, must be skipped\n")
    packages = find_packages(tmp_path)
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in packages)
    assert rels == [".", "services/bar", "services/foo"]


def test_owner_is_nearest_ancestor(tmp_path):
    _write_hierarchical(tmp_path)
    packages = find_packages(tmp_path)
    # A file under services/foo is owned by services/foo, not the root.
    assert owner(tmp_path / "services/foo/data.txt", packages) == tmp_path / "services/foo"
    # A file with no deeper package is owned by the root.
    assert owner(tmp_path / "app/main.txt", packages) == tmp_path
    assert owner(tmp_path / "tools/upper.py", packages) == tmp_path


def test_owner_deeper_package_carves_out_subtree(tmp_path):
    _write_hierarchical(tmp_path)
    # Add a nested package under services/foo/inner.
    (tmp_path / "services/foo/inner").mkdir()
    (tmp_path / "services/foo/inner/brix.py").write_text("# inner package\n")
    packages = find_packages(tmp_path)
    # A file under the nested package is owned by it, not services/foo.
    assert owner(tmp_path / "services/foo/inner/x.txt", packages) == tmp_path / "services/foo/inner"
    # A sibling still under services/foo stays with services/foo.
    assert owner(tmp_path / "services/foo/data.txt", packages) == tmp_path / "services/foo"


# --- the equivalence: hierarchical builds identically to the monolith -------

def test_hierarchical_workspace_builds_identically_to_monolith(tmp_path):
    mono_root = tmp_path / "mono"
    hier_root = tmp_path / "hier"
    mono_root.mkdir()
    hier_root.mkdir()
    _write_common(mono_root)
    _write_hierarchical(hier_root)

    mono_result = IncrementalBuilder(lambda: _monolith(mono_root), root=mono_root).build()
    hier_result = IncrementalBuilder(
        lambda: load_workspace(hier_root).graph, root=hier_root
    ).build()

    assert mono_result.success and hier_result.success
    # Same action set (labels), regardless of package split.
    mono_labels = sorted(n.label for n in mono_result.plan.nodes)
    hier_labels = sorted(n.label for n in hier_result.plan.nodes)
    assert mono_labels == hier_labels

    # Same outputs, byte-for-byte.
    for _name, _src, dest in SLICES:
        assert (mono_root / dest).read_text() == (hier_root / dest).read_text()
        assert (hier_root / dest).exists()


def test_workspace_owner_of_convenience(tmp_path):
    _write_hierarchical(tmp_path)
    ws = load_workspace(tmp_path)
    assert ws.owner_of(tmp_path / "services/bar/data.txt") == tmp_path / "services/bar"
    assert {p.relative_to(tmp_path).as_posix() for p in ws.packages} == {
        ".", "services/foo", "services/bar"
    }


def test_merge_remaps_forced_orders(tmp_path):
    _write_common(tmp_path)
    a = _upper_action("a", "app/main.txt", "build/a.txt")
    b = _upper_action("b", "services/foo/data.txt", "build/b.txt")
    # A forced order within b (between its own action and a second one).
    b.add(
        ExternalProcessAction(
            name="second b",
            command_line="{python} {tool} {src} {dest}",
            inputs={"python": b.data.tool(sys.executable),
                    "tool": b.data.file("tools/upper.py"),
                    "src": b.data.file("services/bar/data.txt")},
            outputs={"dest": b.data.file("build/b2.txt")},
        )
    )
    b.force_order(b.actions[0], b.actions[1])
    merged = merge_graphs([a, b])
    assert len(merged.actions) == 3  # a(1) + b(2)
    # b's forced order (0->1 within b) is remapped by a's offset (1) -> (1->2).
    assert merged.forced_orders == [(1, 2, b.forced_orders[0][2])]


# --- tier 2: the derived cross-package dependency index ---------------------

def _write_dep_workspace(root):
    """A workspace where `app` consumes `lib`'s output (a cross-package edge),
    plus an independent `root` package with no cross-deps."""
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools/upper.py").write_text(UPPER_TOOL)
    (root / "services/lib").mkdir(parents=True, exist_ok=True)
    (root / "services/lib/src.txt").write_text("lib source")
    (root / "services/lib/brix.py").write_text(
        _pkg_source("lib", "services/lib/src.txt", "build/lib/out.txt")
    )
    (root / "services/app").mkdir(parents=True, exist_ok=True)
    # app reads lib's output -> app depends on lib.
    (root / "services/app/brix.py").write_text(
        _pkg_source("app", "build/lib/out.txt", "build/app/final.txt")
    )
    # root package: independent (reads a static tool file, produces its own out).
    (root / "brix.py").write_text(
        _pkg_source("root", "tools/upper.py", "build/root/out.txt")
    )


def test_derived_cross_package_index(tmp_path):
    _write_dep_workspace(tmp_path)
    ws = load_workspace(tmp_path)
    lib = tmp_path / "services/lib"
    app = tmp_path / "services/app"
    root = tmp_path

    # Derived purely from the data references — no declaration.
    assert ws.depends_on[app] == {lib}
    assert ws.depends_on[lib] == set()
    assert ws.depends_on[root] == set()
    # The reverse index: lib changing implicates app.
    assert ws.dependents_of(lib) == {app}
    assert ws.dependents_of(app) == set()


def test_index_persistence_round_trip(tmp_path):
    _write_dep_workspace(tmp_path)
    ws = load_workspace(tmp_path)
    ws.save_index()

    from brix.workspace import load_index

    loaded = load_index(tmp_path)
    lib = tmp_path / "services/lib"
    app = tmp_path / "services/app"
    assert loaded[app] == {lib}
    assert loaded[lib] == set()
    assert (tmp_path / "build/.brix/workspace-index.json").exists()


def test_missing_index_degrades_to_empty(tmp_path):
    from brix.workspace import load_index

    assert load_index(tmp_path) == {}  # no index yet -> safe (full load)
