#!/usr/bin/env python3
"""Running C++ tests under brix, with per-binary impact selection.

Two independent units (adder, greeter) and a gtest binary for each. What makes
selection *free* in C++ is that a test binary is itself a build product:

  * each object's ``-MMD`` depfile makes it stale when a header it includes
    changes; the link graph makes a binary stale when an object it links changes;
  * so a `cpp.RunTestAction` — which replays on the binary's fingerprint — re-runs
    a test binary exactly when code that binary *statically links* changes, and
    never otherwise.

Running one `RunTestAction` per binary is therefore already impact selection: the
incremental engine does it, through tracking brix already has. Editing adder
re-runs only adder_test; greeter_test replays from cache. No selection index, no
eval-time analysis — the run unit (a binary) is the selection unit.

    python brix_build.py build      # first run: builds and runs both tests
    python brix_build.py build      # CACHED — nothing runs
    # edit src/adder.cpp
    python brix_build.py build      # re-runs adder_test only; greeter_test skipped

The one dependency this cannot see is code loaded at run time (a dlopen'd
plugin); declare those as `runtime_inputs=` on the RunTestAction. Needs g++ and
the gtest libraries (gtest_main via pkg-config).
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, cpp  # noqa: E402

ROOT = HERE
CXXFLAGS = ["-std=c++17", "-Iinclude"]
GTEST = cpp.pkg_config(["gtest_main"])  # -lgtest_main -lgtest, placed after objects

# Each test binary and the objects linked into it, as (source tree, unit).
# Independent on purpose: adder_test links nothing greeter_test does.
SUITES = {
    "adder_test": [("src", "adder"), ("test", "adder_test")],
    "greeter_test": [("src", "greeter"), ("test", "greeter_test")],
}


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data

    # Compile library and test sources to objects, each with a -MMD depfile.
    # Separate object dirs so the two compiles own disjoint outputs.
    for tree in ("src", "test"):
        graph.add(
            cpp.CompileSourceAction(
                source_files=d.glob(f"{tree}/{{name}}.cpp"),
                object_files=d.glob(f"build/obj/{tree}/{{name}}.o"),
                compile_options=CXXFLAGS,
            )
        )

    # Link each test binary, then run it as a tracked action. The RunTestAction
    # is what turns the engine's per-binary staleness into test selection.
    for name, objects in SUITES.items():
        (tree0, unit0), *rest = objects
        objs = d.file(f"build/obj/{tree0}/{unit0}.o")
        for tree, unit in rest:
            objs = objs + d.file(f"build/obj/{tree}/{unit}.o")
        graph.add(
            cpp.LinkApplicationAction(
                object_files=objs,
                output_file=d.file(f"build/test/{name}"),
                link_options=["-pthread"],
                deps=GTEST,
            )
        )
        graph.add(cpp.RunTestAction(test_binary=f"build/test/{name}"))
    return graph


def builder():
    return CacheBuilder(
        IncrementalBuilder(create_graph, root=ROOT, metadata_dir="build/.brix"),
        cache_dir="build/.brix-cache",
    )


if __name__ == "__main__":
    sys.exit(
        cli.main(
            create_graph,
            root=ROOT,
            metadata_dir="build/.brix",
            builder_factory=builder,
        )
    )
