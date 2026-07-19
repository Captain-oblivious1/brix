#!/usr/bin/env python3
"""Prove that lazy loading partitions a **polyglot** (Java + TypeScript) monorepo.

Run:  python3 examples/polyglot_monorepo/verify_polyglot.py

The workspace has two independent stacks in one repo:

    backend/   Java   libs/model, services/orders
    frontend/  TS     shared, app   (app depends on shared, via the ts-dts lens)

After recording the dependency index (a full load), each scenario asks
`load_affected` which packages to load for a given change and checks the answer.
The claim the single-language dogfoods don't make is the **cross-language**
partition: a backend edit must never load a frontend package, and vice versa.

  * edit a backend Java package   -> load only it; NO frontend
  * edit the TS library `shared`  -> load shared AND its dependent app; NO backend
  * edit the TS app               -> load only app
  * edit an unreferenced file     -> load nothing

Selection is checked with no build tools at all (it is graph structure, not
compilation). The script then builds for real what the toolchain present can
build — the Java packages here; the TS packages where `tsc` is available
(`BRIX_TSC`, or a global `tsc`) — to confirm the merged graph is buildable, not
just loadable. Git-driven selection (`changed=None`, git enumerates the change
set) is the same call and is proven in `examples/monorepo_dogfood.py`; this
script uses explicit change sets so it needs no git state.

Exits non-zero on any surprise, so it doubles as a check.
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import IncrementalBuilder, load_affected, load_workspace  # noqa: E402

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []

HAVE_JDK = all(shutil.which(t) for t in ("javac", "jar"))
HAVE_TSC = bool(os.environ.get("BRIX_TSC") or shutil.which("tsc"))

# (title, changed paths, packages that must load)
SELECTION_SCENARIOS = [
    (
        "Edit a backend Java lib -> load only it; NO frontend",
        ["backend/libs/model/src/main/java/com/example/model/Model.java"],
        {"backend/libs/model"},
    ),
    (
        "Edit a backend Java service -> load only it; NO frontend",
        ["backend/services/orders/src/main/java/com/example/orders/Orders.java"],
        {"backend/services/orders"},
    ),
    (
        "Edit the TS library -> load shared AND app; NO backend",
        ["frontend/shared/src/index.ts"],
        {"frontend/shared", "frontend/app"},
    ),
    (
        "Edit the TS app -> load only app",
        ["frontend/app/src/main.ts"],
        {"frontend/app"},
    ),
    (
        "Edit an unreferenced file -> load nothing",
        ["notes.txt"],
        set(),
    ),
]


def rel(paths) -> set[str]:
    return {Path(p).resolve().relative_to(ROOT).as_posix() for p in paths}


def check(title: str, changed: list[str], expect: set[str]) -> None:
    got = rel(load_affected(ROOT, changed=changed).patterns)
    ok = got == expect
    print(f"[{'OK ' if ok else 'FAIL'}] {title}")
    print(f"        changed: {', '.join(changed)}")
    print(f"        loaded : {', '.join(sorted(got)) or '(none)'}")
    if not ok:
        FAILURES.append(f"{title}: loaded {sorted(got)}, expected {sorted(expect)}")
    print()


def build_slice(title: str, changed: list[str]) -> None:
    """Build the slice a change selects, for real, and require success."""
    ws = load_affected(ROOT, changed=changed)
    result = IncrementalBuilder(lambda: ws.graph, root=ROOT).build()
    ok = result.success
    print(f"[{'OK ' if ok else 'FAIL'}] build: {title}")
    if not ok:
        FAILURES.append(f"build {title}: {result.errors}")


def main() -> int:
    shutil.rmtree(ROOT / "build", ignore_errors=True)

    print("Recording the dependency index (full load)...\n")
    load_workspace(ROOT).save_index()

    print("== Cross-language selection (no build tools needed) ==\n")
    for title, changed, expect in SELECTION_SCENARIOS:
        check(title, changed, expect)

    print("== Real builds of what the toolchain can build ==\n")
    if HAVE_JDK:
        build_slice(
            "backend Java (model, orders)",
            [
                "backend/libs/model/src/main/java/com/example/model/Model.java",
                "backend/services/orders/src/main/java/com/example/orders/Orders.java",
            ],
        )
        for jar in ("build/backend/model.jar", "build/backend/orders.jar"):
            if not (ROOT / jar).is_file():
                FAILURES.append(f"missing {jar}")
    else:
        print("[skip] no JDK (javac/jar) — backend build not exercised")
    if HAVE_TSC:
        build_slice("frontend TS (shared, app)", ["frontend/shared/src/index.ts"])
        for js in ("build/frontend/shared/index.js", "build/frontend/app/main.js"):
            if not (ROOT / js).is_file():
                FAILURES.append(f"missing {js}")
    else:
        print("[skip] no tsc — frontend build not exercised (set BRIX_TSC)")
    print()

    shutil.rmtree(ROOT / "build", ignore_errors=True)

    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
