"""Hierarchical build files: discovery, ownership, and full-merge loading.

Tier 1 of the monorepo-scaling plan (docs/monorepo-scaling.md). A large repo can
split its one root ``brix_build.py`` into several placed at module/service boundaries,
each a **package** responsible for its subtree until interrupted by a deeper
``brix_build.py``. This module discovers those packages, resolves which one *owns* a
given path (nearest ancestor), and loads them.

Loading here is the **safe, non-lazy** foundation: every package is loaded and
their graphs merged into one, so a hierarchical workspace builds *identically* to
the equivalent monolith — the one-root-file case is exactly today's behavior.
The ownership map is what a later tier uses to load only the affected packages;
it is built and tested now, but nothing yet skips a load.

Conventions:

* A package ``brix_build.py`` exposes ``create_graph() -> Graph`` (configurable). When
  loaded as a package it must not run a build itself — the usual
  ``if __name__ == "__main__": cli.main(...)`` guard already prevents that on
  import, so a file can be both a standalone entry point and a package.
* Paths stay **workspace-root-relative**, exactly as in a monolith, so the merge
  is a pure union with no rebasing. (Package-relative paths are a possible later
  ergonomic, deliberately out of tier 1.)
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .graph import Graph
from .patterns import PathPattern

_INDEX_FILE = "workspace-index.json"
_INDEX_VERSION = 4

# Directories never scanned for packages: brix's own storage and the usual
# heavy/generated trees. A package's build outputs live under these, and they
# never contain a package's authoring brix_build.py.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _is_skipped(name: str) -> bool:
    return name in _SKIP_DIRS or name == "build" or name.startswith(".brix")


def find_packages(root: str | Path, *, build_file: str = "brix_build.py") -> list[Path]:
    """Absolute directories under ``root`` that contain a package build file,
    sorted (shallowest-first, then lexicographic). Skips brix's own storage and
    common generated trees."""
    root = Path(root).resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_skipped(d))
        if build_file in filenames:
            found.append(Path(dirpath))
    found.sort(key=lambda p: (len(p.parts), p.as_posix()))
    return found


def owner(path: str | Path, package_dirs: list[Path]) -> Path | None:
    """The package that owns ``path``: the nearest ancestor directory (at or
    above the path) that is a package. None if no package covers it.

    "Nearest ancestor" is the package-boundary rule — a whole service's files
    collapse to its package, and a deeper package carves out its own subtree.
    """
    target = Path(path).resolve()
    base = target if target.is_dir() else target.parent
    dirs = set(package_dirs)
    for candidate in (base, *base.parents):
        if candidate in dirs:
            return candidate
    return None


def merge_graphs(graphs: list[Graph]) -> Graph:
    """Union of several package graphs into one, remapping the index-based
    ``forced_orders`` so cross-referenced positions stay valid after
    concatenation. Actions are shared by reference (never mutated)."""
    combined = Graph()
    for graph in graphs:
        offset = len(combined.actions)
        combined.actions.extend(graph.actions)
        for before, after, trigger in graph.forced_orders:
            combined.forced_orders.append((before + offset, after + offset, trigger))
    return combined


def _load_module(package_dir: Path, build_file: str):
    """Import a package's build file under a unique synthetic module name (all
    are literally ``brix_build.py``, so a shared name would collide in sys.modules)."""
    import sys

    path = package_dir / build_file
    mod_name = "brix_pkg_" + path.resolve().as_posix().replace("/", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package build file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _derive_package_deps(loaded: list[tuple[Path, Graph]]) -> dict[Path, set[Path]]:
    """The cross-package dependency graph — ``package -> the packages it depends
    on`` — derived from the actions' data references, using the planner's exact
    edge logic (`infer_dependency`, the same it applies action-to-action). No
    declaration: if package C has an action consuming an output produced by an
    action in package P, C depends on P."""
    from .planner import infer_dependency

    deps: dict[Path, set[Path]] = {pkg: set() for pkg, _ in loaded}
    for consumer_pkg, consumer_graph in loaded:
        for producer_pkg, producer_graph in loaded:
            if producer_pkg == consumer_pkg or producer_pkg in deps[consumer_pkg]:
                continue
            if any(
                infer_dependency(producer, consumer) is not None
                for consumer in consumer_graph.actions
                for producer in producer_graph.actions
            ):
                deps[consumer_pkg].add(producer_pkg)
    return deps


# --- persisted pattern descriptors -------------------------------------------
#
# The index must let a *later* build — with no package loaded — answer two
# questions: "which packages does this changed path concern?" (matching) and
# "which packages depend on which?" (edges). Both are answered from the same
# persisted per-package input/output pattern descriptors, extracted with the
# planner's own atom helpers, so selection can never drift from what edge
# inference and re-fingerprinting actually look at.

def _pattern_descriptors(graph: Graph) -> dict[str, list[list[str]]]:
    """Serializable input/output path patterns for a package's actions.
    A descriptor is ``["glob", template]`` or ``["regex", source]``. Glob
    excludes are dropped: matching without them can only over-match, which
    only ever loads more — the safe direction."""
    from .data import RegexData
    from .planner import input_atoms, output_atoms

    def descs(atom_lists) -> list[list[str]]:
        out: set[tuple[str, str]] = set()
        for atoms in atom_lists:
            for atom in atoms:
                if isinstance(atom, RegexData):
                    out.add(("regex", atom.regex.pattern))
                else:
                    out.add(("glob", atom.pattern.template))
        return [list(d) for d in sorted(out)]

    return {
        "inputs": descs(input_atoms(a) for a in graph.actions),
        "outputs": descs(output_atoms(a) for a in graph.actions),
    }


def _descriptor_matches(desc: list[str], rel: str) -> bool:
    """Does a repo-relative path fall in a descriptor's path set?"""
    kind, text = desc
    if kind == "regex":
        return re.compile(text).fullmatch(rel) is not None
    return PathPattern(text).match(rel) is not None


import sys
import threading

_footprint_local = threading.local()
_hook_installed = False
_hook_lock = threading.Lock()


def _audit_hook(event: str, args) -> None:
    """Records file-read paths while a package's ``create_graph()`` runs, so a
    later build can reload that package if a file it *read at construction*
    changed (a config it opened, a manifest it parsed) — the dynamic-dependency
    case a static glob pattern doesn't cover. Cheap when inactive: one
    thread-local check per open."""
    record = getattr(_footprint_local, "active", None)
    if record is None:
        return
    if event == "open" and args:
        path = args[0]
        mode = args[1] if len(args) > 1 else "r"
        text_mode = str(mode) if mode is not None else "r"
        if isinstance(path, (str, bytes)) and not any(c in text_mode for c in "wax"):
            try:
                record.add(os.fspath(path))
            except TypeError:
                pass


def _ensure_hook() -> None:
    global _hook_installed
    with _hook_lock:
        if not _hook_installed:
            sys.addaudithook(_audit_hook)  # permanent by design; no-op when inactive
            _hook_installed = True


def _with_footprint(root: Path, brix_path: Path, factory):
    """Run ``factory`` (a package's ``create_graph``) while recording the files
    its construction read, filtered to footprint-relevant repo inputs. Returns
    ``(result, footprint)``."""
    _ensure_hook()
    reads: set[str] = set()
    previous = getattr(_footprint_local, "active", None)
    _footprint_local.active = reads
    try:
        result = factory()
    finally:
        _footprint_local.active = previous

    footprint: set[Path] = set()
    for raw in reads:
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if not path.is_file() or path == brix_path.resolve():
            continue  # the package's own build file is tracked separately
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue  # outside the repo — not a workspace input
        # Skip brix's own machinery and build storage; keep config/data reads.
        if path.suffix in (".py", ".pyc") or "__pycache__" in rel.parts:
            continue
        if rel.parts and (rel.parts[0] == "build" or rel.parts[0].startswith(".brix")):
            continue
        footprint.add(path)
    return result, footprint


@dataclass
class Workspace:
    """A loaded hierarchical workspace: its packages, their merged graph, the
    ownership map, and the *derived* cross-package dependency index. `graph`
    plugs straight into the existing builders (it is an ordinary merged
    `Graph`). `owner_of` maps a changed path to its package; `dependents_of`
    (the reverse of `depends_on`) is what a later tier will walk to load only
    the packages an edit touches — "package P changed → also load its
    dependents." Nothing consumes the index yet, so there is no staleness
    surface here; it is derived and (optionally) persisted for tiers 2b/3."""

    root: Path
    packages: list[Path]
    graph: Graph
    depends_on: dict[Path, set[Path]] = field(default_factory=dict)
    footprints: dict[Path, set[Path]] = field(default_factory=dict)
    patterns: dict[Path, dict[str, list[list[str]]]] = field(default_factory=dict)
    # True when every package was loaded (only=None), so `depends_on` is the
    # complete cross-package graph — the one time save_index can persist edges
    # authoritatively. A subset load leaves it False.
    full_load: bool = True

    def owner_of(self, path: str | Path) -> Path | None:
        return owner(path, self.packages)

    def dependents_of(self, package: str | Path) -> set[Path]:
        """Packages that depend on ``package`` (the reverse index): if this
        package's files change, these must also be loaded/rebuilt."""
        package = Path(package).resolve()
        return {p for p, deps in self.depends_on.items() if package in deps}

    def save_index(self, metadata_dir: str | Path = "build/.brix") -> Path:
        """Persist per-package input/output patterns, footprints, and the
        cross-package dependency edges (repo-relative), so a later build can
        decide what to load without loading — or re-deriving — anything.

        **Merges** with an existing index: entries are written for the packages
        this workspace actually *loaded* (``self.patterns``), entries for
        packages that no longer exist are dropped, and entries for
        discovered-but-unloaded packages are kept as-is — so a subset load
        refreshes its slice without clobbering the rest. ``last_selected``
        records the loaded set, the recovery anchor after a failed build.

        **Edges** are the performance-critical part. Deriving them is
        O(packages² × patterns), so they are computed once on a *full* load
        (where ``depends_on`` is complete and authoritative) and persisted; a
        later subset build reads them O(packages) instead of re-deriving. A
        subset save carries the persisted edges forward — sound because a subset
        build only happens when no ``brix_build.py`` changed, so patterns are stable —
        *unless* a footprint-driven reload actually changed a loaded package's
        input/output patterns, in which case the edges are dropped so the next
        build full-loads and heals them (edges depend only on the patterns)."""
        directory = self.root / metadata_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _INDEX_FILE

        def rel(p: Path) -> str:
            r = p.resolve().relative_to(self.root).as_posix()
            return r if r != "" else "."

        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = {}
        usable = existing.get("version") == _INDEX_VERSION
        old_entries: dict[str, dict] = dict(existing.get("packages", {})) if usable else {}
        alive = {rel(p) for p in self.packages}
        entries = {k: v for k, v in old_entries.items() if k in alive}
        for pkg, descriptors in self.patterns.items():
            entries[rel(pkg)] = {
                "inputs": descriptors["inputs"],
                "outputs": descriptors["outputs"],
                "footprint": sorted(rel(f) for f in self.footprints.get(pkg, set())),
            }

        def io(entry: dict) -> tuple:
            return (entry.get("inputs"), entry.get("outputs"))

        if self.full_load:
            # The authoritative edges: every package was loaded, so depends_on
            # is complete. This is the one place they are (re)derived.
            depends_on: dict[str, list[str]] | None = {
                rel(pkg): sorted(rel(d) for d in deps)
                for pkg, deps in self.depends_on.items()
            }
        elif (
            usable
            and "depends_on" in existing
            and set(entries) == set(old_entries)
            and all(io(entries[k]) == io(old_entries[k]) for k in entries)
        ):
            # Subset save, patterns unchanged: the persisted edges still hold.
            depends_on = existing["depends_on"]
        else:
            # Subset save that changed a package's patterns (a footprint reload):
            # the edges may be stale, so drop them — the next build sees no edges,
            # full-loads, and re-derives. Correct, at the cost of one full load.
            depends_on = None

        payload = {
            "version": _INDEX_VERSION,
            "packages": entries,
            "last_selected": sorted(rel(p) for p in self.patterns),
        }
        if depends_on is not None:
            payload["depends_on"] = depends_on
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        tmp.replace(path)
        return path


@dataclass
class WorkspaceIndex:
    """The persisted index, decoded: per-package pattern/footprint entries, the
    persisted cross-package dependency edges (read O(packages), never
    re-derived), and the last-loaded selection."""

    root: Path
    entries: dict[Path, dict]
    depends_on: dict[Path, set[Path]]
    last_selected: set[Path] | None

    def dependents_closure(self, seeds: set[Path]) -> set[Path]:
        """`seeds` plus every package that transitively depends on one — the
        set that must be reloaded when the seeds' outputs may have changed."""
        dependents: dict[Path, set[Path]] = {}
        for pkg, deps in self.depends_on.items():
            for dep in deps:
                dependents.setdefault(dep, set()).add(pkg)
        closed: set[Path] = set()
        frontier = set(seeds)
        while frontier:
            pkg = frontier.pop()
            if pkg in closed:
                continue
            closed.add(pkg)
            frontier |= dependents.get(pkg, set())
        return closed


def load_index_data(
    root: str | Path, metadata_dir: str | Path = "build/.brix"
) -> WorkspaceIndex | None:
    """Read the persisted index, or None if absent/unreadable/old-version, or
    if it carries no persisted edges (never full-built, or edges invalidated by
    a pattern change) — in every None case the caller degrades to a full load,
    the safe direction, which re-persists the edges. Reading the edges directly
    is O(packages); it never re-derives them (that O(packages²) cost is paid
    once, on a full build, in `save_index`)."""
    root = Path(root).resolve()
    path = root / metadata_dir / _INDEX_FILE
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if payload.get("version") != _INDEX_VERSION:
        return None
    raw_deps = payload.get("depends_on")
    if raw_deps is None:
        return None  # no trustworthy edges -> full load heals the index

    def abs_of(r: str) -> Path:
        return root if r == "." else (root / r)

    entries = {abs_of(k): v for k, v in payload.get("packages", {}).items()}
    depends_on = {
        abs_of(pkg): {abs_of(d) for d in deps} for pkg, deps in raw_deps.items()
    }
    raw_selected = payload.get("last_selected")
    return WorkspaceIndex(
        root=root,
        entries=entries,
        depends_on=depends_on,
        last_selected=(
            {abs_of(r) for r in raw_selected} if raw_selected is not None else None
        ),
    )


def load_index(root: str | Path, metadata_dir: str | Path = "build/.brix") -> dict[Path, set[Path]]:
    """The persisted index as absolute-path `depends_on` (edges derived from
    the persisted patterns), or ``{}`` if absent/unreadable (degrade to a full
    load — the safe direction)."""
    data = load_index_data(root, metadata_dir)
    return data.depends_on if data is not None else {}


def load_workspace(
    root: str | Path,
    *,
    build_file: str = "brix_build.py",
    graph_attr: str = "create_graph",
    only: set[Path] | None = None,
) -> Workspace:
    """Discover every package under ``root``, load each package's graph, and
    merge them. Returns a `Workspace`; ``ws.graph`` is a plain merged `Graph`
    usable with any builder — e.g.::

        ws = load_workspace(ROOT)
        cli.main(lambda: load_workspace(ROOT).graph, root=ROOT, ...)

    ``only`` (a set of package directories) is the **subset-loading primitive**:
    only those packages' graphs are loaded and merged, for a build that touches
    just part of the workspace. It is deliberately *mechanical* — it loads
    exactly what it is told; deciding *which* packages are the affected set (so
    the partial build is complete, not stale) is the lazy-loading tier's job,
    proven by the equivalence oracle. Unselected packages' outputs are assumed
    already present and fresh on disk (from a prior full build). ``packages``
    still lists every discovered package (for ownership); the cross-package
    index is derived only on a full load — a subset build consults the persisted
    index via `load_index`, never a partial re-derivation.

    A package whose build file lacks ``graph_attr`` is skipped (it may be a
    standalone entry point that only wires `cli.main`, not a graph contributor).
    """
    root = Path(root).resolve()
    packages = find_packages(root, build_file=build_file)
    selected = None if only is None else {Path(p).resolve() for p in only}
    loaded: list[tuple[Path, Graph]] = []
    footprints: dict[Path, set[Path]] = {}
    patterns: dict[Path, dict[str, list[list[str]]]] = {}
    for package_dir in packages:
        if selected is not None and package_dir.resolve() not in selected:
            continue
        module = _load_module(package_dir, build_file)
        factory = getattr(module, graph_attr, None)
        if factory is None:
            continue
        graph, footprint = _with_footprint(root, package_dir / build_file, factory)
        if not isinstance(graph, Graph):
            raise TypeError(
                f"{package_dir / build_file}: {graph_attr}() must return a Graph, "
                f"got {type(graph).__name__}"
            )
        loaded.append((package_dir, graph))
        footprints[package_dir] = footprint
        patterns[package_dir] = _pattern_descriptors(graph)
    merged = merge_graphs([g for _, g in loaded])
    # The dependency index is only meaningful for the whole workspace; a subset
    # load leaves it empty and defers to the persisted index.
    depends_on = _derive_package_deps(loaded) if only is None else {}
    return Workspace(
        root=root,
        packages=packages,
        graph=merged,
        depends_on=depends_on,
        footprints=footprints,
        patterns=patterns,
        full_load=only is None,
    )
