#!/usr/bin/env python3
"""Starter template: a TypeScript service built by brix.

Copy this directory, drop your sources under ``src``, and you have a working
build:

    python brix_build.py build      # -> dist/*.js
    python brix_build.py run app    # build the entry point, then `node dist/main.js`
    python brix_build.py status     # what would build, and why
    python brix_build.py help       # the live build configuration

Requires ``tsc`` (the TypeScript compiler). It is rarely global, so install one
locally and this template will find it::

    npm install typescript@5

brix must be importable — in a real project it is an installed package, so your
``brix_build.py`` starts straight at ``from brix import ...``. The one marked block
below only lets this template also run from inside the brix repo; **delete it in
your copy.**
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---- DELETE THIS BLOCK IN YOUR COPY ---------------------------------------
# It lets the template import brix from this repo's ``src/`` when run in-tree.
# Your own project installs brix as a package, so none of this is needed.
_repo_src = HERE.parents[2] / "src"
if (_repo_src / "brix").is_dir():
    sys.path.insert(0, str(_repo_src))
# ---------------------------------------------------------------------------

from brix import Graph, build_stack, cli, ts  # noqa: E402

ROOT = HERE
OUT_DIR = "dist"

# tsc is usually a local devDependency: prefer $BRIX_TSC, then a local install,
# then a global ``tsc``.
_local_tsc = HERE / "node_modules" / ".bin" / "tsc"
TSC = (
    os.environ.get("BRIX_TSC")
    or (str(_local_tsc) if _local_tsc.exists() else None)
    or shutil.which("tsc")
    or "tsc"
)


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        ts.CompileAction(
            # Every ``.ts`` under ``src`` is compiled; ``{name}`` is recursive,
            # so nested folders are covered. brix also discovers the imports each
            # file actually reads, so an edit to a deeply-imported source
            # re-stales this build even if this glob doesn't name it.
            sources=d.glob("src/{name}.ts"),
            out_dir=OUT_DIR,
            tsc=d.tool(TSC),
            options=["--target", "es2020", "--module", "commonjs",
                     "--rootDir", "src", "--strict"],
        )
    )
    return g


def run(paths):
    """``run`` builds the bundle (alias ``app``) and then runs the entry point."""
    return subprocess.call(["node", f"{OUT_DIR}/main.js"])


if __name__ == "__main__":

    def builder():
        # Cache + incremental, but NO atomic staging: ``tsc`` resolves
        # cross-module ``import``s off the real filesystem, which an atomic
        # overlay can't redirect — so built outputs must sit at their real paths.
        # A single-module service would be fine with atomic staging; dropping it
        # keeps the template correct as soon as you add a second module.
        return build_stack(create_graph, root=ROOT, staging_dir=None)

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            builder_factory=builder,
            aliases={"app": cli.Alias(f"{OUT_DIR}/main.js", "the built entry point")},
            commands={"run": run},
        )
    )
