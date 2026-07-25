#!/usr/bin/env python3
"""Running TypeScript tests under brix, with per-file impact selection.

Like the C++ example, selection here is *free*: a `ts.RunTestAction` replays on
its input fingerprint, and its byte-lens inputs are a test file plus its
transitive import closure. So editing a module re-runs only the test files that
import it — the incremental engine does the selecting, because the run unit (a
test file) is the selection unit. No selection index, no eval-time selector.

The one piece specific to TS: unlike C++, a test file is not a build product
whose staleness the engine already tracks, so we hand each RunTestAction its
import closure explicitly. `testimpact_ts.TsProvider.test_input_sets()` computes
it (and widens to every source for a test that reaches a computed
import()/require() or an unresolved import — the fail-open).

    python brix_build.py build      # first run: runs both test files
    python brix_build.py build      # CACHED — nothing runs
    # edit src/util.ts
    python brix_build.py build      # re-runs adder's test (it imports util); greeter's is skipped

Needs `npm install` here first (typescript + vitest).
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from brix import CacheBuilder, Graph, IncrementalBuilder, cli, ts  # noqa: E402
from brix import testimpact_ts as ti  # noqa: E402

ROOT = HERE
VITEST = ROOT / "node_modules/.bin/vitest"


def _source_files() -> list[str]:
    """Every workspace .ts file (POSIX, repo-relative), the graph the provider
    analyzes and the universe a fail-open test would depend on."""
    return sorted(
        p.relative_to(ROOT).as_posix()
        for d in ("src", "test")
        for p in (ROOT / d).rglob("*.ts")
    )


def create_graph() -> Graph:
    graph = Graph()
    d = graph.data
    if not VITEST.exists():
        sys.exit("vitest not installed — run `npm install` in this directory first")

    provider = ti.TsProvider(files=_source_files(), root=ROOT)
    vitest = d.tool(str(VITEST))

    # One RunTestAction per test file, over exactly its import closure. That
    # footprint is the whole of the selection: the engine re-runs a test only
    # when a file in its closure changes.
    for test_file, closure in provider.test_input_sets().items():
        graph.add(
            ts.RunTestAction(
                test_file=test_file,
                sources=[s for s in sorted(closure) if s != test_file],
                runner=vitest,
            )
        )
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
