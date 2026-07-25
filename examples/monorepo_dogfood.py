#!/usr/bin/env python3
"""End-to-end dogfood of the monorepo stack, on the workflow the air-gapped
trial will actually run.

It stitches together everything the recent work added and runs it as a user
would — no test scaffolding, real `cc`/`ar`, the default (Rust) engine:

* **hierarchical build files** — two packages (`services/util`, `services/app`)
  each with their own `brix_build.py`, discovered and merged (tier 1);
* **derived cross-package dependency** — `app` links `util`'s static library,
  so brix infers app→util with no declaration (tier 2);
* **git-driven lazy loading** — `load_affected(root)` asks git what changed
  since the last build and loads only the affected packages (tier 3);
* **git-accelerated incremental build** — `detector=GitChangeDetector`, now
  native on the Rust engine, so unchanged tracked files are vouched without a
  stat, and the baseline written by the Rust build is read back by the Python
  change enumeration (cross-engine metadata interop);
* **C header-dep tracking** — the `-MMD` depfile channel, so a header edit
  re-stales the compiles that include it.

Runs a realistic edit sequence and checks *what actually rebuilt* against what
should have, plus the program's output byte-for-byte. Needs `cc`, `ar`, `git`.

    python examples/monorepo_dogfood.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("BRIX_REPO", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "src"))

from brix import GitChangeDetector, IncrementalBuilder, Reporter, load_affected  # noqa: E402


# --- the workspace ----------------------------------------------------------

UTIL_H = "int util_value(void);\n"
UTIL_C = "#include \"util.h\"\nint util_value(void) {{ return {value}; }}\n"
APP_C = (
    '#include <stdio.h>\n#include "util.h"\n'
    "int main(void) {{ printf(\"%d\\n\", util_value() + {bump}); return 0; }}\n"
)

UTIL_BRIX = '''\
from brix import Graph, cpp
from brix.data import ToolData

def create_graph():
    g = Graph(); d = g.data
    g.add(cpp.CompileSourceAction(
        source_file=d.glob("services/util/src/{name}.c"),
        object_file=d.glob("build/util/{name}.o"),
        compiler=ToolData("cc"),
        compile_options=["-Iservices/util/include"],
    ))
    g.add(cpp.ArchiveAction(
        object_files=d.glob("build/util/{name}.o"),
        output_file=d.file("build/util/libutil.a"),
        ar=ToolData("ar"),
    ))
    return g
'''

APP_BRIX = '''\
from brix import Graph, cpp
from brix.data import DataUnion, ToolData

def create_graph():
    g = Graph(); d = g.data
    g.add(cpp.CompileSourceAction(
        source_file=d.glob("services/app/src/{name}.c"),
        object_file=d.glob("build/app/{name}.o"),
        compiler=ToolData("cc"),
        compile_options=["-Iservices/util/include"],
    ))
    # Linking against util's libutil.a is the *only* cross-package reference —
    # brix derives app depends-on util from it, with no declaration.
    g.add(cpp.LinkApplicationAction(
        object_files=DataUnion(
            d.glob("build/app/{name}.o"), d.file("build/util/libutil.a")
        ),
        output_file=d.file("build/app/app"),
        linker=ToolData("cc"),
    ))
    return g
'''


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def make_workspace(root: Path, util_value: int, app_bump: int) -> None:
    # Build output MUST be gitignored: git-driven lazy loading treats untracked
    # files as changes, and an un-ignored build/ would make every no-op build
    # "see" its own outputs as changes (they match the packages' output
    # patterns) and reload everything. Real repos ignore their build dir; this
    # is the one precondition the trial's repo has to satisfy.
    write(root, ".gitignore", "build/\n")
    write(root, "services/util/include/util.h", UTIL_H)
    write(root, "services/util/src/util.c", UTIL_C.format(value=util_value))
    write(root, "services/util/brix_build.py", UTIL_BRIX)
    write(root, "services/app/src/app.c", APP_C.format(bump=app_bump))
    write(root, "services/app/brix_build.py", APP_BRIX)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


# --- the harness ------------------------------------------------------------

class Ran(Reporter):
    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


def run_build(root: Path):
    """One `load_affected` + git-accelerated incremental build, exactly as the
    trial's root brix_build.py would. Returns (ran labels, loaded package names)."""
    rep = Ran()
    ws = load_affected(root)  # changed=None -> git enumerates what changed
    result = IncrementalBuilder(
        lambda: ws.graph, root=root, detector=GitChangeDetector
    ).build(reporter=rep)
    assert result.success, result.errors
    loaded = {p.relative_to(root).as_posix() for p in ws.patterns}
    return sorted(rep.labels), loaded


def app_output(root: Path) -> str:
    exe = root / "build/app/app"
    if not exe.exists():
        return "<missing binary>"
    return subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip()


PASS, FAIL = "PASS", "FAIL"
failures: list[str] = []


def check(title: str, ok: bool, detail: str) -> None:
    print(f"[{PASS if ok else FAIL}] {title}\n        {detail}")
    if not ok:
        failures.append(title)


def main() -> int:
    for tool in ("cc", "ar", "git"):
        if shutil.which(tool) is None:
            print(f"skipping: {tool} not on PATH")
            return 0

    root = Path(tempfile.mkdtemp(prefix="brix-monorepo-dogfood-"))
    try:
        make_workspace(root, util_value=41, app_bump=1)
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "Dogfood")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "initial")

        # 1. Cold build: no index yet -> full load; everything compiles.
        ran, loaded = run_build(root)
        check(
            "Cold build loads both packages and produces a working binary",
            loaded == {"services/util", "services/app"}
            and "compile util" in ran and "compile app" in ran
            and "link build/app/app" in ran and app_output(root) == "42",
            f"loaded={sorted(loaded)} ran={ran} output={app_output(root)!r}",
        )

        # 2. No-op: git reports nothing changed since the last build -> load and
        #    run nothing, and the existing outputs are left untouched (a subset
        #    that selects zero packages must not reap the unloaded outputs).
        ran, loaded = run_build(root)
        check(
            "No-op build loads nothing, runs nothing, preserves the binary",
            ran == [] and loaded == set() and app_output(root) == "42",
            f"loaded={sorted(loaded)} ran={ran} output={app_output(root)!r}",
        )

        # 3. Edit util's source (41 -> 99). Git detects it; the selector pulls in
        #    util AND its dependent app. util recompiles + re-archives and app
        #    RE-LINKS, but app.o replays (app.c/util.h are git-vouched unchanged).
        write(root, "services/util/src/util.c", UTIL_C.format(value=99))
        ran, loaded = run_build(root)
        check(
            "Editing util rebuilds util + relinks app, but replays app's compile",
            loaded == {"services/util", "services/app"}
            and "compile util" in ran and "ar build/util/libutil.a" in ran
            and "link build/app/app" in ran and "compile app" not in ran
            and app_output(root) == "100",
            f"loaded={sorted(loaded)} ran={ran} output={app_output(root)!r}",
        )

        # 4. Edit app's source (+1 -> +2). Only app's compile+link run; util does
        #    no work (its inputs are unchanged since the last build, so it
        #    replays even though git still lists util.c as dirty).
        write(root, "services/app/src/app.c", APP_C.format(bump=2))
        ran, loaded = run_build(root)
        check(
            "Editing app rebuilds only app; util does no work",
            "compile app" in ran and "link build/app/app" in ran
            and "compile util" not in ran and "ar build/util/libutil.a" not in ran
            and app_output(root) == "101",
            f"ran={ran} output={app_output(root)!r}",
        )

        # 5. Edit util's HEADER. It is a *discovered* (depfile) input of both
        #    compiles, unattributable to a package without loading, so the
        #    selector conservatively full-loads — and both compiles that include
        #    it re-run, proving cross-package header tracking end to end.
        write(root, "services/util/include/util.h", UTIL_H + "/* touched */\n")
        ran, loaded = run_build(root)
        check(
            "Editing a shared header full-loads and rebuilds both includers",
            loaded == {"services/util", "services/app"}
            and "compile util" in ran and "compile app" in ran
            and app_output(root) == "101",
            f"loaded={sorted(loaded)} ran={ran} output={app_output(root)!r}",
        )

        print()
        if failures:
            print(f"DOGFOOD FAILED: {len(failures)} check(s) — {', '.join(failures)}")
            return 1
        print("DOGFOOD PASSED: the full monorepo stack composes end to end.")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
