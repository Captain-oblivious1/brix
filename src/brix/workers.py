"""Persistent workers: a long-lived process that executes many actions.

A worker amortizes startup/warmup cost (a cold JVM is the motivating case) by
staying alive across requests. It is purely an **execution transport**: an
action run by a worker must be indistinguishable — in results, fingerprints,
caching, staging, and reporting — from the same action spawned as a process.
The only observable difference is speed.

An action opts in with ``worker=``:

    javac_worker = WorkerSpec(
        name="javac",
        command_line="{java} -jar {worker_jar}",
        inputs={"java": data.tool("java"), "worker_jar": data.file("build/javac-worker.jar")},
    )
    ExternalProcessAction(
        name="compile {module}",
        worker=javac_worker,
        command_line="--sources {sources} -d {classes_dir}",
        inputs={...}, outputs={...},
    )

With ``worker=`` set, ``command_line`` renders exactly as today (overlay
output-redirection included, which is what keeps atomic staging working), but
the rendered argv is sent to the worker as a request instead of spawned.

Protocol — JSON, one object per line, over the worker's stdin/stdout. The
worker's stdout is reserved for the protocol; tool output travels in the
response. Engine → worker:

    {"id": 7, "args": ["--sources", "src/A.java", ...], "cwd": "/abs/workspace"}

Worker → engine:

    {"id": 7, "exitCode": 0, "stdout": "...", "stderr": "..."}

v1 rules: one request in flight per worker process (parallelism is the
scheduler starting up to ``jobs`` workers of a spec); the worker is stateless
between requests (``cwd`` travels per-request, so a future cross-build daemon
can serve many workspaces); the scheduler owns lifecycles — workers start
lazily on first request and are all terminated at build end.

Failure semantics (do not soften): a worker crash or protocol garbage fails
the in-flight request with ``exit_code=None`` and a ``brix:`` stderr — like a
signal kill, it is not the action's deterministic answer, so it is never
cached; the worker is discarded, not reused. A worker that fails to start
fails every request the same way. There is **no** silent fallback to
spawning — a misconfigured worker must be loud, not quietly slow.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .data import DataExpr
from .process import ProcessResult


@dataclass
class WorkerSpec:
    """How to launch a worker. ``command_line`` renders (like an action's) from
    ``inputs`` into the worker's launch argv; ``name`` is diagnostic identity."""

    name: str
    command_line: str
    inputs: dict[str, DataExpr] = field(default_factory=dict)


class WorkerCrashed(Exception):
    """The worker died or spoke garbage mid-request."""


class WorkerStartupError(Exception):
    """The worker process could not be launched."""


class Worker:
    """One live worker process. Not thread-safe: a pool lends it to one thread
    at a time (one request in flight)."""

    def __init__(self, argv: list[str], cwd: Path) -> None:
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
            )
        except OSError as exc:
            raise WorkerStartupError(str(exc)) from exc
        self._next_id = 0

    def request(self, args: list[str], cwd: str) -> dict:
        self._next_id += 1
        rid = self._next_id
        line = json.dumps({"id": rid, "args": args, "cwd": cwd})
        try:
            assert self.proc.stdin is not None and self.proc.stdout is not None
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            response = self.proc.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerCrashed(f"write/read failed: {exc}") from exc
        if not response:  # EOF — the worker exited without answering
            raise WorkerCrashed("worker exited without responding")
        try:
            message = json.loads(response)
        except ValueError as exc:
            raise WorkerCrashed(f"non-JSON protocol line {response!r}: {exc}") from exc
        if message.get("id") != rid:
            raise WorkerCrashed(f"response id {message.get('id')!r} != request {rid}")
        return message

    def crash_detail(self) -> str:
        """Best-effort stderr of a crashed worker; the process is killed first
        so reading its stderr pipe cannot block."""
        self.terminate()
        try:
            assert self.proc.stderr is not None
            return self.proc.stderr.read() or ""
        except OSError:
            return ""

    def terminate(self) -> None:
        if self.proc.poll() is not None:
            return
        for closer in (self.proc.stdin,):
            try:
                if closer is not None:
                    closer.close()
            except OSError:
                pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class WorkerPool:
    """Live workers of one spec. Lends an idle worker or starts a new one; a
    crashed worker is discarded, never returned to the idle set."""

    def __init__(self, spec: WorkerSpec) -> None:
        self.spec = spec
        self._idle: list[Worker] = []
        self._all: list[Worker] = []
        self._lock = threading.Lock()

    def _launch_argv(self, ctx) -> list[str]:
        # Reuse the action renderer so tool resolution and overlay-resolved
        # input paths (the worker jar may be a staged build product) work
        # exactly as they do for a spawned command.
        from .actions import ExternalProcessAction
        from .render import render_command

        launcher = ExternalProcessAction(
            name=self.spec.name,
            command_line=self.spec.command_line,
            inputs=self.spec.inputs,
        )
        return shlex.split(render_command(launcher, ctx))

    def check_out(self, ctx) -> Worker:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        worker = Worker(self._launch_argv(ctx), ctx.workspace_root)  # may raise
        with self._lock:
            self._all.append(worker)
        return worker

    def check_in(self, worker: Worker) -> None:
        with self._lock:
            self._idle.append(worker)

    def discard(self, worker: Worker) -> None:
        with self._lock:
            if worker in self._idle:
                self._idle.remove(worker)
        worker.terminate()

    def terminate_all(self) -> None:
        with self._lock:
            workers = list(self._all)
            self._all.clear()
            self._idle.clear()
        for worker in workers:
            worker.terminate()


class WorkerRegistry:
    """Build-scoped: one pool per WorkerSpec, all torn down at build end. Owned
    by the scheduler; layers never see it (workers are pure transport)."""

    def __init__(self) -> None:
        self._pools: dict[int, WorkerPool] = {}
        self._lock = threading.Lock()

    def _pool(self, spec: WorkerSpec) -> WorkerPool:
        with self._lock:
            pool = self._pools.get(id(spec))
            if pool is None:
                pool = WorkerPool(spec)
                self._pools[id(spec)] = pool
            return pool

    def run(self, spec: WorkerSpec, action_label: str, args: list[str], ctx) -> ProcessResult:
        pool = self._pool(spec)
        command = " ".join(args)
        try:
            worker = pool.check_out(ctx)
        except WorkerStartupError as exc:
            # No fallback to spawning: a misconfigured worker is loud. exit_code
            # None => not the action's deterministic answer => never cached.
            return ProcessResult(
                action_name=action_label, command=command, argv=list(args),
                stderr=f"brix: worker {spec.name!r} failed to start: {exc}",
            )
        try:
            message = worker.request(args, str(ctx.workspace_root))
        except WorkerCrashed as exc:
            detail = worker.crash_detail()
            pool.discard(worker)
            stderr = f"brix: worker {spec.name!r} crashed: {exc}"
            if detail:
                stderr += "\n" + detail
            return ProcessResult(
                action_name=action_label, command=command, argv=list(args), stderr=stderr,
            )
        pool.check_in(worker)
        return ProcessResult(
            action_name=action_label,
            command=command,
            argv=list(args),
            exit_code=message.get("exitCode"),
            stdout=message.get("stdout", "") or "",
            stderr=message.get("stderr", "") or "",
            source="process",
        )

    def terminate_all(self) -> None:
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            pool.terminate_all()
