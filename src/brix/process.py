"""Process execution and structured results."""

from __future__ import annotations

import os
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


def scrub_env(
    literal_env: dict[str, str],
    allow_names: set[str],
    env_unset: tuple[str, ...] | list[str],
) -> dict[str, str]:
    """The **authoritative** environment for scrub mode: only the plumbing base
    (`brix.env.BASE_ENV`) and the action's declared allowlist (`allow_names` =
    its SYSTEM + PASS names), each resolved from the ambient environment, with
    literal ``env=`` values layered on and ``UNSET`` names removed. Everything
    undeclared is simply absent, so it cannot silently affect the build."""
    from .env import BASE_ENV

    allow = set(BASE_ENV) | allow_names
    final = {name: os.environ[name] for name in allow if name in os.environ}
    final.update(literal_env or {})
    for name in env_unset or ():
        final.pop(name, None)
    return final


def run_process(
    action_name: str,
    command: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    env_unset: tuple[str, ...] | list[str] | None = None,
    authoritative: bool = False,
) -> ProcessResult:
    """Run a rendered command line, capturing stdout/stderr/exit status.

    ``env`` entries extend (and shadow) the inherited environment; ``env_unset``
    names are removed from it (force-absent), which also forces a full inherited
    environment to be built so the removal takes effect. With ``authoritative``
    (scrub mode) ``env`` is the *complete* environment — nothing is inherited.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return ProcessResult(action_name, command, stderr=f"brix: bad command line: {exc}")
    if not argv:
        return ProcessResult(action_name, command, stderr="brix: empty command line")
    if authoritative:
        full_env = dict(env or {})  # complete environment; nothing inherited
    elif env or env_unset:
        full_env = {**os.environ, **(env or {})}
        for name in env_unset or ():
            full_env.pop(name, None)
    else:
        full_env = None
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=full_env)
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
