"""Recording the build environment (diagnostic capture — phase 1).

brix fingerprints what a build *declares*. It is blind to what a tool reads
from the *inherited* environment without anyone declaring it — `JAVA_HOME`,
`CC`, a locale — which can change a build's output while brix, seeing no
declared change, serves the result as up to date: the classic "works on my
machine." This module records the environment each build ran under so that a
later difference is **visible**, not silent.

It is a *detective, not a guard*: it records and diffs; it does not rebuild on
an environment change (that is the deferred `env_used=` promotion). The captured
values are comparison *state* (see docs/content-key.md), so they live in
metadata, never the graph, and — being pure Python-owned diagnostic state — this
touches neither the fingerprint path nor the Rust engine. See
docs/environment-recording.md for the full contract.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Session noise that is not build-relevant and would bury a real signal. A
# conservative ignore-list (not an allow-list): completeness beats quiet for a
# diagnostic — showing a harmless extra is cheap, hiding the one variable that
# mattered defeats the purpose. Exact names plus fnmatch globs; everything not
# matched here is kept (PATH, HOME, JAVA_HOME, *FLAGS*, locale, project vars).
DEFAULT_IGNORE: tuple[str, ...] = (
    "_",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "TERM",
    "COLUMNS",
    "LINES",
    "LS_COLORS",
    "COLORTERM",
    "WINDOWID",
    "DISPLAY",
    "GPG_TTY",
    "VTE_VERSION",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_*",             # SSH_AUTH_SOCK, SSH_CONNECTION, SSH_CLIENT, SSH_TTY
    "XDG_SESSION*",      # per-session; XDG_DATA_DIRS/XDG_CONFIG_DIRS are kept
    "XDG_RUNTIME_DIR",
    "XDG_SEAT",
    "XDG_VTNR",
    "TERM_PROGRAM*",
    "TERM_SESSION_ID",
)

_RECORD_NAME = "environment.json"


def _is_ignored(name: str, patterns: tuple[str, ...]) -> bool:
    return any(name == p or fnmatch.fnmatchcase(name, p) for p in patterns)


def capture(
    environ: dict[str, str] | None = None,
    ignore: tuple[str, ...] | list[str] | None = None,
) -> dict[str, str]:
    """The build-relevant environment: `os.environ` minus the ignore-list.

    `ignore` *extends* `DEFAULT_IGNORE` (it does not replace it), so a project
    quiets its own known volatiles without losing the completeness default."""
    source = os.environ if environ is None else environ
    patterns = DEFAULT_IGNORE + tuple(ignore or ())
    return {
        name: value
        for name, value in source.items()
        if not _is_ignored(name, patterns)
    }


def write_record(
    directory: str | Path,
    *,
    command: str,
    variables: dict[str, str],
) -> Path:
    """Write the environment record beside the action metadata, atomically
    (tmp + replace). Returns the record path. Independent of metadata.json —
    pure diagnostic state, removed by `clean` with the rest of the directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _RECORD_NAME
    record = {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "brix_command": command,
        "variables": dict(sorted(variables.items())),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True))
    tmp.replace(path)
    return path


def read_record(directory: str | Path) -> dict | None:
    """The last recorded environment, or None if nothing was recorded (or the
    file is unreadable — a corrupt record is treated as absent, not an error)."""
    path = Path(directory) / _RECORD_NAME
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@dataclass
class EnvDiff:
    """What changed between a recorded environment and the current one."""

    added: dict[str, str] = field(default_factory=dict)      # name -> current
    removed: dict[str, str] = field(default_factory=dict)    # name -> recorded
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (recorded, current)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def diff(recorded: dict[str, str], current: dict[str, str]) -> EnvDiff:
    """Classify current-vs-recorded into added / removed / changed."""
    result = EnvDiff()
    for name, value in current.items():
        if name not in recorded:
            result.added[name] = value
        elif recorded[name] != value:
            result.changed[name] = (recorded[name], value)
    for name, value in recorded.items():
        if name not in current:
            result.removed[name] = value
    return result


def render(directory: str | Path, current: dict[str, str]) -> tuple[str, bool]:
    """The `brix.py env` printout and whether the environment differs.

    Returns (text, changed): `changed` is False when nothing is recorded yet or
    the environment is unchanged, True when it differs — the caller maps that to
    the exit code (0 clean / unrecorded, 1 changed)."""
    record = read_record(directory)
    if record is None:
        return ("No build environment recorded yet (run a build first).", False)

    recorded_vars = record.get("variables", {})
    lines = [
        f"Last build environment (captured {record.get('captured_at', '?')}, "
        f"command: {record.get('brix_command', '?')}):",
        f"  {len(recorded_vars)} variable(s) recorded.",
    ]
    delta = diff(recorded_vars, current)
    if delta.is_empty:
        lines.append("")
        lines.append("No changes since then.")
        return ("\n".join(lines), False)

    lines.append("")
    lines.append("Changes since then (current vs. recorded):")
    width = max(
        (len(n) for n in (*delta.changed, *delta.added, *delta.removed)),
        default=0,
    )
    for name in sorted(delta.changed):
        old, new = delta.changed[name]
        lines.append(f"  ~ {name.ljust(width)}  {old}  ->  {new}")
    for name in sorted(delta.added):
        lines.append(f"  + {name.ljust(width)}  {delta.added[name]}")
    for name in sorted(delta.removed):
        lines.append(f"  - {name.ljust(width)}  (was {delta.removed[name]})")
    return ("\n".join(lines), True)


def summary_line(directory: str | Path, current: dict[str, str]) -> str | None:
    """A one-line note for `status`, or None when there is nothing to say
    (no record, or unchanged). Does not change `status`'s exit semantics."""
    record = read_record(directory)
    if record is None:
        return None
    delta = diff(record.get("variables", {}), current)
    if delta.is_empty:
        return None
    return (
        f"environment: {delta.count} variable(s) changed since last build "
        "— run 'brix.py env'"
    )
