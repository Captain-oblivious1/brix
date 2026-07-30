"""Emit the Python lowering of a DepSet-consuming graph as JSON.

Exercises the shared `DepSet`: composition (`+` ordered merge with first-wins
dedup, `-` fnmatch exclude), the compile/link flag split, and both consumers —
cpp `deps=` (compile_flags on every TU, link_flags + byte-lens artifact files on
the link) and kotlin `classpath=` (a path-sep-joined value + a kotlin-abi track).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from brix import Graph, cpp, kotlin  # noqa: E402
from brix.deps import DepSet  # noqa: E402
from brix.lowering import graph_to_spec  # noqa: E402


def reference_graph() -> Graph:
    g = Graph()
    d = g.data
    # Compose: a second DepSet re-lists libfoo.a (deduped, first wins) and adds
    # -lbar; then drop libbar.a by fnmatch — but its -lbar flag survives (exclude
    # filters artifacts only), proving the flag/artifact split.
    cdeps = (
        DepSet(
            artifacts=("build/lib/libfoo.a",),
            compile_flags=("-I/opt/foo/include", "-DFOO=1"),
            link_flags=("-L/opt/foo/lib", "-lfoo"),
        )
        + DepSet(artifacts=("build/lib/libbar.a", "build/lib/libfoo.a"), link_flags=("-lbar",))
        - "*/libbar.a"
    )
    cp = DepSet(artifacts=("libs/guava.jar", "libs/annotations.jar"))
    g.add(
        cpp.CompileSourceAction(
            source_file=d.glob("src/{name}.cpp"),
            object_file=d.glob("build/obj/{name}.o"),
            compiler=d.tool("clang++"),
            compile_options=["-O2"],
            deps=cdeps,
        ),
        cpp.LinkApplicationAction(
            object_files=d.glob("build/obj/{name}.o"),
            output_file=d.file("build/bin/app"),
            linker=d.tool("clang++"),
            deps=cdeps,
        ),
        kotlin.JarAction(
            source_files=d.glob("src/main/kotlin/{name}.kt"),
            output_file=d.file("build/libs/app.jar"),
            classpath=cp,
        ),
    )
    return g


if __name__ == "__main__":
    print(json.dumps(graph_to_spec(reference_graph())))
