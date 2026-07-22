#!/usr/bin/env python3
"""Build the_silver_searcher (ag) with brix — a real C tool with third-party
system-library dependencies.

The dogfood for `cpp.pkg_config`: ag's dependencies (PCRE, zlib, lzma) are
resolved from the system via pkg-config into a composable `DepSet`, which feeds
the compile and link. brix has no dependency resolver by design — it drives
pkg-config and consumes the flags (docs/dependencies.md). ag's 12 C sources are
compiled to object files (the Windows-only one dropped with the glob `-`
operator) and linked into the `ag` binary; `-MMD` depfiles keep header edits
selective.

Setup (once):
    git clone --depth 1 https://github.com/ggreer/the_silver_searcher.git \
        ~/Projects/brix_test/the_silver_searcher
    cd ~/Projects/brix_test/the_silver_searcher && ./autogen.sh && ./configure
    # autotools generates src/config.h (brix owns the compile/link from there)

Run (from anywhere):
    python examples/the_silver_searcher/brix_build.py build      # -> build/ag
    python examples/the_silver_searcher/brix_build.py status
    ~/Projects/brix_test/the_silver_searcher/build/ag --version

The checkout defaults to ~/Projects/brix_test/the_silver_searcher (override with
BRIX_AG_ROOT). This file uses the brix in this repo — it puts ../../src on
sys.path.
"""

import os
import sys
from pathlib import Path

# Point at this repository's source tree for the in-tree example.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, cpp  # noqa: E402

_DEFAULT_ROOT = os.path.expanduser("~/Projects/brix_test/the_silver_searcher")
ROOT = Path(os.environ.get("BRIX_AG_ROOT", _DEFAULT_ROOT)).resolve()


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    # Third-party deps resolved from the system via pkg-config -> a DepSet whose
    # link flags (-lpcre -lz -llzma) feed the link; compile flags (if any) the
    # compile. A missing lib fails loudly here, before the build starts.
    deps = cpp.pkg_config(["libpcre", "zlib", "liblzma"])
    graph.add(
        cpp.CompileSourceAction(
            # print_w32.c is Windows-only — drop it with the glob `-` operator.
            source_files=data.glob("src/{name}.c") - "*print_w32.c",
            object_files=data.glob("build/obj/{name}.o"),
            compiler=data.tool("cc"),
            compile_options=[
                "-std=gnu89", "-D_GNU_SOURCE", "-DHAVE_CONFIG_H",
                "-Isrc", "-O2", "-pthread",
            ],
            deps=deps,
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/ag"),
            linker=data.tool("cc"),
            link_options=["-pthread"],
            deps=deps,
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
            aliases={"ag": "build/ag"},
        )
    )
