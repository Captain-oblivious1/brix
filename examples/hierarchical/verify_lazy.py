#!/usr/bin/env python3
"""Prove that lazy loading loads only the packages an edit actually affects.

Run:  python3 examples/hierarchical/verify_lazy.py

The workspace is  core -> api -> web  (a two-hop chain) plus an independent
`docs` package, all using one shared `tools/step.py`. After an initial full
build (which records the dependency index), each scenario edits one file, asks
`load_affected` which packages to load for that change, and checks the answer:

  * edit a leaf source     -> load that package AND its transitive dependents
  * edit the independent pkg -> load only it
  * edit the shared tool    -> load EVERYTHING (it is an input of every package;
                               this is the soundness guarantee, not a miss)
  * edit an unreferenced file -> load NOTHING (zero work)

Each scenario also builds the selected subgraph and confirms the outputs match
what a from-scratch full build produces — so "loaded fewer packages" never means
"shipped a stale build". Exits non-zero on any surprise, so it doubles as a
check.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import IncrementalBuilder, load_affected, load_workspace  # noqa: E402
from brix.workspace import find_packages  # noqa: E402

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def rel(paths) -> set[str]:
    return {Path(p).resolve().relative_to(ROOT).as_posix() for p in paths}


def loaded_packages(workspace) -> set[str]:
    # A package appears in `patterns` only if its create_graph() actually ran.
    return rel(workspace.patterns)


def build(graph_supplier) -> None:
    result = IncrementalBuilder(graph_supplier, root=ROOT).build()
    assert result.success, result.errors


def full_build_outputs() -> dict[str, str]:
    """Build the whole workspace from scratch in a throwaway tree and return its
    outputs, the ground truth each lazy build must reproduce."""
    scratch = ROOT.parent / "_hier_reference"
    shutil.rmtree(scratch, ignore_errors=True)
    shutil.copytree(
        ROOT, scratch, ignore=shutil.ignore_patterns("build", "_hier_reference")
    )
    result = IncrementalBuilder(
        lambda: load_workspace(scratch).graph, root=scratch
    ).build()
    assert result.success, result.errors
    outputs = {
        p.relative_to(scratch / "build").as_posix(): p.read_text()
        for p in sorted((scratch / "build").rglob("*"))
        if p.is_file() and ".brix" not in p.parts
    }
    shutil.rmtree(scratch, ignore_errors=True)
    return outputs


def current_outputs() -> dict[str, str]:
    build_dir = ROOT / "build"
    return {
        p.relative_to(build_dir).as_posix(): p.read_text()
        for p in sorted(build_dir.rglob("*"))
        if p.is_file() and ".brix" not in p.parts
    }


def scenario(title: str, edit, changed: list[str], expect_loaded: set[str]) -> None:
    edit()
    workspace = load_affected(ROOT, changed=changed)
    got = loaded_packages(workspace)
    discovered = rel(find_packages(ROOT)) - {"."}  # the root entry point isn't a pkg
    build(lambda: workspace.graph)

    ok_selection = got == expect_loaded
    ok_outputs = current_outputs() == full_build_outputs()
    status = "OK " if (ok_selection and ok_outputs) else "FAIL"

    print(f"[{status}] {title}")
    print(f"        changed  : {', '.join(changed)}")
    print(f"        loaded   : {', '.join(sorted(got)) or '(none)'}")
    print(f"        skipped  : {', '.join(sorted(discovered - got)) or '(none)'}")
    if not ok_selection:
        FAILURES.append(f"{title}: loaded {sorted(got)}, expected {sorted(expect_loaded)}")
    if not ok_outputs:
        FAILURES.append(f"{title}: outputs diverged from a full build")
    print()


def main() -> int:
    shutil.rmtree(ROOT / "build", ignore_errors=True)

    # Snapshot every file a scenario mutates, so the tree is restored exactly
    # afterwards no matter what the edits were — the script is idempotent.
    mutated = ["libs/core/src.txt", "docs/src.txt", "tools/step.py"]
    originals = {p: (ROOT / p).read_text() for p in mutated}

    # Initial full build: produces every output and records the dependency
    # index the selector reads. (load_affected with no index loads everything.)
    print("Initial full build (records the index)...\n")
    build(lambda: load_affected(ROOT, changed=[]).graph)

    scenario(
        "Edit a leaf source (libs/core/src.txt) -> load the chain, skip docs",
        lambda: (ROOT / "libs/core/src.txt").write_text("hello-v2\n"),
        ["libs/core/src.txt"],
        {"libs/core", "services/api", "services/web"},
    )
    scenario(
        "Edit the independent package (docs/src.txt) -> load only docs",
        lambda: (ROOT / "docs/src.txt").write_text("documentation-v2\n"),
        ["docs/src.txt"],
        {"docs"},
    )
    scenario(
        "Edit the shared tool (tools/step.py) -> load EVERYTHING (soundness)",
        lambda: (ROOT / "tools/step.py").write_text(
            (ROOT / "tools/step.py").read_text() + "\n# touched\n"
        ),
        ["tools/step.py"],
        {"libs/core", "services/api", "services/web", "docs"},
    )
    scenario(
        "Edit an unreferenced file (notes.txt) -> load NOTHING",
        lambda: (ROOT / "notes.txt").write_text("just a note\n"),
        ["notes.txt"],
        set(),
    )

    # Leave the tree as we found it (the edits above are throwaway).
    for p, text in originals.items():
        (ROOT / p).write_text(text)
    (ROOT / "notes.txt").unlink(missing_ok=True)
    shutil.rmtree(ROOT / "build", ignore_errors=True)

    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All scenarios passed: lazy loading is both minimal and complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
