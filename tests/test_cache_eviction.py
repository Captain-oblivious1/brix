"""Artifact-cache eviction: bound the built-in `CacheStore` by size / age.

Eviction is **off by default** and **safe by construction** — every entry is
reproducible, so a pruned entry becomes a recompute on its next build, never a
wrong result. Recency is LRU by *use*: a cache hit refreshes an entry's mtime
(coarsened), so a hot dependency outlives a cold one under a size cap.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from brix import CacheBuilder, CacheStore, IncrementalBuilder
from brix.cache import _TOUCH_COARSENING_SECONDS
from brix.layers import NodeContext
from brix.process import ProcessResult

from test_cache_store import Spawns
from test_incremental import make_graph, write_workspace


# -- helpers -----------------------------------------------------------------


def _fp(n: int) -> str:
    """A distinct 64-hex fingerprint (valid for the <fp[:2]>/<fp> sharding)."""
    return f"{n:064x}"


def _seed(store: CacheStore, fp: str, payload: bytes, tmp: Path) -> Path:
    """Populate one entry with a single output blob; return its entry dir."""
    source = tmp / f"src-{fp[:8]}"
    source.write_bytes(payload)
    store.store(fp, ProcessResult("act", "cmd", exit_code=0), {"out.txt": source})
    return store._entry_dir(fp)


def _age(entry_dir: Path, seconds_ago: float) -> None:
    """Backdate an entry's recency marker (entry.json mtime)."""
    when = time.time() - seconds_ago
    os.utime(entry_dir / "entry.json", (when, when))


def _entry_dirs(cache_dir: Path) -> list[Path]:
    return [p.parent for p in cache_dir.rglob("entry.json")]


# -- prune: size cap ---------------------------------------------------------


def test_prune_max_bytes_evicts_oldest_first(tmp_path):
    store = CacheStore(tmp_path / "cache")
    # Five equal-size entries, each older than the next.
    for i in range(5):
        entry = _seed(store, _fp(i), b"x" * 1000, tmp_path)
        _age(entry, seconds_ago=(5 - i) * 10_000)  # fp(0) oldest ... fp(4) newest
    one = sum(f.stat().st_size for f in (store._entry_dir(_fp(0))).rglob("*") if f.is_file())

    store.prune(max_bytes=one * 2)  # room for ~2 entries

    survivors = {p.name for p in _entry_dirs(store.directory)}
    assert survivors == {_fp(3), _fp(4)}  # the two newest survived


def test_prune_off_by_default_keeps_everything(tmp_path):
    """A build with no bound never evicts, even a tiny cache full of entries."""
    cache = tmp_path / "cache"
    store = CacheStore(cache)
    for i in range(3):
        _seed(store, _fp(i), b"z" * 100_000, tmp_path)

    ws = tmp_path / "ws"
    ws.mkdir()
    write_workspace(ws, {"a": "x"})
    assert CacheBuilder(IncrementalBuilder(make_graph, root=ws), cache_dir=cache).build().success

    assert len(_entry_dirs(cache)) >= 3  # nothing was pruned


# -- prune: TTL --------------------------------------------------------------


def test_prune_max_age_evicts_stale_only(tmp_path):
    store = CacheStore(tmp_path / "cache")
    fresh = _seed(store, _fp(1), b"a" * 500, tmp_path)
    stale = _seed(store, _fp(2), b"b" * 500, tmp_path)
    _age(fresh, seconds_ago=10)
    _age(stale, seconds_ago=100_000)

    store.prune(max_age=3600)

    survivors = {p.name for p in _entry_dirs(store.directory)}
    assert survivors == {_fp(1)}  # only the stale entry went


# -- LRU by use --------------------------------------------------------------


def test_hit_refreshes_recency_so_size_cap_keeps_the_hot_entry(tmp_path):
    """A restore (cache hit) on the *older* entry makes it the *newest*, so a
    size cap then evicts the one that was not used instead."""
    store = CacheStore(tmp_path / "cache")
    hot = _seed(store, _fp(1), b"h" * 800, tmp_path)
    cold = _seed(store, _fp(2), b"c" * 800, tmp_path)
    # Start both old (older than the touch-coarsening window) with hot older.
    _age(hot, seconds_ago=_TOUCH_COARSENING_SECONDS + 50_000)
    _age(cold, seconds_ago=_TOUCH_COARSENING_SECONDS + 10_000)

    # Use the hot entry: restore refreshes its mtime to ~now.
    out = tmp_path / "out"
    out.mkdir()
    assert store.restore(_fp(1), store.load(_fp(1)), NodeContext.for_root(out))

    one = sum(f.stat().st_size for f in hot.rglob("*") if f.is_file())
    store.prune(max_bytes=one + one // 2)  # room for one entry

    survivors = {p.name for p in _entry_dirs(store.directory)}
    assert survivors == {_fp(1)}  # the used (hot) entry survived; the cold one went


def test_hit_within_coarsening_window_skips_the_write(tmp_path):
    """A just-written entry is not re-touched on the next hit (no inode churn)."""
    store = CacheStore(tmp_path / "cache")
    entry = _seed(store, _fp(1), b"k" * 100, tmp_path)
    before = (entry / "entry.json").stat().st_mtime

    out = tmp_path / "out"
    out.mkdir()
    store.restore(_fp(1), store.load(_fp(1)), NodeContext.for_root(out))

    assert (entry / "entry.json").stat().st_mtime == before  # unchanged


# -- soundness: eviction never yields a wrong build --------------------------


def test_prune_then_rebuild_equals_clean(tmp_path):
    """Evict the whole cache, then a fresh checkout rebuilds correctly — a
    pruned entry only ever forces a recompute, never a wrong or missing one."""
    cache = tmp_path / "cache"
    first = tmp_path / "one"
    first.mkdir()
    write_workspace(first, {"a": "x", "b": "y"})
    assert CacheBuilder(IncrementalBuilder(make_graph, root=first), cache_dir=cache).build().success

    CacheStore(cache).prune(max_bytes=0)  # evict everything
    assert _entry_dirs(cache) == []

    second = tmp_path / "two"
    second.mkdir()
    write_workspace(second, {"a": "x", "b": "y"})
    spawns = Spawns()
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=second), cache_dir=cache
    ).build(reporter=spawns)
    assert result.success
    assert spawns.labels  # the empty cache forced real recomputation
    assert (second / "build/all.txt").read_text() == "XY"  # and it is correct


# -- builder wiring ----------------------------------------------------------


def test_builder_max_bytes_caps_store_after_build(tmp_path):
    """A bounded CacheBuilder prunes the shared dir once the build finishes."""
    cache = tmp_path / "cache"
    store = CacheStore(cache)
    for i in range(6):
        entry = _seed(store, _fp(i), b"q" * 50_000, tmp_path)
        _age(entry, seconds_ago=(6 - i) * 1000)
    before = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())

    ws = tmp_path / "ws"
    ws.mkdir()
    write_workspace(ws, {"a": "x"})
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=ws), cache_dir=cache, max_bytes=120_000
    ).build()
    assert result.success

    after = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
    assert after < before
    assert after <= 120_000 + 60_000  # under the cap (+ this build's own entries)


def test_custom_store_with_bound_rejected(tmp_path):
    from test_cache_store import MemoryStore

    with pytest.raises(ValueError):
        CacheBuilder(
            IncrementalBuilder(make_graph, root=tmp_path),
            store=MemoryStore(),
            max_bytes=1_000,
        )


# -- robustness --------------------------------------------------------------


def test_prune_ignores_tmp_and_incomplete_entries(tmp_path):
    """Half-written staging dirs and entries without entry.json are left be —
    another build may own them; prune must not crash or claim them."""
    cache = tmp_path / "cache"
    store = CacheStore(cache)
    good = _seed(store, _fp(1), b"g" * 1000, tmp_path)
    _age(good, seconds_ago=100_000)

    (cache / ".tmp-999-abcd" / "files").mkdir(parents=True)  # in-flight store()
    incomplete = cache / _fp(2)[:2] / _fp(2)
    (incomplete / "files").mkdir(parents=True)  # entry dir with no entry.json

    freed = store.prune(max_bytes=0)  # try to evict everything

    assert freed > 0  # the good entry went
    assert (cache / ".tmp-999-abcd").exists()  # staging left alone
    assert incomplete.exists()  # incomplete entry left alone
