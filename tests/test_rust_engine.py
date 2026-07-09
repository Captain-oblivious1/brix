"""BRIX_ENGINE=rust: builds cross the PyO3 boundary and come back equal.

The full parity check is the whole suite run with BRIX_ENGINE=rust set;
these tests pin the dispatch itself — when the Rust engine is used, when
it silently falls back to Python, and when a missing extension must be
loud. Skipped entirely when the extension is not built
(`cargo build -p brix-py`).
"""

import sys
from collections import Counter

import pytest

from brix import CacheBuilder, IncrementalBuilder, NodeState
from brix import engine

pytestmark = pytest.mark.skipif(
    engine._extension() is None,
    reason="brix_py extension not built (cargo build -p brix-py)",
)

UPPER_TOOL = """\
import sys
from pathlib import Path
Path("runs.log").open("a").write("upper:" + sys.argv[1] + "\\n")
text = Path(sys.argv[1]).read_text()
Path(sys.argv[2]).write_text(text.upper())
print("uppercased", sys.argv[1])
"""


def make_graph():
    from brix import ExpandAll, ExternalProcessAction, Graph

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
        )
    )
    return graph


def write_workspace(tmp_path, sources):
    (tmp_path / "src").mkdir(exist_ok=True)
    for name, text in sources.items():
        (tmp_path / "src" / f"{name}.txt").write_text(text)
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools/upper.py").write_text(UPPER_TOOL)


def runs(tmp_path) -> Counter:
    log = tmp_path / "runs.log"
    if not log.exists():
        return Counter()
    return Counter(log.read_text().splitlines())


class RecordingReporter:
    def __init__(self):
        self.calls = []

    def build_begin(self, plan):
        self.calls.append(("build_begin", [n.label for n in plan.nodes]))

    def node_started(self, node):
        self.calls.append(("node_started", node.label))

    def process_started(self, node):
        self.calls.append(("process_started", node.label))

    def node_completed(self, node, status, result, error=None):
        self.calls.append(("node_completed", node.label, status, error))

    def node_skipped(self, node, why):
        self.calls.append(("node_skipped", node.label))

    def collection_realized(self, node, members, children):
        self.calls.append(("collection_realized", node.label, list(members)))

    def node_inserted(self, node, reason):
        self.calls.append(("node_inserted", node.label))

    def build_end(self, result):
        self.calls.append(("build_end", result.success))


def test_rust_engine_builds_and_replays(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")
    write_workspace(tmp_path, {"a": "hi ", "b": "there"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)

    first = builder.build()
    assert first.success
    assert (tmp_path / "build/upper/a.txt").read_text() == "HI "
    assert (tmp_path / "build/upper/b.txt").read_text() == "THERE"

    second = builder.build()
    assert second.success
    assert runs(tmp_path) == Counter({"upper:src/a.txt": 1, "upper:src/b.txt": 1})
    assert all(r.source == "replayed" for r in second.process_results)
    process_nodes = [n for n in second.plan.nodes if n.kind.value == "process"]
    assert process_nodes  # the final plan includes the expanded children
    assert all(
        second.states.status(n.id) is NodeState.DONE for n in second.plan.nodes
    )


def test_rust_engine_delivers_events(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")
    write_workspace(tmp_path, {"a": "x"})
    reporter = RecordingReporter()
    result = IncrementalBuilder(make_graph, root=tmp_path).build(reporter=reporter)

    kinds = [c[0] for c in reporter.calls]
    assert kinds[0] == "build_begin"
    assert kinds[-1] == "build_end"
    assert reporter.calls[-1] == ("build_end", True)
    realized = [c for c in reporter.calls if c[0] == "collection_realized"]
    assert realized and realized[0][2] == ["a"]
    completed = [c for c in reporter.calls if c[0] == "node_completed"]
    assert ("node_completed", "upper a", NodeState.DONE, None) in completed
    assert result.success


def test_reporter_exception_reraised(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")
    write_workspace(tmp_path, {"a": "x"})

    class Exploding(RecordingReporter):
        def node_completed(self, node, status, result, error=None):
            raise RuntimeError("reporter boom")

    with pytest.raises(RuntimeError, match="reporter boom"):
        IncrementalBuilder(make_graph, root=tmp_path).build(reporter=Exploding())


def test_python_engine_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIX_ENGINE", raising=False)
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    assert engine.maybe_build(builder) is None


def test_identity_graph_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")

    def graph_with_identity():
        from brix import ExternalProcessAction, Graph

        graph = Graph()
        graph.add(
            ExternalProcessAction(
                name="jar",
                command_line="{python} {f} {out}",
                inputs={
                    "python": graph.data.tool(sys.executable),
                    "f": graph.data.file("x.jar", identity="jvm-abi"),
                },
                outputs={"out": graph.data.file("build/out")},
            )
        )
        return graph

    builder = IncrementalBuilder(graph_with_identity, root=tmp_path)
    assert engine.maybe_build(builder) is None  # Python engine runs it


def test_custom_store_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")

    class NullStore:
        def load(self, fingerprint):
            return None

        def restore(self, fingerprint, entry, output_root):
            return False

        def store(self, fingerprint, result, outputs):
            pass

    builder = CacheBuilder(
        IncrementalBuilder(make_graph, root=tmp_path), store=NullStore()
    )
    assert builder.layer_specs() is None
    assert engine.maybe_build(builder) is None


def test_missing_extension_is_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")
    monkeypatch.setattr(engine, "_extension_cache", None)
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    with pytest.raises(RuntimeError, match="brix_py extension was not found"):
        engine.maybe_build(builder)
