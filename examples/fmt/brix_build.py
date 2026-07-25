#!/usr/bin/env python3
"""Build a small app against the fmt library with brix.cpp.

A C++ counterpart to examples/junit5: validate `brix.cpp` — per-source
parallel compilation and, the real point, automatic `#include` header-dep
tracking (compiler depfiles) — on a famous real project's headers.

    examples/fmt/setup.sh                     # assemble build-src/ (once)
    python examples/fmt/brix_build.py build         # compile + link -> build/hello
    build/hello                               # run it
    python examples/fmt/brix_build.py status

Edit any header fmt's sources include (e.g. include/fmt/base.h) and rebuild:
only the translation units that include it recompile — brix learned the
dependency from the depfile the compiler wrote, with nothing declared.

Checkout defaults to ~/Projects/brix_test/fmt (override BRIX_FMT_ROOT); this
file uses the brix in this repo (../../src on sys.path).
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, cpp  # noqa: E402

ROOT = Path(os.environ.get("BRIX_FMT_ROOT", os.path.expanduser("~/Projects/brix_test/fmt"))).resolve()

# fmt is a C++ library; -I include resolves #include "fmt/...". C++17 is fmt's
# baseline for these sources.
CXX_OPTIONS = ["-std=c++17", "-I", "include", "-O2"]


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        cpp.CompileSourceAction(
            source_file=data.glob("build-src/{name}.cc"),
            object_file=data.glob("build/obj/{name}.o"),
            compile_options=CXX_OPTIONS,
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/hello"),
        ),
    )
    return graph


if __name__ == "__main__":
    from brix import AtomicBuilder, CacheBuilder, IncrementalBuilder

    def builder():
        return AtomicBuilder(
            CacheBuilder(
                IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
                cache_dir="build/.brix-cache",
            ),
            staging_dir="build/.brix-staging",
        )

    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            staging_dir="build/.brix-staging",
            builder_factory=builder,
            aliases={"hello": "build/hello"},
        )
    )
