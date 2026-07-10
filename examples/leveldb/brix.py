#!/usr/bin/env python3
"""Build leveldb as a static library with brix.cpp, then link a smoke test.

The C++ dogfood that exercises the whole cpp toolchain on a large real
project (~38 translation units): per-source parallel compilation with
automatic `#include` header-dep tracking, `cpp.ArchiveAction` bundling the
objects into `libleveldb.a`, and `cpp.LinkApplicationAction` linking a small
program against that archive.

    examples/leveldb/setup.sh                 # stage build-src/ (once)
    python examples/leveldb/brix.py build     # compile -> ar -> libleveldb.a
    python examples/leveldb/brix.py run smoke  # link main.o + .a, then run it
    python examples/leveldb/brix.py status

Edit a header the sources include (e.g. db/dbformat.h) and rebuild: only the
translation units that include it recompile — brix learned the dependency
from the depfile the compiler wrote, nothing declared. Edit only .cc
implementation and `run smoke` re-links and re-runs without recompiling the
untouched units.

Checkout defaults to ~/Projects/brix_test/leveldb (override BRIX_LEVELDB_ROOT);
this file uses the brix in this repo (../../src on sys.path).
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import Graph, cli, cpp  # noqa: E402

ROOT = Path(
    os.environ.get("BRIX_LEVELDB_ROOT", os.path.expanduser("~/Projects/brix_test/leveldb"))
).resolve()

# leveldb is C++17, built without exceptions/RTTI (its CMake default).
# -I. resolves "db/...", "table/...", "port/..."; -Iinclude resolves the
# public "leveldb/..." headers; -Ibuild-src/config finds the generated
# port_config.h that port_stdcxx.h pulls in via __has_include.
LIB_OPTIONS = [
    "-std=c++17", "-fno-exceptions", "-fno-rtti",
    "-DLEVELDB_PLATFORM_POSIX=1", "-DLEVELDB_COMPILE_LIBRARY",
    "-I.", "-Iinclude", "-Ibuild-src/config",
]
# The smoke program only needs the public headers.
SMOKE_OPTIONS = ["-std=c++17", "-Iinclude"]


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        # Every staged library source -> its object, in parallel.
        cpp.CompileSourceAction(
            source_files=data.glob("build-src/lib/{name}.cc"),
            object_files=data.glob("build/obj/{name}.o"),
            compile_options=LIB_OPTIONS,
        ),
        # Bundle the objects into the static library.
        cpp.ArchiveAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/lib/libleveldb.a"),
        ),
        # Compile the smoke program.
        cpp.CompileSourceAction(
            source_files=data.glob("smoke/{name}.cc"),
            object_files=data.glob("build/smoke/{name}.o"),
            compile_options=SMOKE_OPTIONS,
        ),
        # Link it against the archive (-lpthread: leveldb's POSIX env uses it).
        cpp.LinkApplicationAction(
            object_files=data.file("build/smoke/main.o") + data.file("build/lib/libleveldb.a"),
            output_file=data.file("build/smoke_test"),
            link_options=["-lpthread"],
        ),
    )
    return graph


def run_targets(paths: list[Path]) -> int:
    """Build-then-act: run each freshly built target, forwarding its status."""
    for path in paths:
        result = subprocess.run([str(path)])
        if result.returncode != 0:
            return result.returncode
    return 0


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
            aliases={"lib": "build/lib/libleveldb.a", "smoke": "build/smoke_test"},
            commands={"run": run_targets},
        )
    )
