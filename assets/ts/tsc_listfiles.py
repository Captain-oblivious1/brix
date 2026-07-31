#!/usr/bin/env python3
"""Run ``tsc`` and record which files it actually read, as a Makefile depfile.

This is brix's TypeScript counterpart of ``cc -MMD -MF``: TypeScript's real
dependency set is the transitive ``import`` graph, which the declared source
glob doesn't capture (a module can import a shared ``.ts`` from outside its own
``src/``, or an ambient ``.d.ts``). ``tsc --listFiles`` prints every file in the
compilation to stdout; this wrapper runs the compile with that flag, keeps the
workspace-relative source/declaration files, and writes them as a Makefile-format
depfile that the incremental layer already knows how to scan — so editing a
deeply-imported source re-stales the compile, on either engine, with no
engine change.

Usage::

    tsc_listfiles.py <depfile> [--exclude <dir>]... -- <tsc> <tsc-arg>...

* ``<depfile>`` — where to write the Makefile depfile (a declared output, so
  under atomic staging brix passes the staged path here).
* ``--exclude <dir>`` — a workspace-relative directory whose files are NOT
  recorded as discovered deps: this module's own ``out_dir`` (its emit is not an
  input) and each *declared* dependency's directory (those ``.d.ts`` are tracked
  through the ts-dts lens; byte-tracking them here would defeat it).
* everything after ``--`` is the real ``tsc`` invocation, run verbatim with
  ``--listFiles`` appended.

Only files under the workspace root (the process cwd, where brix runs actions)
are kept; absolute lib/``node_modules`` paths and anything outside the tree are
dropped, exactly as the C/C++ scanner drops absolute system prerequisites.
"""

import subprocess
import sys
from pathlib import Path

# TypeScript inputs whose edits can change a compile's output. `.json` is
# included because `resolveJsonModule` imports make a JSON file a real input.
_TRACKED_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".d.ts", ".json")


def _is_tracked(rel: str) -> bool:
    return rel.endswith(_TRACKED_SUFFIXES) and not rel.endswith(".js")


def main(argv: list[str]) -> int:
    depfile = argv[0]
    rest = argv[1:]
    excludes: list[str] = []
    while rest and rest[0] == "--exclude":
        excludes.append(rest[1].rstrip("/") + "/")
        rest = rest[2:]
    if not rest or rest[0] != "--":
        sys.stderr.write("tsc_listfiles: expected `-- <tsc> <args>...`\n")
        return 2
    tsc_cmd = rest[1:]

    proc = subprocess.run(
        [*tsc_cmd, "--listFiles"], capture_output=True, text=True
    )
    sys.stderr.write(proc.stderr)

    root = Path.cwd().resolve()
    deps: list[str] = []
    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        path = Path(candidate)
        # A --listFiles entry is an existing file; anything else (an error
        # summary, a diagnostic) is a real message and is forwarded to stdout.
        if not path.is_file():
            sys.stdout.write(line + "\n")
            continue
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue  # outside the workspace (lib.d.ts, a global node_modules)
        if "node_modules/" in f"/{rel}":
            continue
        if not _is_tracked(rel):
            continue
        if any(rel.startswith(prefix) for prefix in excludes):
            continue
        deps.append(rel)

    # Makefile format: `target: prereq...`, spaces backslash-escaped. The scanner
    # discards the target, so its exact text is irrelevant.
    body = " ".join(sorted(set(dep.replace(" ", "\\ ") for dep in deps)))
    out = Path(depfile)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"tsdeps: {body}\n")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
