"""Pluggable cache store: custom stores, layer-level degradation, two-tier."""

from dataclasses import asdict
from pathlib import Path

import pytest

from brix import CacheBuilder, CacheStore, IncrementalBuilder, Reporter, TwoTierCacheStore

from test_incremental import make_graph, runs, write_workspace


class Spawns(Reporter):
    """Counts real process spawns — the thing caching is meant to avoid."""

    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


class MemoryStore:
    """A store that keeps entries and blob *content* in memory — proves the
    protocol works for a non-filesystem backend."""

    def __init__(self):
        self.entries: dict[str, dict] = {}  # fp -> {"result", "files": {logical: bytes}}
        self.loads = 0
        self.stores = 0

    def load(self, fingerprint):
        self.loads += 1
        e = self.entries.get(fingerprint)
        if e is None:
            return None
        return {"result": dict(e["result"]), "outputs": list(e["files"])}

    def restore(self, fingerprint, entry, ctx):
        e = self.entries.get(fingerprint)
        if e is None:
            return False
        for logical, data in e["files"].items():
            dest = ctx.output_root / logical
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return True

    def store(self, fingerprint, result, outputs):
        self.stores += 1
        self.entries[fingerprint] = {
            "result": asdict(result),
            "files": {logical: Path(p).read_bytes() for logical, p in outputs.items()},
        }


class Counting:
    """Wraps a store, counting loads — to prove front-first avoids back hits."""

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


class Broken:
    """A store that fails every operation, as an unreachable remote would."""

    def load(self, fingerprint):
        raise ConnectionError("cache unreachable")

    def restore(self, fingerprint, entry, ctx):
        raise ConnectionError("cache unreachable")

    def store(self, fingerprint, result, outputs):
        raise ConnectionError("cache unreachable")


# -- custom store via store= -------------------------------------------------


def test_custom_store_used_end_to_end(tmp_path):
    """A store= object caches across workspaces just like cache_dir."""
    mem = MemoryStore()
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        write_workspace(root, {"a": "x", "b": "y"})

    assert CacheBuilder(IncrementalBuilder(make_graph, root=first), store=mem).build().success
    assert mem.stores  # real runs populated the in-memory store
    assert runs(first)

    spawns = Spawns()
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=second), store=mem
    ).build(reporter=spawns)
    assert result.success
    assert spawns.labels == []  # everything came from the store
    assert runs(second) == {}
    assert (second / "build/all.txt").read_text() == "XY"
    assert all(r.source == "cache" for r in result.process_results)


def test_cache_dir_and_store_together_rejected(tmp_path):
    with pytest.raises(ValueError):
        CacheBuilder(
            IncrementalBuilder(make_graph, root=tmp_path),
            cache_dir=tmp_path / "c",
            store=MemoryStore(),
        )


# -- layer-level degradation -------------------------------------------------


def test_broken_store_never_fails_build(tmp_path):
    """Every store call raises; the build still succeeds by running the work."""
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    spawns = Spawns()
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=tmp_path), store=Broken()
    ).build(reporter=spawns)
    assert result.success
    assert spawns.labels  # a store miss means the work really ran
    assert (tmp_path / "build/all.txt").read_text() == "XY"


def test_store_that_fails_only_on_write(tmp_path):
    """A populate failure is swallowed; the build's outputs are correct."""

    class WriteFails(MemoryStore):
        def store(self, fingerprint, result, outputs):
            raise OSError("disk full")

    write_workspace(tmp_path, {"a": "x"})
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=tmp_path), store=WriteFails()
    ).build()
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "X"


# -- two-tier ----------------------------------------------------------------


def _seed_back(tmp_path, back_dir):
    """Populate a back store by building once in a throwaway workspace."""
    seed = tmp_path / "seed"
    seed.mkdir()
    write_workspace(seed, {"a": "x", "b": "y"})
    assert CacheBuilder(
        IncrementalBuilder(make_graph, root=seed), store=CacheStore(back_dir)
    ).build().success


def test_two_tier_back_hit_promotes_to_front(tmp_path):
    """A back hit serves the build and is copied into the front, so the next
    machine-local build needs no back round-trip."""
    back_dir, front_dir = tmp_path / "back", tmp_path / "front"
    _seed_back(tmp_path, back_dir)

    # First build: front empty, so the entries come from the back tier.
    b = tmp_path / "b"
    b.mkdir()
    write_workspace(b, {"a": "x", "b": "y"})
    back = Counting(CacheStore(back_dir))
    spawns = Spawns()
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=b),
        store=TwoTierCacheStore(CacheStore(front_dir), back),
    ).build(reporter=spawns)
    assert result.success
    assert spawns.labels == []  # served entirely from cache (back tier)
    assert back.loads > 0  # the back was consulted
    assert (b / "build/all.txt").read_text() == "XY"
    assert any(front_dir.rglob("entry.json"))  # promoted into the front

    # Second build: front is now warm, the back must not be touched at all.
    c = tmp_path / "c"
    c.mkdir()
    write_workspace(c, {"a": "x", "b": "y"})
    back2 = Counting(CacheStore(back_dir))
    spawns2 = Spawns()
    result2 = CacheBuilder(
        IncrementalBuilder(make_graph, root=c),
        store=TwoTierCacheStore(CacheStore(front_dir), back2),
    ).build(reporter=spawns2)
    assert result2.success
    assert spawns2.labels == []
    assert back2.loads == 0  # front served everything; no back round-trip
    assert (c / "build/all.txt").read_text() == "XY"


def test_two_tier_store_writes_both_tiers(tmp_path):
    """A real run populates front and back together."""
    back_dir, front_dir = tmp_path / "back", tmp_path / "front"
    write_workspace(tmp_path, {"a": "x"})
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=tmp_path),
        store=TwoTierCacheStore(CacheStore(front_dir), CacheStore(back_dir)),
    ).build()
    assert result.success
    assert any(front_dir.rglob("entry.json"))
    assert any(back_dir.rglob("entry.json"))


def test_two_tier_back_down_still_caches_front(tmp_path):
    """With the back unreachable, the front is still populated and served."""
    front_dir = tmp_path / "front"
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        write_workspace(root, {"a": "x"})

    # Build with a broken back: front should still get the entries.
    assert CacheBuilder(
        IncrementalBuilder(make_graph, root=first),
        store=TwoTierCacheStore(CacheStore(front_dir), Broken()),
    ).build().success
    assert any(front_dir.rglob("entry.json"))

    # A second workspace hits the warm front despite the back being down.
    spawns = Spawns()
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=second),
        store=TwoTierCacheStore(CacheStore(front_dir), Broken()),
    ).build(reporter=spawns)
    assert result.success
    assert spawns.labels == []
    assert (second / "build/all.txt").read_text() == "X"
