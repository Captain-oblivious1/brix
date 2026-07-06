#!/usr/bin/env python3
"""Pluggable cache store demo: a two-tier team cache across machines.

Run from anywhere:  python3 examples/cache_store_demo/brix.py

The cache store is pluggable. `CacheBuilder(inner, store=...)` takes any
object with the three-method protocol (`load` / `restore` / `store`, see
`CacheStoreProtocol`) instead of a local `cache_dir`. `TwoTierCacheStore`
puts a fast local store in *front* of a slow shared ("team") one.

A single `brix.py build` can't show this off -- the interesting behavior is
across machines and checkouts -- so this script simulates several builds in
throwaway workspaces (created in a temp dir and cleaned up at the end). It
walks the four behaviors that matter:

  1. The first build anywhere runs the work and seeds the shared team cache.
  2. A second machine with a cold local cache HITS the team cache (nothing
     runs) and PROMOTES the result into its own local cache.
  3. That machine builds again: its now-warm local cache serves everything
     and the team cache is never consulted -- no round-trip.
  4. A machine whose team cache is DOWN degrades to running the work; the
     build still succeeds. Robustness lives in the layer, so a leaf store
     may simply raise.

Every workspace shares the same source (src/*.txt -> uppercase -> combine),
so every build is a legitimate cache hit for the others.
"""

import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import (
    CacheBuilder,
    CacheStore,
    ExpandAll,
    ExternalProcessAction,
    Graph,
    IncrementalBuilder,
    Reporter,
    TwoTierCacheStore,
)

HERE = Path(__file__).parent


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": data.file("build/all.txt")},
        ),
    )
    return graph


class Spawns(Reporter):
    """Counts real process spawns -- the thing caching avoids."""

    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


class Counting:
    """Wraps a store, counting loads -- to show front-first avoids the back.

    Only `load` is instrumented; `restore`/`store` pass straight through.
    This is itself a valid pluggable store (three methods), so it doubles as
    a second example of the protocol.
    """

    def __init__(self, inner):
        self.inner = inner
        self.loads = 0

    def load(self, fingerprint):
        self.loads += 1
        return self.inner.load(fingerprint)

    def restore(self, fingerprint, entry, ctx):
        return self.inner.restore(fingerprint, entry, ctx)

    def store(self, fingerprint, result, outputs):
        self.inner.store(fingerprint, result, outputs)


class Down:
    """A store that fails every call, standing in for an unreachable remote."""

    def load(self, fingerprint):
        raise ConnectionError("team cache is unreachable")

    def restore(self, fingerprint, entry, ctx):
        raise ConnectionError("team cache is unreachable")

    def store(self, fingerprint, result, outputs):
        raise ConnectionError("team cache is unreachable")


def make_workspace(dst: Path) -> None:
    """A fresh checkout: the shared source and tools, no build outputs."""
    (dst / "src").mkdir(parents=True, exist_ok=True)
    (dst / "tools").mkdir(exist_ok=True)
    for name in ("one", "two"):
        shutil.copy2(HERE / "src" / f"{name}.txt", dst / "src" / f"{name}.txt")
    for tool in ("upper.py", "concat.py"):
        shutil.copy2(HERE / "tools" / tool, dst / "tools" / tool)


def report(result, spawns: Spawns, back: Counting | None = None) -> None:
    sources = Counter(r.source for r in result.process_results)
    kinds = ", ".join(f"{n} {src}" for src, n in sorted(sources.items()))
    print(f"    build: {'OK' if result.success else 'FAILED'}")
    print(f"    processes spawned : {len(spawns.labels)}")
    print(f"    results by source : {kinds}")
    if back is not None:
        print(f"    team-cache loads  : {back.loads}")


def build(root: Path, store) -> tuple:
    spawns = Spawns()
    result = CacheBuilder(IncrementalBuilder(create_graph, root=root), store=store).build(
        reporter=spawns
    )
    return result, spawns


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="brix-cache-demo-"))
    team_cache = work / "team-cache"  # the slow shared store
    local_cache = work / "local-cache"  # this machine's fast front store
    try:
        # 1) Machine A seeds the team cache by running the work.
        print("=== 1. Machine A: first build seeds the shared team cache ===")
        a = work / "machineA"
        make_workspace(a)
        result, spawns = build(a, CacheStore(team_cache))
        report(result, spawns)
        print(f"    output: {(a / 'build/all.txt').read_text()!r}")
        if not result.success:
            return 1

        # 2) Machine B, cold local cache: hit the team cache, promote to front.
        print()
        print("=== 2. Machine B: cold local cache -> team-cache hit + promote ===")
        b = work / "machineB"
        make_workspace(b)
        back = Counting(CacheStore(team_cache))
        result, spawns = build(b, TwoTierCacheStore(CacheStore(local_cache), back))
        report(result, spawns, back)
        promoted = any(local_cache.rglob("entry.json"))
        print(f"    nothing ran; served from the team cache and promoted locally: {promoted}")
        if not result.success:
            return 1

        # 3) Machine B again, warm local cache: the team cache is never touched.
        print()
        print("=== 3. Machine B again: warm local cache -> no team-cache round-trip ===")
        b2 = work / "machineB-again"
        make_workspace(b2)
        back = Counting(CacheStore(team_cache))
        result, spawns = build(b2, TwoTierCacheStore(CacheStore(local_cache), back))
        report(result, spawns, back)
        print(f"    the local front served everything; team-cache loads == {back.loads}")
        if not result.success:
            return 1

        # 4) Machine C, team cache DOWN: degrade to running the work.
        print()
        print("=== 4. Machine C: team cache DOWN -> degrade to running the work ===")
        c = work / "machineC"
        make_workspace(c)
        result, spawns = build(c, TwoTierCacheStore(CacheStore(work / "cold-front"), Down()))
        report(result, spawns)
        print("    an unreachable store is a cache miss, never a failed build")
        print(f"    output: {(c / 'build/all.txt').read_text()!r}")
        return 0 if result.success else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
