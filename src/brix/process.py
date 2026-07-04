"""Process execution and structured results."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProcessResult:
    """The captured result of one process invocation (or a failure to invoke)."""

    action_name: str
    command: str
    argv: list[str] = field(default_factory=list)
    exit_code: int | None = None
    signal: int | None = None
    stdout: str = ""
    stderr: str = ""
    # Provenance: how this result was obtained ("process", "replayed",
    # later "cache"). Stamped by whatever produced the result. Scheduling
    # never reads it; reporting may.
    source: str = "process"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> str:
        if self.ok:
            return f"ok: {self.action_name}"
        if self.signal is not None:
            return f"FAILED (signal {self.signal}): {self.action_name}"
        if self.exit_code is not None:
            return f"FAILED (exit {self.exit_code}): {self.action_name}"
        return f"FAILED (did not run): {self.action_name}"


def run_process(action_name: str, command: str, cwd: Path) -> ProcessResult:
    """Run a rendered command line, capturing stdout/stderr/exit status."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return ProcessResult(action_name, command, stderr=f"brix: bad command line: {exc}")
    if not argv:
        return ProcessResult(action_name, command, stderr="brix: empty command line")
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        return ProcessResult(action_name, command, argv=argv, stderr=f"brix: cannot execute: {exc}")
    rc = proc.returncode
    return ProcessResult(
        action_name=action_name,
        command=command,
        argv=argv,
        exit_code=rc if rc >= 0 else None,
        signal=-rc if rc < 0 else None,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
