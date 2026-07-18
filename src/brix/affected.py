"""Affected-package selection: the lazy-loading core (tier 3 of
docs/monorepo-scaling.md).

Given the paths changed since the last build, decide the provably complete set
of package ``brix.py`` files to load — or admit that nothing can be proven and
load them all. The governing invariant from the design note: **over-loading is
a wasted parse (harmless); under-loading is a silent stale build (fatal).**
Every rule here resolves uncertainty toward loading more, and ``None`` —
"load everything" — is the answer whenever proof runs out.

Selection is **pattern-based, not ownership-based**. Packages may legitimately
reference paths outside their own subtree (a shared ``tools/`` script is an
input of many packages), so "which package directory contains the changed
file" under-selects. What is sound is matching each changed path against the
persisted per-package input/output patterns — extracted from the very atoms
the planner uses for edge inference and the incremental layer re-fingerprints
— plus each package's construction footprint (the files its ``create_graph``
read: the dynamic-dependency case), then closing over the derived reverse
edges so dependents of an affected package reload too.

The FULL fallbacks (``select_affected`` returns ``None``):

* no persisted index — first build, version bump, or corruption;
* any package build file itself changed — an added/removed/edited ``brix.py``
  can change the workspace's structure, and only a full load re-derives it;
* a changed path appears in the discovered-deps table — compiler-scanned
  inputs (e.g. C headers) are recorded per *action key*, which cannot be
  attributed to a package without loading it, so any hit forces a full load;
* a changed path lies outside the workspace — nothing relates it to packages;
* the previous build failed and there is no recorded ``last_selected`` to
  recover from. (When there is one, it is unioned into the selection: the
  failed build's packages have unfinished work regardless of what changed
  since.)

`load_affected` is the composition entry point and is **opt-in** — the
monolithic full load stays the default. Changes come either from the caller
or from `GitChangeDetector.changed_paths()`; when neither can answer, it
degrades to exactly `load_workspace(root)`.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .metadata import BuildMetadata, TimeAndHashChangeDetector
from .workspace import (
    Workspace,
    _descriptor_matches,
    find_packages,
    load_index_data,
    load_workspace,
)


def _relative(root: Path, changed) -> list[str] | None:
    """Changed paths as repo-relative POSIX strings; None if any falls outside
    the workspace (an input we cannot relate to any package -> full load)."""
    rels: list[str] = []
    for entry in changed:
        path = Path(entry)
        if not path.is_absolute():
            path = root / path
        try:
            rels.append(path.resolve().relative_to(root).as_posix())
        except ValueError:
            return None
    return rels


def select_affected(
    root: str | Path,
    changed,
    packages,
    *,
    brix_file: str = "brix.py",
    metadata_dir: str | Path = "build/.brix",
) -> set[Path] | None:
    """The packages that must be loaded given ``changed`` paths, or ``None``
    meaning "load everything" (see the module docstring for when).

    ``packages`` is the currently *discovered* package list (from
    `find_packages`); a discovered package with no index entry is new since
    the last index write and is always selected — there is no proof it is
    unaffected."""
    root = Path(root).resolve()
    index = load_index_data(root, metadata_dir)
    if index is None:
        return None
    rels = _relative(root, changed)
    if rels is None:
        return None
    if any(PurePosixPath(r).name == brix_file for r in rels):
        return None

    metadata = BuildMetadata(root, metadata_dir)
    metadata.load()
    recovery: set[Path] = set()
    if metadata.has_failed_actions():
        if index.last_selected is None:
            return None
        recovery = set(index.last_selected)
    scanned = metadata.all_discovered_deps()
    if any(r in scanned for r in rels):
        return None

    package_dirs = [Path(p).resolve() for p in packages]
    seeds = {p for p in package_dirs if p not in index.entries}  # new packages
    for pkg in package_dirs:
        entry = index.entries.get(pkg)
        if entry is None:
            continue
        footprint = set(entry.get("footprint", []))
        descriptors = list(entry.get("inputs", [])) + list(entry.get("outputs", []))
        for rel in rels:
            if rel in footprint or any(
                _descriptor_matches(d, rel) for d in descriptors
            ):
                seeds.add(pkg)
                break
    return index.dependents_closure(seeds) | recovery


def _git_changed(root: Path, metadata_dir: str | Path) -> set[Path] | None:
    """Enumerate changes since the last build via git, or None when git cannot
    answer. The metadata here is a private read-only copy — the build's own
    detector is what records the next baseline, at build end."""
    from .gitdetect import GitChangeDetector

    metadata = BuildMetadata(root, metadata_dir)
    metadata.load()
    detector = GitChangeDetector(root, inner=TimeAndHashChangeDetector(root, metadata))
    return detector.changed_paths()


def load_affected(
    root: str | Path,
    changed=None,
    *,
    brix_file: str = "brix.py",
    graph_attr: str = "create_graph",
    metadata_dir: str | Path = "build/.brix",
) -> Workspace:
    """Load only the packages affected by ``changed`` (or by what git reports
    changed since the last build, when ``changed`` is None) — falling back to
    a full `load_workspace` whenever completeness cannot be proven.

    Either way the persisted index is refreshed for every package actually
    loaded (`Workspace.save_index` merges, so a subset load never clobbers the
    other packages' entries), and ``last_selected`` is written **at load
    time** — before the build runs — so a build that fails midway can be
    recovered by the next selection.
    """
    root = Path(root).resolve()
    if changed is None:
        changed = _git_changed(root, metadata_dir)
    selected: set[Path] | None = None
    if changed is not None:
        packages = find_packages(root, brix_file=brix_file)
        selected = select_affected(
            root, changed, packages, brix_file=brix_file, metadata_dir=metadata_dir
        )
    workspace = load_workspace(
        root, brix_file=brix_file, graph_attr=graph_attr, only=selected
    )
    workspace.save_index(metadata_dir)
    return workspace
