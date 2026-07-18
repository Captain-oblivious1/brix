"""Hierarchical build files: discovery, ownership, and full-merge loading.

Tier 1 of the monorepo-scaling plan (docs/monorepo-scaling.md). A large repo can
split its one root ``brix.py`` into several placed at module/service boundaries,
each a **package** responsible for its subtree until interrupted by a deeper
``brix.py``. This module discovers those packages, resolves which one *owns* a
given path (nearest ancestor), and loads them.

Loading here is the **safe, non-lazy** foundation: every package is loaded and
their graphs merged into one, so a hierarchical workspace builds *identically* to
the equivalent monolith — the one-root-file case is exactly today's behavior.
The ownership map is what a later tier uses to load only the affected packages;
it is built and tested now, but nothing yet skips a load.

Conventions:

* A package ``brix.py`` exposes ``create_graph() -> Graph`` (configurable). When
  loaded as a package it must not run a build itself — the usual
  ``if __name__ == "__main__": cli.main(...)`` guard already prevents that on
  import, so a file can be both a standalone entry point and a package.
* Paths stay **workspace-root-relative**, exactly as in a monolith, so the merge
  is a pure union with no rebasing. (Package-relative paths are a possible later
  ergonomic, deliberately out of tier 1.)
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

from .graph import Graph

# Directories never scanned for packages: brix's own storage and the usual
# heavy/generated trees. A package's build outputs live under these, and they
# never contain a package's authoring brix.py.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _is_skipped(name: str) -> bool:
    return name in _SKIP_DIRS or name == "build" or name.startswith(".brix")


def find_packages(root: str | Path, *, brix_file: str = "brix.py") -> list[Path]:
    """Absolute directories under ``root`` that contain a package build file,
    sorted (shallowest-first, then lexicographic). Skips brix's own storage and
    common generated trees."""
    root = Path(root).resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_skipped(d))
        if brix_file in filenames:
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


def _load_module(package_dir: Path, brix_file: str):
    """Import a package's build file under a unique synthetic module name (all
    are literally ``brix.py``, so a shared name would collide in sys.modules)."""
    import sys

    path = package_dir / brix_file
    mod_name = "brix_pkg_" + path.resolve().as_posix().replace("/", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package build file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class Workspace:
    """A loaded hierarchical workspace: its packages, their merged graph, and
    the ownership map. `graph` plugs straight into the existing builders (it is
    an ordinary merged `Graph`); `owner_of` is what a later tier will use to
    load only the packages an edit touches."""

    root: Path
    packages: list[Path]
    graph: Graph

    def owner_of(self, path: str | Path) -> Path | None:
        return owner(path, self.packages)


def load_workspace(
    root: str | Path,
    *,
    brix_file: str = "brix.py",
    graph_attr: str = "create_graph",
) -> Workspace:
    """Discover every package under ``root``, load each package's graph, and
    merge them (full, non-lazy). Returns a `Workspace`; ``ws.graph`` is a plain
    merged `Graph` usable with any builder — e.g.::

        ws = load_workspace(ROOT)
        cli.main(lambda: load_workspace(ROOT).graph, root=ROOT, ...)

    A package whose build file lacks ``graph_attr`` is skipped (it may be a
    standalone entry point that only wires `cli.main`, not a graph contributor).
    """
    root = Path(root).resolve()
    packages = find_packages(root, brix_file=brix_file)
    graphs: list[Graph] = []
    for package_dir in packages:
        module = _load_module(package_dir, brix_file)
        factory = getattr(module, graph_attr, None)
        if factory is None:
            continue
        graph = factory()
        if not isinstance(graph, Graph):
            raise TypeError(
                f"{package_dir / brix_file}: {graph_attr}() must return a Graph, "
                f"got {type(graph).__name__}"
            )
        graphs.append(graph)
    return Workspace(root=root, packages=packages, graph=merge_graphs(graphs))
