"""Cross-build worker daemons through the full Python boundary
(docs/java-fast-builds.md v2): a second build adopts the first build's warm
worker; the suite-wide default (conftest.py) keeps daemons off everywhere
else. Rust engine only — under BRIX_ENGINE=py workers die at build end."""

import os
import signal
import sys
from pathlib import Path

import pytest

from brix import IncrementalBuilder
from brix import engine

pytestmark = pytest.mark.skipif(
    engine._extension() is None
    or not (Path(engine._extension().__file__).parent / "brix-workerd").is_file(),
    reason="brix_py extension or brix-workerd not built",
)

# Writes the requested output file and answers with its own pid, so
# adoption is provable by pid equality across builds.
PID_WORKER = """\
import json, os, sys
from pathlib import Path
for line in sys.stdin:
    req = json.loads(line)
    Path(req["cwd"], req["args"][0]).parent.mkdir(parents=True, exist_ok=True)
    Path(req["cwd"], req["args"][0]).write_text("done")
    print(json.dumps({"id": req["id"], "exitCode": 0,
                      "stdout": str(os.getpid()), "stderr": ""}), flush=True)
"""


def make_graph():
    from brix import ExternalProcessAction, Graph, WorkerSpec

    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="via worker",
            command_line="{out}",
            worker=WorkerSpec(
                name="pidw",
                command_line="{python} {script}",
                inputs={
                    "python": data.tool(sys.executable),
                    "script": data.file("tools/pid_worker.py"),
                },
            ),
            inputs={},
            outputs={"out": data.file("build/out.txt")},
        )
    )
    return graph


def kill_daemons(root: Path):
    base = root / "build/.brix/workers"
    if not base.is_dir():
        return
    for pointer in base.rglob("*.sock"):  # pointer files: content = socket path
        socket = Path(pointer.read_text().strip())
        pidfile = socket.with_suffix(".pid")
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        for leftover in (socket, pidfile, socket.with_suffix(".stderr")):
            leftover.unlink(missing_ok=True)


def worker_pid(result) -> str:
    (r,) = [r for r in result.process_results if r.source == "process"]
    return r.stdout.strip()


def test_second_build_adopts_the_warm_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/pid_worker.py").write_text(PID_WORKER)
    try:
        first = IncrementalBuilder(make_graph, root=tmp_path).build(worker_idle_timeout=60)
        assert first.success, first.errors
        # Force a rerun without touching the worker's inputs (its fingerprint
        # is the registry key): delete the output.
        (tmp_path / "build/out.txt").unlink()
        second = IncrementalBuilder(make_graph, root=tmp_path).build(worker_idle_timeout=60)
        assert second.success, second.errors
        assert worker_pid(first) == worker_pid(second)  # the same warm process
    finally:
        kill_daemons(tmp_path)


def test_timeout_zero_means_no_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIX_ENGINE", "rust")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/pid_worker.py").write_text(PID_WORKER)
    first = IncrementalBuilder(make_graph, root=tmp_path).build(worker_idle_timeout=0)
    assert first.success, first.errors
    assert not (tmp_path / "build/.brix/workers").exists()  # no registry at all
    (tmp_path / "build/out.txt").unlink()
    second = IncrementalBuilder(make_graph, root=tmp_path).build(worker_idle_timeout=0)
    assert second.success, second.errors
    assert worker_pid(first) != worker_pid(second)  # v1: died at build end
