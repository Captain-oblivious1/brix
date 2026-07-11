#!/usr/bin/env python3
"""Characterize brix's fixed per-build overhead as it scales with graph size.

A no-op build does zero process work — every node replays — so its wall time IS
brix's overhead: realize the globs, expand, and stat/hash every input to decide
nothing is stale. That floor is paid on *every* build in a big monorepo, so the
question that governs scaling is how it grows with node/file count. This walks a
size ladder and reports µs/node so a constant column reveals linear vs superlinear
behavior directly.

Measures the in-process build() call, so it excludes the fixed cold-CLI tax
(~0.25s: python start + import brix + load the Rust engine) and isolates the
engine's scaling. Run under both engines:

    python examples/scaling_benchmark.py                 # default (Rust) engine
    BRIX_ENGINE=py python examples/scaling_benchmark.py  # Python reference engine
    SIZES=100,1000,10000 NO_FANIN=1 python examples/scaling_benchmark.py

CRITICAL: build the Rust engine in RELEASE first —
    (cd rust && cargo build -p brix-py --release)
A debug .so (plain `cargo build`) is unoptimized and runs this hashing/JSON-heavy
per-node work ~4-6x slower, which silently skews every number here (and can make
the Rust engine look slower than the Python one — a pure artifact). The engine
loader prefers target/release over target/debug, so a stale debug-only build is
easy to measure by accident.
"""

import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(os.environ.get("BRIX_REPO", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "src"))

from brix import ExpandAll, ExternalProcessAction, Graph, IncrementalBuilder  # noqa: E402

# A tiny concat tool for the optional fan-in node (writes a real output file).
CONCAT = (
    "import sys\n"
    "from pathlib import Path\n"
    "Path(sys.argv[-1]).write_text("
    "''.join(Path(p).read_text() for p in sys.argv[1:-1]))\n"
)

FANIN = os.environ.get("NO_FANIN") != "1"


def graph_factory(root: Path):
    def make():
        g = Graph()
        d = g.data
        # N independent "compiles" (a cp per source), fanned by one ExpandAll
        # over a glob — the realistic shape, so glob realization is part of the
        # cost. Note the graph object model is O(1) (one ExpandAll); the N-ness
        # appears only when the engine realizes the glob and expands, which is
        # exactly the per-build cost we are measuring.
        g.add(
            ExpandAll(
                element_name="name",
                collection=d.glob("src/{name}.txt"),
                child=ExternalProcessAction(
                    name="cp {name}",
                    command_line="{cp} {source} {dest}",
                    inputs={"cp": d.tool("cp"), "source": d.file("src/{name}.txt")},
                    outputs={"dest": d.file("build/out/{name}.txt")},
                ),
            ),
        )
        if FANIN:
            # A fan-in whose glob-realize across all N outputs is exactly the
            # kind of overhead that scales.
            g.add(
                ExternalProcessAction(
                    name="combine",
                    command_line="{py} {tool} {parts} {all}",
                    inputs={
                        "py": d.tool(sys.executable),
                        "tool": d.file("tools/concat.py"),
                        "parts": d.glob("build/out/{name}.txt"),
                    },
                    outputs={"all": d.file("build/all.txt")},
                ),
            )
        return g

    return make


def setup(root: Path, n: int) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    (root / "tools/concat.py").write_text(CONCAT)
    for i in range(n):
        (root / "src" / f"f{i:05d}.txt").write_text(f"content {i}\n")


JOBS = int(os.environ["JOBS"]) if os.environ.get("JOBS") else None


def bench(n: int, reps: int = 4):
    root = Path(tempfile.mkdtemp(prefix=f"brixbench{n}_"))
    try:
        setup(root, n)
        factory = graph_factory(root)

        b = IncrementalBuilder(factory, root=root, metadata_dir=".brix-meta")
        t0 = time.perf_counter()
        assert b.build(jobs=JOBS).success, "clean build failed"
        clean = time.perf_counter() - t0

        noop = []
        for _ in range(reps):
            # A fresh builder per rep, sharing on-disk metadata — like re-running
            # the CLI, so the whole freshness pass happens cold each time.
            fresh = IncrementalBuilder(factory, root=root, metadata_dir=".brix-meta")
            t = time.perf_counter()
            assert fresh.build(jobs=JOBS).success, "no-op build failed"
            noop.append(time.perf_counter() - t)

        (root / "src" / "f00000.txt").write_text("changed\n")
        fresh = IncrementalBuilder(factory, root=root, metadata_dir=".brix-meta")
        t = time.perf_counter()
        assert fresh.build(jobs=JOBS).success, "edit build failed"
        edit = time.perf_counter() - t

        return clean, min(noop), statistics.median(noop), edit
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    engine = os.environ.get("BRIX_ENGINE", "rust")
    sizes = [int(x) for x in os.environ.get("SIZES", "100,300,1000,3000").split(",")]
    fanin = "with fan-in" if FANIN else "no fan-in"
    print(f"\nengine={engine}  jobs={JOBS}  ({fanin}; in-process build(); seconds)")
    print(f"{'nodes':>7} {'clean':>8} {'noop-min':>9} {'noop-med':>9} {'1-edit':>8} "
          f"{'noop µs/node':>13}")
    for n in sizes:
        clean, noop_min, noop_med, edit = bench(n)
        print(f"{n:>7} {clean:>8.3f} {noop_min:>9.4f} {noop_med:>9.4f} {edit:>8.4f} "
              f"{noop_min / n * 1e6:>13.1f}")


if __name__ == "__main__":
    main()
