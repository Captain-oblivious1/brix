"""Persistent workers: a worker-routed action is indistinguishable from a
spawned one (results, provenance, caching, liveness), and failures degrade
correctly (crash / startup failure -> exit_code=None, never cached, no
silent fallback to spawning)."""

import sys
from pathlib import Path

from brix import (
    CacheBuilder,
    CleanBuilder,
    ExternalProcessAction,
    Graph,
    IncrementalBuilder,
    NodeContext,
    Reporter,
    TimeAndHashChangeDetector,
    WorkerSpec,
)
from brix.incremental import action_fingerprint

WORKERS = Path(__file__).parent / "workers"
EXEC_WORKER = WORKERS / "exec_worker.py"
CRASH_WORKER = WORKERS / "crash_worker.py"

UPPER = "import sys; open(sys.argv[2],'w').write(open(sys.argv[1]).read().upper())\n"


class Started(Reporter):
    """Records process_started — fires on a real spawn *or* a worker dispatch,
    never on a replay/cache hit."""

    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


def workspace(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/a.txt").write_text("hi")
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools/upper.py").write_text(UPPER)


def upper_graph(use_worker: bool, worker_script: Path = EXEC_WORKER) -> Graph:
    graph = Graph()
    d = graph.data
    spec = (
        WorkerSpec(
            name="exec",
            command_line="{py} {w}",
            inputs={"py": d.tool(sys.executable), "w": d.file(str(worker_script))},
        )
        if use_worker
        else None
    )
    graph.add(
        ExternalProcessAction(
            name="upper a",
            worker=spec,
            command_line="{py} {tool} {src} {dst}",
            inputs={
                "py": d.tool(sys.executable),
                "tool": d.file("tools/upper.py"),
                "src": d.file("src/a.txt"),
            },
            outputs={"dst": d.file("build/a.txt")},
        )
    )
    return graph


def test_worker_output_identical_to_spawning(tmp_path):
    spawned, worked = tmp_path / "spawn", tmp_path / "work"
    for root in (spawned, worked):
        workspace(root)

    spawn_result = CleanBuilder(upper_graph(False), root=spawned).build()
    started = Started()
    worker_result = CleanBuilder(upper_graph(True), root=worked).build(reporter=started)

    assert spawn_result.success and worker_result.success
    assert (spawned / "build/a.txt").read_text() == "HI"
    assert (worked / "build/a.txt").read_text() == "HI"
    # Indistinguishable: same provenance, and the liveness signal still fires.
    assert all(r.source == "process" for r in worker_result.process_results)
    assert started.labels == ["upper a"]


def test_worker_result_is_cacheable(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    cache = tmp_path / "cache"
    for root in (first, second):
        workspace(root)

    assert (
        CacheBuilder(
            IncrementalBuilder(lambda: upper_graph(True), root=first), cache_dir=cache
        )
        .build()
        .success
    )
    started = Started()
    result = CacheBuilder(
        IncrementalBuilder(lambda: upper_graph(True), root=second), cache_dir=cache
    ).build(reporter=started)
    assert result.success
    assert started.labels == []  # cache hit: no worker dispatched
    assert all(r.source == "cache" for r in result.process_results)
    assert (second / "build/a.txt").read_text() == "HI"


def test_worker_crash_is_uncacheable_failure(tmp_path):
    workspace(tmp_path)
    graph = Graph()
    d = graph.data
    spec = WorkerSpec(
        name="crash",
        command_line="{py} {w}",
        inputs={"py": d.tool(sys.executable), "w": d.file(str(CRASH_WORKER))},
    )
    graph.add(
        ExternalProcessAction(
            name="boom",
            worker=spec,
            command_line="{py} {tool} {src} {dst}",
            inputs={
                "py": d.tool(sys.executable),
                "tool": d.file("tools/upper.py"),
                "src": d.file("src/a.txt"),
            },
            outputs={"dst": d.file("build/a.txt")},
        )
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert not result.success
    (r,) = result.process_results
    # A crash is not the action's deterministic answer: exit_code None is what
    # keeps the cache layer from ever storing it (same rule as a signal kill).
    assert r.exit_code is None
    assert "crashed" in r.stderr


def test_worker_startup_failure_no_spawn_fallback(tmp_path):
    workspace(tmp_path)
    graph = Graph()
    d = graph.data
    spec = WorkerSpec(
        name="missing",
        command_line="{prog}",
        inputs={"prog": d.tool("/nonexistent/brix-no-such-worker")},
    )
    graph.add(
        ExternalProcessAction(
            name="x",
            worker=spec,
            command_line="{py} {tool} {src} {dst}",
            inputs={
                "py": d.tool(sys.executable),
                "tool": d.file("tools/upper.py"),
                "src": d.file("src/a.txt"),
            },
            outputs={"dst": d.file("build/a.txt")},
        )
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert not result.success
    (r,) = result.process_results
    assert r.exit_code is None
    assert "failed to start" in r.stderr
    assert not (tmp_path / "build/a.txt").exists()  # no quiet fallback to spawning


def test_worker_identity_joins_the_fingerprint(tmp_path):
    workspace(tmp_path)
    ctx = NodeContext.for_root(tmp_path)
    detector = TimeAndHashChangeDetector(tmp_path)

    def fp(use_worker: bool, worker_script: Path = EXEC_WORKER) -> str:
        action = upper_graph(use_worker, worker_script).actions[0]
        return action_fingerprint(action, ctx, detector)

    no_worker = fp(False)
    with_worker = fp(True)
    assert no_worker != with_worker  # the worker section changes the key

    alt = tmp_path / "alt_worker.py"
    alt.write_text("import sys\nsys.stdin.readline()\n")
    assert with_worker != fp(True, alt)  # a different worker jar invalidates too
