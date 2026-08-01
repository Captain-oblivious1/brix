"""Metadata-ledger eviction: bound the incremental store by builds-since-seen.

Like the artifact cache, this is **off by default** and **safe by
construction** — a dropped record only forces a recompute on that action's next
build, never a wrong result. Recency is by *build*: each build stamps the keys
in its fully expanded graph with a monotonic epoch (in a ``_seen`` side map),
and a key unstamped for ``keep_builds`` builds is evicted, together with its
discovered deps; dead file-hash entries are GC'd too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brix import CacheBuilder, IncrementalBuilder
from brix.metadata import compact_metadata

from test_cache_store import Spawns
from test_incremental import make_graph, runs, write_workspace


# -- fixtures ----------------------------------------------------------------


def _rec(fp: str = "deadbeef") -> dict:
    return {"fingerprint": fp, "ok": True, "outputs": {}, "result": {}}


def _write_ledger(path: Path, **sections) -> None:
    data = {
        "version": 1,
        "files": sections.get("files", {}),
        "actions": sections.get("actions", {}),
        "memberships": sections.get("memberships", {}),
        "deps": sections.get("deps", {}),
    }
    if "seen" in sections:
        data["_seen"] = sections["seen"]
    if "epoch" in sections:
        data["epoch"] = sections["epoch"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# -- compact_metadata: the core policy --------------------------------------


def test_absent_action_pruned_current_kept(tmp_path):
    led = tmp_path / "metadata.json"
    _write_ledger(led, actions={"A": _rec(), "B": _rec()})

    reclaimed = compact_metadata(
        led, current_actions={"A"}, current_memberships=set(), keep_builds=1
    )

    data = _load(led)
    assert reclaimed == 1
    assert set(data["actions"]) == {"A"}  # B (not in the current graph) evicted
    assert data["epoch"] == 1
    assert data["_seen"]["A"] == 1


def test_absent_record_ages_out_over_keep_builds(tmp_path):
    led = tmp_path / "metadata.json"
    _write_ledger(led, actions={"A": _rec(), "B": _rec()})
    # B is never current; with keep_builds=3 it must survive two builds, go on the third.
    survivals = []
    for _ in range(3):
        compact_metadata(led, current_actions={"A"}, current_memberships=set(), keep_builds=3)
        survivals.append("B" in _load(led)["actions"])
    assert survivals == [True, True, False]
    assert "A" in _load(led)["actions"]  # current every build → never evicted


def test_current_record_never_pruned_however_old(tmp_path):
    led = tmp_path / "metadata.json"
    _write_ledger(led, actions={"A": _rec()})
    for _ in range(20):
        compact_metadata(led, current_actions={"A"}, current_memberships=set(), keep_builds=1)
    assert "A" in _load(led)["actions"]


def test_consistent_gc_of_deps_and_memberships(tmp_path):
    led = tmp_path / "metadata.json"
    _write_ledger(
        led,
        actions={"A": _rec(), "B": _rec()},
        deps={"A": ["x"], "B": ["y"]},
        memberships={"M1": ["a"], "M2": ["b"]},
    )

    compact_metadata(led, current_actions={"A"}, current_memberships={"M1"}, keep_builds=1)

    data = _load(led)
    assert set(data["actions"]) == {"A"}
    assert set(data["deps"]) == {"A"}  # B's discovered deps went with it
    assert set(data["memberships"]) == {"M1"}  # M2 (absent collection) evicted


def test_dead_file_entries_pruned_live_kept(tmp_path):
    led = tmp_path / "metadata.json"
    alive = tmp_path / "real.txt"
    alive.write_text("here")
    _write_ledger(
        led,
        actions={"A": _rec()},
        files={str(alive): {"sha256": "h"}, str(tmp_path / "gone.txt"): {"sha256": "z"}},
    )

    compact_metadata(led, current_actions={"A"}, current_memberships=set(), keep_builds=1)

    files = _load(led)["files"]
    assert str(alive) in files  # on-disk path keeps its cached hash
    assert str(tmp_path / "gone.txt") not in files  # vanished path is dead weight


def test_seen_index_forgets_pruned_keys(tmp_path):
    led = tmp_path / "metadata.json"
    _write_ledger(led, actions={"A": _rec(), "B": _rec()})
    compact_metadata(led, current_actions={"A"}, current_memberships=set(), keep_builds=1)
    seen = _load(led)["_seen"]
    assert set(seen) == {"A"}  # no dangling seen entry for the evicted B


def test_foreign_version_left_untouched(tmp_path):
    led = tmp_path / "metadata.json"
    led.write_text(json.dumps({"version": 999, "actions": {"A": _rec()}}))
    before = led.read_text()
    assert compact_metadata(led, current_actions=set(), current_memberships=set(), keep_builds=1) == 0
    assert led.read_text() == before


def test_missing_ledger_is_a_noop(tmp_path):
    assert compact_metadata(
        tmp_path / "nope.json", current_actions=set(), current_memberships=set(), keep_builds=1
    ) == 0


# -- builder wiring ----------------------------------------------------------


def test_off_by_default_no_epoch_or_seen(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    assert IncrementalBuilder(make_graph, root=tmp_path).build().success
    data = _load(tmp_path / "build/.brix/metadata.json")
    assert "epoch" not in data and "_seen" not in data  # untouched without a bound


def test_builder_prunes_injected_stale_record(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    led = tmp_path / "build/.brix/metadata.json"

    # First build populates the ledger with this graph's real records.
    assert IncrementalBuilder(make_graph, root=tmp_path).build().success
    data = _load(led)
    real_keys = set(data["actions"])
    assert real_keys  # something got recorded
    # Inject a stale record from an imaginary past build.
    data["actions"]["STALE" + "0" * 59] = _rec("old")
    led.write_text(json.dumps(data))

    # A bounded rebuild stamps the current graph and evicts the stranger.
    result = IncrementalBuilder(make_graph, root=tmp_path, keep_builds=1).build()
    assert result.success
    actions = _load(led)["actions"]
    assert not any(k.startswith("STALE") for k in actions)  # stale pruned
    assert real_keys <= set(actions)  # every current record survived


def test_keep_builds_below_one_rejected(tmp_path):
    with pytest.raises(ValueError):
        IncrementalBuilder(make_graph, root=tmp_path, keep_builds=0)


def test_current_records_survive_repeated_bounded_builds(tmp_path):
    """The tightest bound (keep_builds=1) must still never evict a record the
    current build depends on — otherwise every build would recompute."""
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    led = tmp_path / "build/.brix/metadata.json"
    builder = lambda: IncrementalBuilder(make_graph, root=tmp_path, keep_builds=1)

    assert builder().build().success
    keys = set(_load(led)["actions"])
    # Rebuild twice more; the record set is stable (nothing current is lost).
    for _ in range(2):
        spawns = Spawns()
        assert builder().build(reporter=spawns).success
        assert spawns.labels == []  # fully incremental — records were kept, not re-run
    assert set(_load(led)["actions"]) == keys


# -- soundness: eviction never yields a wrong build --------------------------


def test_compact_then_rebuild_equals_clean(tmp_path):
    """Evict the whole ledger, then rebuild — a pruned record only forces a
    recompute, and the recomputed output is correct."""
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    led = tmp_path / "build/.brix/metadata.json"
    assert CacheBuilder(
        IncrementalBuilder(make_graph, root=tmp_path), cache_dir=tmp_path / ".nocache-x"
    ).build().success

    # Simulate a bound so aggressive it drops everything (nothing "current").
    compact_metadata(led, current_actions=set(), current_memberships=set(), keep_builds=1)
    assert _load(led)["actions"] == {}  # ledger emptied

    spawns = Spawns()
    result = IncrementalBuilder(make_graph, root=tmp_path).build(reporter=spawns)
    assert result.success
    assert spawns.labels  # the emptied ledger forced real recomputation
    assert (tmp_path / "build/all.txt").read_text() == "XY"  # and it is correct
