#!/usr/bin/env python3
"""Starter template: a Java service built by brix.

Copy this directory, drop your sources under ``src/main/java``, set ENTRY_POINT
to your `main` class, and you have a working build:

    python brix_build.py build      # -> build/libs/app.jar
    python brix_build.py run app    # build the `app` jar, then `java -jar` it
    python brix_build.py status     # what would build, and why
    python brix_build.py help       # the live build configuration

Requires a JDK (``javac`` + ``jar``) on PATH. brix must be importable — in a
real project it is an installed package, so your ``brix_build.py`` starts straight at
``from brix import ...``. The one marked block below only lets this template also
run from inside the brix repo; **delete it in your copy.**
"""

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

from brix import Graph, build_stack, cli, java  # noqa: E402

ROOT = HERE

# Your service's fully-qualified `public static void main` class, and where the
# runnable jar lands. Change these two and the sources; the rest is generic.
ENTRY_POINT = "com.example.app.App"
APP_JAR = "build/libs/app.jar"


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        java.JarAction(
            # Every ``.java`` under this root is compiled. ``{name}`` is
            # recursive, so nested packages (``com/example/...``) are covered.
            # Add more globs to pull in extra source roots or generated sources.
            source_files=d.glob("src/main/java/{name}.java"),
            output_file=d.file(APP_JAR),
            entry_point=ENTRY_POINT,
            # To ship resources (``.properties``, ``META-INF/services/...``),
            # create ``src/main/resources`` and add:
            #     resource_dir="src/main/resources",
        )
    )
    return g


def run(paths):
    """``run`` builds the jar (alias ``app``) and then launches it."""
    return subprocess.call(["java", "-jar", str(paths[0])])


if __name__ == "__main__":

    def builder():
        # The recommended stack: atomic staging (all-or-nothing) over a shared
        # result cache over incremental rebuilds. Pass ``cache_dir=None`` to drop
        # the cache or ``staging_dir=None`` to drop atomic staging.
        return build_stack(create_graph, root=ROOT)

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            builder_factory=builder,
            aliases={"app": cli.Alias(APP_JAR, "the runnable application jar")},
            commands={"run": run},
        )
    )
