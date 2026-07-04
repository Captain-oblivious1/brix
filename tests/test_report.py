"""The reporter seam: the engine never prints; reporters render events."""

import io
import sys

from brix import (
    CleanBuilder,
    ConsoleReporter,
    ErrorsAtEndReporter,
    ExpandAll,
    ExternalProcessAction,
    Graph,
    IncrementalBuilder,
    Reporter,
)


def _upper_graph():
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
                outputs={"dest": data.file("build/{name}.txt")},
            ),
        )
    )
    return graph


def _write_workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.txt").write_text("hi")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/upper.py").write_text(
        "import sys\n"
        "with open(sys.argv[1]) as f: text = f.read()\n"
        "with open(sys.argv[2], 'w') as f: f.write(text.upper())\n"
        "print('worked on', sys.argv[1])\n"
    )


def _failing_graph():
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="boom",
            command_line="{python} -c {code}",
            inputs={
                "python": data.tool(sys.executable),
                "code": data.value("'import sys; print(\"exploding\", file=sys.stderr); sys.exit(9)'"),
            },
        )
    )
    return graph


def test_default_is_silent(tmp_path, capsys):
    _write_workspace(tmp_path)
    result = CleanBuilder(_upper_graph(), root=tmp_path).build()
    assert result.success
    assert capsys.readouterr().out == ""
    # Exit-code-only clients need nothing but the result.
    assert (0 if result.success else 1) == 0


def test_failure_is_silent_too_but_result_tells_all(tmp_path, capsys):
    result = CleanBuilder(_failing_graph(), root=tmp_path).build()
    assert not result.success
    assert capsys.readouterr().out == ""
    (proc,) = result.process_results
    assert proc.exit_code == 9
    assert "exploding" in proc.stderr


def test_recording_reporter_sees_the_whole_lifecycle(tmp_path):
    class RecordingReporter(Reporter):
        def __init__(self):
            self.events = []

        def build_begin(self, plan):
            self.events.append("begin")

        def node_started(self, node):
            self.events.append(f"started: {node.label}")

        def node_completed(self, node, status, result, error=None):
            self.events.append(f"completed: {node.label} ({status.value})")

        def collection_realized(self, node, members, children):
            self.events.append(f"realized: {members}")

        def build_end(self, result):
            self.events.append(f"end: ok={result.success}")

    _write_workspace(tmp_path)
    reporter = RecordingReporter()
    result = CleanBuilder(_upper_graph(), root=tmp_path).build(reporter=reporter)
    assert result.success
    assert reporter.events[0] == "begin"
    assert reporter.events[-1] == "end: ok=True"
    assert "realized: ['a']" in reporter.events
    assert "started: upper a" in reporter.events
    assert "completed: upper a (done)" in reporter.events


def test_console_reporter_marks_replays(tmp_path):
    _write_workspace(tmp_path)
    builder = IncrementalBuilder(_upper_graph, root=tmp_path)

    out1 = io.StringIO()
    first = builder.build(reporter=ConsoleReporter(stream=out1))
    assert first.success
    assert "ok: upper a\n" in out1.getvalue()
    assert "(replayed)" not in out1.getvalue()

    out2 = io.StringIO()
    second = builder.build(reporter=ConsoleReporter(stream=out2))
    assert second.success
    assert "ok: upper a (replayed)" in out2.getvalue()
    assert "worked on src/a.txt" in out2.getvalue()  # stdout replay still shown


def test_console_reporter_can_hide_replays(tmp_path):
    _write_workspace(tmp_path)
    builder = IncrementalBuilder(_upper_graph, root=tmp_path)
    builder.build()

    out = io.StringIO()
    second = builder.build(reporter=ConsoleReporter(stream=out, show_replays=False))
    assert second.success
    text = out.getvalue()
    assert "upper a" not in text  # fresh work only; nothing was fresh work
    assert "BUILD OK" in text


def test_result_provenance(tmp_path):
    _write_workspace(tmp_path)
    builder = IncrementalBuilder(_upper_graph, root=tmp_path)
    first = builder.build()
    assert all(r.source == "process" for r in first.process_results)
    second = builder.build()
    assert all(r.source == "replayed" for r in second.process_results)


def test_errors_at_end_reporter(tmp_path):
    out = io.StringIO()
    result = CleanBuilder(_failing_graph(), root=tmp_path).build(
        reporter=ErrorsAtEndReporter(stream=out)
    )
    assert not result.success
    text = out.getvalue()
    assert "FAILED (exit 9): boom" in text
    assert "exploding" in text
    assert "BUILD FAILED" in text


def test_errors_at_end_reporter_is_quiet_on_success(tmp_path):
    _write_workspace(tmp_path)
    out = io.StringIO()
    result = CleanBuilder(_upper_graph(), root=tmp_path).build(
        reporter=ErrorsAtEndReporter(stream=out)
    )
    assert result.success
    assert out.getvalue() == "BUILD OK (2 done)\n"
