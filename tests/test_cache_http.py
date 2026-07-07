"""HttpCacheStore: a remote cache backend over plain GET/PUT.

Runs a real in-process HTTP server backed by a temp directory, so the store is
exercised over the wire (sockets, status codes, headers) rather than mocked.
"""

import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brix import (
    CacheBuilder,
    CacheStore,
    HttpCacheStore,
    IncrementalBuilder,
    NodeContext,
    Reporter,
    TwoTierCacheStore,
)
from brix.process import ProcessResult

from test_incremental import make_graph, runs, write_workspace


class _Handler(BaseHTTPRequestHandler):
    """A minimal content-addressed blob server: GET/PUT files under a root."""

    def _target(self) -> Path:
        return self.server.root / self.path.lstrip("/")  # type: ignore[attr-defined]

    def do_GET(self):
        target = self._target()
        if target.is_file():
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        target = self._target()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        self.send_response(201)
        self.end_headers()

    def log_message(self, *args):  # keep the test output quiet
        pass


class _Server:
    """A backing HTTP server on an ephemeral port, as a context manager."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.root = root  # type: ignore[attr-defined]
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class Spawns(Reporter):
    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


# -- end-to-end across workspaces --------------------------------------------


def test_remote_store_caches_across_workspaces(tmp_path):
    """A build in one workspace populates the remote; a second workspace is
    served entirely from it — the shared-cache win, over real HTTP."""
    with _Server(tmp_path / "remote") as server:
        first, second = tmp_path / "one", tmp_path / "two"
        for root in (first, second):
            root.mkdir()
            write_workspace(root, {"a": "x", "b": "y"})

        store = HttpCacheStore(server.base_url)
        assert CacheBuilder(IncrementalBuilder(make_graph, root=first), store=store).build().success
        assert runs(first)  # the first build really ran the work

        spawns = Spawns()
        result = CacheBuilder(
            IncrementalBuilder(make_graph, root=second), store=HttpCacheStore(server.base_url)
        ).build(reporter=spawns)
        assert result.success
        assert spawns.labels == []  # nothing spawned; all served from the remote
        assert runs(second) == {}
        assert (second / "build/all.txt").read_text() == "XY"
        assert all(r.source == "cache" for r in result.process_results)


def test_unreachable_remote_never_fails_build(tmp_path):
    """Pointed at a dead endpoint, every store call degrades to a miss and the
    build still succeeds by running the work."""
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    dead = HttpCacheStore("http://127.0.0.1:1", retries=0)
    spawns = Spawns()
    result = CacheBuilder(
        IncrementalBuilder(make_graph, root=tmp_path), store=dead
    ).build(reporter=spawns)
    assert result.success
    assert spawns.labels  # a miss means the work really ran
    assert (tmp_path / "build/all.txt").read_text() == "XY"


# -- store-level round-trip: mode preservation -------------------------------


def test_executable_mode_survives_round_trip(tmp_path):
    """Bytes over HTTP carry no mode, so the store records it in the entry and
    re-applies it on restore — a restored binary stays executable."""
    with _Server(tmp_path / "remote") as server:
        store = HttpCacheStore(server.base_url)

        produced = tmp_path / "produced" / "bin" / "app"
        produced.parent.mkdir(parents=True)
        produced.write_text("#!/bin/sh\necho hi\n")
        os.chmod(produced, 0o755)

        fp = "deadbeef" + "0" * 56
        result = ProcessResult(
            action_name="build app", command="cc -o app app.c", exit_code=0, source="process"
        )
        store.store(fp, result, {"bin/app": produced})

        entry = store.load(fp)
        assert entry is not None
        out_root = tmp_path / "restored"
        out_root.mkdir()
        assert store.restore(fp, entry, NodeContext.for_root(out_root))

        restored = out_root / "bin/app"
        assert restored.read_text() == "#!/bin/sh\necho hi\n"
        assert stat.S_IMODE(restored.stat().st_mode) & 0o111  # executable bit kept


def test_missing_entry_is_a_miss(tmp_path):
    with _Server(tmp_path / "remote") as server:
        store = HttpCacheStore(server.base_url)
        assert store.load("f" * 64) is None


# -- two-tier: local front, remote back --------------------------------------


def test_remote_as_back_tier_promotes_to_local_front(tmp_path):
    """The intended deployment: a fast local front over a shared remote back. A
    back hit serves the build and is promoted into the local front."""
    with _Server(tmp_path / "remote") as server:
        # Seed the remote back by building once in a throwaway workspace.
        seed = tmp_path / "seed"
        seed.mkdir()
        write_workspace(seed, {"a": "x", "b": "y"})
        assert CacheBuilder(
            IncrementalBuilder(make_graph, root=seed), store=HttpCacheStore(server.base_url)
        ).build().success

        front_dir = tmp_path / "front"
        work = tmp_path / "work"
        work.mkdir()
        write_workspace(work, {"a": "x", "b": "y"})
        spawns = Spawns()
        result = CacheBuilder(
            IncrementalBuilder(make_graph, root=work),
            store=TwoTierCacheStore(CacheStore(front_dir), HttpCacheStore(server.base_url)),
        ).build(reporter=spawns)
        assert result.success
        assert spawns.labels == []  # served from the remote back
        assert (work / "build/all.txt").read_text() == "XY"
        assert any(front_dir.rglob("entry.json"))  # promoted into the local front
