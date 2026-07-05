"""CacheBuilder: reuse results across workspaces, keyed by fingerprint."""

import io
import sys

from brix import AtomicBuilder, CacheBuilder, ConsoleReporter, IncrementalBuilder, Reporter

from test_incremental import make_graph, runs, write_workspace


def builder(root, cache_dir):
    return CacheBuilder(IncrementalBuilder(make_graph, root=root), cache_dir=cache_dir)


class Spawns(Reporter):
    """Counts real process spawns — the thing caching is meant to avoid."""

    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


def test_fresh_checkout_restores_from_cache(tmp_path):
    cache = tmp_path / "shared-cache"
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        write_workspace(root, {"a": "x", "b": "y"})

    assert builder(first, cache).build().success
    assert runs(first)  # real processes populated the cache

    spawns = Spawns()
    result = builder(second, cache).build(reporter=spawns)
    assert result.success
    assert spawns.labels == []  # nothing ran
    assert runs(second) == {}
    assert (second / "build/all.txt").read_text() == "XY"
    assert all(r.source == "cache" for r in result.process_results)


def test_clean_rebuild_restores_from_cache(tmp_path):
    """The classic payoff: rm -rf outputs + metadata, rebuild instantly."""
    import shutil

    write_workspace(tmp_path, {"a": "x", "b": "y"})
    b = builder(tmp_path, tmp_path / "build/.brix-cache")
    assert b.build().success
    ran_before = runs(tmp_path)

    shutil.rmtree(tmp_path / "build/upper")
    (tmp_path / "build/all.txt").unlink()
    shutil.rmtree(tmp_path / "build/.brix")

    result = b.build()
    assert result.success
    assert runs(tmp_path) == ran_before  # no new processes
    assert (tmp_path / "build/all.txt").read_text() == "XY"
    assert all(r.source == "cache" for r in result.process_results)


def test_incremental_freshness_wins_over_cache(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    b = builder(tmp_path, tmp_path / "cache")
    assert b.build().success
    second = b.build()
    assert second.success
    # Fresh nodes replay from local metadata; the cache is never consulted.
    assert all(r.source == "replayed" for r in second.process_results)


def test_changed_input_misses_then_repopulates(tmp_path):
    cache = tmp_path / "cache"
    write_workspace(tmp_path, {"a": "x"})
    b = builder(tmp_path, cache)
    assert b.build().success

    (tmp_path / "src/a.txt").write_text("z")
    spawns = Spawns()
    assert b.build(reporter=spawns).success
    assert spawns.labels  # miss: the changed content really ran
    assert (tmp_path / "build/all.txt").read_text() == "Z"

    # A fresh checkout with the *new* content now hits the new entry.
    other = tmp_path / "other"
    other.mkdir()
    write_workspace(other, {"a": "z"})
    result = builder(other, cache).build(reporter=(s2 := Spawns()))
    assert result.success and s2.labels == []
    assert (other / "build/all.txt").read_text() == "Z"


def test_cached_failure_replays_without_running(tmp_path):
    from brix import ExternalProcessAction, Graph

    def failing_graph():
        graph = Graph()
        data = graph.data
        graph.add(
            ExternalProcessAction(
                name="boom",
                command_line="{python} {tool}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/boom.py"),
                },
            )
        )
        return graph

    cache = tmp_path / "cache"
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        (root / "tools").mkdir(parents=True)
        (root / "tools/boom.py").write_text(
            "import sys; print('kaboom', file=sys.stderr); sys.exit(7)\n"
        )

    b1 = CacheBuilder(IncrementalBuilder(failing_graph, root=first), cache_dir=cache)
    assert not b1.build().success

    spawns = Spawns()
    b2 = CacheBuilder(IncrementalBuilder(failing_graph, root=second), cache_dir=cache)
    result = b2.build(reporter=spawns)
    assert not result.success
    assert spawns.labels == []  # the failure replayed from cache
    (proc,) = result.process_results
    assert proc.source == "cache"
    assert proc.exit_code == 7
    assert "kaboom" in proc.stderr


def test_cache_hit_stdout_replay_and_provenance_marker(tmp_path):
    cache = tmp_path / "cache"
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        write_workspace(root, {"a": "hi"})
    assert builder(first, cache).build().success

    out = io.StringIO()
    assert builder(second, cache).build(reporter=ConsoleReporter(stream=out)).success
    text = out.getvalue()
    assert "ok: upper a (cache)" in text
    assert "uppercased src/a.txt" in text  # stdout replayed from the entry
    assert "run:" not in text  # no process ever spawned


def test_cache_under_atomic_routes_through_staging(tmp_path):
    """Restored outputs land in staging and only commit with the build."""
    cache = tmp_path / "cache"
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        root.mkdir()
        write_workspace(root, {"a": "x"})

    def full_stack(root):
        return AtomicBuilder(
            CacheBuilder(
                IncrementalBuilder(make_graph, root=root), cache_dir=cache
            ),
            staging_dir="build/staging",
        )

    assert full_stack(first).build().success

    class StagingWatcher(Spawns):
        def __init__(self, root):
            super().__init__()
            self.root = root
            self.seen_in_staging = False

        def node_completed(self, node, status, result, error=None):
            staged = self.root / "build/staging/add/build"
            if staged.exists() and any(staged.rglob("*")):
                self.seen_in_staging = True

    watcher = StagingWatcher(second)
    result = full_stack(second).build(reporter=watcher)
    assert result.success
    assert watcher.labels == []  # all cache hits
    assert watcher.seen_in_staging  # restored outputs went via staging
    assert (second / "build/all.txt").read_text() == "X"  # then committed
    assert not (second / "build/staging").exists()
