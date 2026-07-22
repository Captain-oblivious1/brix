#!/usr/bin/env python3
"""Workspace entry point for a hierarchical (multi-package) brix build.

Run from anywhere:

    python3 examples/hierarchical/brix_build.py build
    python3 examples/hierarchical/brix_build.py status

This file is the workspace *entry point*, not a package: it exposes no
``create_graph()``, so the loader discovers it but contributes no actions from
it. The actual build lives in the package ``brix_build.py`` files under ``libs/``,
``services/``, and ``docs/`` — placed sparsely, at boundaries. The loader finds
them, resolves which one owns each path (nearest ancestor), and merges their
graphs. Splitting a monolith this way builds *identically*; the split is what
later lets a build load only the packages an edit touches.

See verify_lazy.py for a runnable proof that a scoped edit loads only the
affected packages.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import cli, load_workspace  # noqa: E402  (also: load_affected)

ROOT = Path(__file__).resolve().parent


def workspace_graph():
    # The safe default: load every package and merge — identical to the
    # equivalent monolith. This is what the air-gapped trial runs first.
    return load_workspace(ROOT).graph

    # Opt into lazy loading — load only the packages affected by what changed
    # since the last build (git-detected), plus their dependents, falling back
    # to a full load whenever completeness can't be proven — by swapping the
    # line above for:
    #
    #     from brix import load_affected
    #     return load_affected(ROOT).graph
    #
    # Relying on lazy loading in anger is post-trial; the full load above is the
    # floor. verify_lazy.py exercises the lazy path directly, with explicit
    # change sets, so it needs no git state.


if __name__ == "__main__":
    cli.main(workspace_graph, root=ROOT)
