#!/usr/bin/env python3
"""Quantify the lazy-loading win: graph-construction time vs package count.

The monorepo-scaling thesis (docs/monorepo-scaling.md) is that the dominant cost
a *small* edit pays in a big repo is **constructing the whole build graph it then
mostly ignores** — running every service's ``create_graph()`` — and that lazy
loading is the tier that removes it. This measures exactly that: the wall time to
build the workspace graph, **full-load** (every package) vs **load-affected**
(only the packages a one-file edit touches), across a ladder of package counts.

Construction here is real work, not a stand-in: each package's ``create_graph()``
reads a manifest and builds ``MODULES`` actions from it — a service that reads a
config to decide its build. No build is executed and no sources are created; we
time construction alone, which is the whole point (incrementality already skips
execution; this is the cost *above* it).

    python examples/lazy_loading_benchmark.py
    PACKAGES=10,50,200,500 MODULES=20 REPS=5 python examples/lazy_loading_benchmark.py

The headline is the last column: full-load grows ~linearly with package count
(the O(whole-repo) construction the design note calls out), while load-affected
stays roughly flat — a small edit's cost stops tracking repo size. (The lazy side
still pays an O(#packages) *discovery + selection* walk, which is cheap per
package; the win is skipping the construction, not the walk.)
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

from brix.affected import select_affected  # noqa: E402
from brix.workspace import find_packages, load_workspace  # noqa: E402

# A package that reads its manifest and builds one action per listed module —
# real construction work (a file read + N action objects with data atoms).
PKG_TEMPLATE = '''\
from pathlib import Path
from brix import Graph, ExternalProcessAction

PKG = {pkg!r}

def create_graph():
    g = Graph(); d = g.data
    modules = Path(__file__).with_name("manifest.txt").read_text().split()
    for m in modules:
        g.add(ExternalProcessAction(
            name=f"compile {{m}}",
            command_line="{{cc}} {{src}} {{obj}}",
            inputs={{"cc": d.tool("cc"), "src": d.file(f"{{PKG}}/src/{{m}}.c")}},
            outputs={{"obj": d.file(f"build/{{PKG}}/{{m}}.o")}},
        ))
    return g
'''


def setup(root: Path, packages: int, modules: int) -> str:
    """Create `packages` independent package dirs, each with `modules` actions.
    Returns a repo-relative changed path inside the first package."""
    names = [f"m{j:03d}" for j in range(modules)]
    for i in range(packages):
        pkg = f"services/svc_{i:04d}"
        d = root / pkg
        d.mkdir(parents=True)
        (d / "manifest.txt").write_text(" ".join(names))
        (d / "brix.py").write_text(PKG_TEMPLATE.format(pkg=pkg))
    return f"services/svc_0000/src/{names[0]}.c"


def _median_ms(fn, reps: int) -> float:
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e3)
    return statistics.median(samples)


def bench(packages: int, modules: int, reps: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"brixlazy{packages}_"))
    try:
        changed = setup(root, packages, modules)
        discovered = find_packages(root)
        all_pkgs = {p.resolve() for p in discovered}
        # Bootstrap the persisted index (untimed): a real full build derives the
        # cross-package edges once and persists them, so later selection reads
        # them O(packages) instead of re-deriving.
        load_workspace(root).save_index()

        # Pure construction: full (every package) vs affected-only. This is the
        # thesis — the cost a small edit pays for the graph it then ignores.
        full = _median_ms(lambda: load_workspace(root, only=all_pkgs), reps)

        selected = select_affected(root, [changed], discovered)
        assert selected is not None and len(selected) == 1, selected  # one-file edit
        sel_set = {p.resolve() for p in selected}
        lazy = _median_ms(lambda: load_workspace(root, only=sel_set), reps)

        # Selection is a separate, graph-free cost (discovery walk + pattern
        # match + an O(packages) read of the persisted edges). Reported so its
        # scaling stays visible.
        select = _median_ms(lambda: select_affected(root, [changed], discovered), reps)
        return {"full": full, "lazy": lazy, "select": select}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    packages = [int(x) for x in os.environ.get("PACKAGES", "10,25,50,100").split(",")]
    modules = int(os.environ.get("MODULES", "20"))
    reps = int(os.environ.get("REPS", "3"))

    print(f"\nmodules/package={modules}  reps={reps}  (milliseconds; construction isolated)")
    print(f"{'packages':>9} {'full':>10} {'affected':>10} {'construct':>10} "
          f"{'select':>10}")
    print(f"{'':>9} {'construct':>10} {'construct':>10} {'speedup':>10} "
          f"{'(1 call)':>10}")
    for n in packages:
        r = bench(n, modules, reps)
        speedup = r["full"] / r["lazy"] if r["lazy"] else float("inf")
        print(f"{n:>9} {r['full']:>10.2f} {r['lazy']:>10.2f} {speedup:>9.1f}x "
              f"{r['select']:>10.1f}", flush=True)

    print(
        "\nReading: full-load construction grows ~linearly with package count;\n"
        "affected-only stays roughly flat, so the construction speedup rises with\n"
        "repo size — the design thesis holds. `select` reads the persisted\n"
        "cross-package edges (O(packages)) rather than re-deriving them, so it\n"
        "stays cheap and linear; a small edit's total cost (affected construct +\n"
        "select) is now well under a full load and the gap widens with repo size."
    )


if __name__ == "__main__":
    main()
