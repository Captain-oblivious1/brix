#!/usr/bin/env python3
"""Workspace entry point for a **polyglot** (Java + TypeScript) monorepo.

    python3 examples/polyglot_monorepo/brix.py build
    python3 examples/polyglot_monorepo/brix.py status

This is the closest available rehearsal of the air-gapped target: a ~50-service
Java+TS monorepo. Two independent stacks live in one workspace and one git repo:

    backend/   Java   — libs/model, services/orders           (java.JarAction)
    frontend/  TS     — shared, app  (app depends on shared)   (ts.CompileAction)

The loader finds each package `brix.py`, merges their graphs, and builds
identically to a monolith. The point this example makes that the single-language
dogfoods do not: **lazy loading partitions a mixed-language workspace** — a
backend edit never loads a frontend package, and vice versa — so a 50-service
repo touches only the slice a change affects, across languages. `verify_polyglot.py`
proves it with explicit change sets (and builds the Java slice for real; the TS
slice builds where `tsc` is available).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import cli, load_workspace  # noqa: E402  (also: load_affected)

ROOT = Path(__file__).resolve().parent


def workspace_graph():
    # Safe default: load every package and merge — identical to the monolith.
    # (Swap for `load_affected(ROOT).graph` to load only what changed; relying
    # on lazy loading in anger is post-trial. See verify_polyglot.py.)
    return load_workspace(ROOT).graph


if __name__ == "__main__":
    cli.main(workspace_graph, root=ROOT)
