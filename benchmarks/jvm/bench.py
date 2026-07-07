#!/usr/bin/env python3
"""JVM build benchmark: brix (phase 2, per-build javac worker) vs Gradle.

Generates one multi-module Java project in two forms — a Gradle build and a
brix build — with a *matched* dependency structure so both recompile the same
set of modules in every scenario, then times the five scenarios from
docs/java-fast-builds.md:

  1. clean build, cold cache          (first build ever)
  2. clean build, warm cache          (brix cache vs Gradle build cache)
  3. no-op                            (nothing to do)
  4. implementation-only change        in the lowest module (`base`)
  5. API change                        in the lowest module (`base`)

Project shape: `base` (lowest) <- lib1, lib2, lib3 <- app. Default package
throughout (so brix's per-module .class tracking is exact — nested-package
tracking is documented future work), cross-module references resolved on the
classpath. `base` is on the libs' compile classpath but NOT on app's, in both
builds, so the recompilation sets match: an impl-only change to `base`
recompiles only `base`; an API change recompiles `base` + the libs (their own
ABI is unchanged, so app stays fresh) in both systems.

Both builds are invoked as a user would (a subprocess), so each system pays
its own startup: brix pays Python + a cold javac-worker JVM per build; Gradle
pays its daemon (kept warm except in the cold scenario).

Run:  python3 benchmarks/jvm/bench.py [--reps N]
Requires: gradle and a JDK on PATH.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BRIX_SRC = (Path(__file__).resolve().parents[2] / "src").resolve()
N_LIBS = 3
EXPECTED_OUTPUT = "276"  # App prints this; a cheap correctness check


# -- project generation ------------------------------------------------------


def sources() -> dict[str, dict[str, str]]:
    """{module: {ClassName: java source}} — default package throughout."""
    mods: dict[str, dict[str, str]] = {}
    base = {}
    for i in range(4):
        base[f"Base{i}"] = f"public class Base{i} {{ public static int v{i}(){{ return {i}; }} }}\n"
    mods["base"] = base
    for k in range(1, N_LIBS + 1):
        lib = {}
        for j in range(4):
            lib[f"L{k}_{j}"] = (
                f"public class L{k}_{j} {{ public static int f(){{ return Base{j}.v{j}() + {k * 10 + j}; }} }}\n"
            )
        mods[f"lib{k}"] = lib
    app = {}
    for j in range(4):
        terms = " + ".join(f"L{k}_{j}.f()" for k in range(1, N_LIBS + 1))
        app[f"A{j}"] = f"public class A{j} {{ public static int g(){{ return {terms}; }} }}\n"
    calls = " + ".join(f"A{j}.g()" for j in range(4))
    app["App"] = (
        "public class App { public static void main(String[] a){ System.out.println("
        + calls
        + "); } }\n"
    )
    mods["app"] = app
    return mods


BASE0_ORIG = sources()["base"]["Base0"]


def base0_impl(rep: int) -> str:
    # A *novel* implementation each rep (unique return value) so every build is
    # a real cache-miss recompile, not a cache replay. ABI unchanged (v0's
    # signature is fixed), so only `base` rebuilds.
    return f"public class Base0 {{ public static int v0(){{ return {1000 + rep}; }} }}\n"


def base0_api(rep: int) -> str:
    # A novel public method each rep -> a fresh ABI each time -> base + libs
    # recompile for real (their classpath ABI changed).
    return (
        "public class Base0 { public static int v0(){ return 0; }"
        f" public static int extra{rep}(){{ return {rep}; }} }}\n"
    )


def write_gradle(root: Path) -> None:
    mods = sources()
    for module, classes in mods.items():
        src = root / module / "src" / "main" / "java"
        src.mkdir(parents=True, exist_ok=True)
        for name, text in classes.items():
            (src / f"{name}.java").write_text(text)
    (root / "settings.gradle").write_text(
        "rootProject.name = 'bench'\n"
        + "include " + ", ".join(f"'{m}'" for m in mods) + "\n"
        + "buildCache { local { directory = new File(rootDir, 'build-cache') } }\n"
    )
    (root / "gradle.properties").write_text("org.gradle.caching=true\n")
    (root / "base" / "build.gradle").write_text("plugins { id 'java-library' }\n")
    for k in range(1, N_LIBS + 1):
        (root / f"lib{k}" / "build.gradle").write_text(
            "plugins { id 'java-library' }\n"
            "dependencies { implementation project(':base') }\n"
        )
    app_deps = "\n".join(
        f"    implementation project(':lib{k}')" for k in range(1, N_LIBS + 1)
    )
    (root / "app" / "build.gradle").write_text(
        "plugins { id 'java-library' }\n"
        "dependencies {\n"
        f"{app_deps}\n"
        "}\n"
    )


BRIX_TEMPLATE = '''#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, {brix_src!r})
from brix import CacheBuilder, Graph, IncrementalBuilder, cli, java

HERE = Path(__file__).parent


def make_builder():
    # Cache innermost so scenario 2 (warm cache, no metadata) restores instead
    # of recompiling; incremental over it so no-op/edit scenarios replay.
    return CacheBuilder(
        IncrementalBuilder(create_graph, root=HERE, metadata_dir="build/.brix"),
        cache_dir="build/.brix-cache",
    )


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    worker = java.javac_worker(g)
    g.add(
        java.JarAction(
            source_files=d.glob("base/{{name}}.java"),
            output_file=d.file("build/base.jar"),
            classes_dir="build/base-classes",
            javac_worker=worker,
        ),
    )
{lib_actions}
    g.add(
        java.JarAction(
            source_files=d.glob("app/{{name}}.java"),
            output_file=d.file("build/app.jar"),
            classes_dir="build/app-classes",
            classpath=[{app_cp}],
            javac_worker=worker,
            entry_point="App",
        ),
    )
    return g


if __name__ == "__main__":
    sys.exit(cli.main(create_graph, root=HERE, builder_factory=make_builder))
'''


def write_brix(root: Path) -> None:
    mods = sources()
    for module, classes in mods.items():
        d = root / module
        d.mkdir(parents=True, exist_ok=True)
        for name, text in classes.items():
            (d / f"{name}.java").write_text(text)
    lib_actions = ""
    for k in range(1, N_LIBS + 1):
        lib_actions += (
            "    g.add(\n"
            "        java.JarAction(\n"
            f'            source_files=d.glob("lib{k}/{{name}}.java"),\n'
            f'            output_file=d.file("build/lib{k}.jar"),\n'
            f'            classes_dir="build/lib{k}-classes",\n'
            '            classpath=[d.file("build/base.jar")],\n'
            "            javac_worker=worker,\n"
            "        ),\n"
            "    )\n"
        )
    app_cp = ", ".join(f'd.file("build/lib{k}.jar")' for k in range(1, N_LIBS + 1))
    (root / "brix.py").write_text(
        BRIX_TEMPLATE.format(
            brix_src=str(BRIX_SRC),
            lib_actions=lib_actions.rstrip("\n"),
            app_cp=app_cp,
        )
    )


# -- running -----------------------------------------------------------------


def run(cmd: list[str], cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def timed(cmd: list[str], cwd: Path) -> float:
    start = time.perf_counter()
    run(cmd, cwd)
    return time.perf_counter() - start


GRADLE = ["gradle", "--console=plain", "-q"]


def gradle_build(root: Path, *, build_cache: bool) -> list[str]:
    args = list(GRADLE) + ["assemble"]
    args.append("--build-cache" if build_cache else "--no-build-cache")
    return args


def brix_cmd() -> list[str]:
    return [sys.executable, "brix.py", "build", "build/app.jar"]


def app_output_brix(root: Path) -> str:
    jars = ":".join(
        str(root / "build" / j)
        for j in ["app.jar", "lib1.jar", "lib2.jar", "lib3.jar", "base.jar"]
    )
    out = subprocess.run(["java", "-cp", jars, "App"], cwd=root, capture_output=True, text=True)
    return out.stdout.strip()


def app_output_gradle(root: Path) -> str:
    jars = ":".join(
        str(root / m / "build" / "libs" / f"{m}.jar")
        for m in ["app", "lib1", "lib2", "lib3", "base"]
    )
    out = subprocess.run(["java", "-cp", jars, "App"], cwd=root, capture_output=True, text=True)
    return out.stdout.strip()


# -- reset helpers -----------------------------------------------------------


def brix_wipe_all(root: Path) -> None:
    shutil.rmtree(root / "build", ignore_errors=True)


def brix_wipe_outputs_keep_cache(root: Path) -> None:
    build = root / "build"
    for child in build.iterdir() if build.exists() else []:
        if child.name == ".brix-cache":
            continue
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()


def gradle_stop() -> None:
    subprocess.run(["gradle", "--stop"], capture_output=True, text=True)


def gradle_clean_all(root: Path) -> None:
    for m in sources():
        shutil.rmtree(root / m / "build", ignore_errors=True)
    shutil.rmtree(root / "build-cache", ignore_errors=True)
    shutil.rmtree(root / ".gradle", ignore_errors=True)


def set_base0(gradle_root: Path, brix_root: Path, text: str) -> None:
    (gradle_root / "base" / "src" / "main" / "java" / "Base0.java").write_text(text)
    (brix_root / "base" / "Base0.java").write_text(text)


def best(times: list[float]) -> tuple[float, float]:
    return min(times), statistics.median(times)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    if not shutil.which("gradle"):
        print("gradle not on PATH", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="brix-jvm-bench-"))
    g = root / "gradle"
    b = root / "brix"
    g.mkdir()
    b.mkdir()
    print(f"workspace: {root}")
    write_gradle(g)
    write_brix(b)

    results: dict[str, dict[str, tuple[float, float]]] = {}

    def record(name: str, brix_times: list[float], gradle_times: list[float]) -> None:
        results[name] = {"brix": best(brix_times), "gradle": best(gradle_times)}
        bm, _ = best(brix_times)
        gm, _ = best(gradle_times)
        print(f"  {name:28s} brix {bm:6.2f}s   gradle {gm:6.2f}s")

    # Correctness + warmup (first Gradle build also downloads/《warms》 the daemon).
    print("validating both builds produce identical output...")
    run(brix_cmd(), b)
    run(gradle_build(g, build_cache=True), g)
    assert app_output_brix(b) == EXPECTED_OUTPUT, app_output_brix(b)
    assert app_output_gradle(g) == EXPECTED_OUTPUT, app_output_gradle(g)
    print(f"  both print {EXPECTED_OUTPUT} ✓")

    # 3. no-op (everything already built and fresh)
    print("scenario 3: no-op")
    bt = [timed(brix_cmd(), b) for _ in range(args.reps)]
    gt = [timed(gradle_build(g, build_cache=True), g) for _ in range(args.reps)]
    record("3. no-op", bt, gt)

    # 4. implementation-only change in base (novel each rep -> real recompile)
    print("scenario 4: impl-only change in base")
    bt, gt = [], []
    for rep in range(args.reps):
        set_base0(g, b, base0_impl(rep))
        bt.append(timed(brix_cmd(), b))
        gt.append(timed(gradle_build(g, build_cache=True), g))
    set_base0(g, b, BASE0_ORIG)  # reset to a known state for later scenarios
    run(brix_cmd(), b)
    run(gradle_build(g, build_cache=True), g)
    record("4. impl-only change", bt, gt)

    # 5. API change in base (novel method each rep -> real recompile of base + libs)
    print("scenario 5: API change in base")
    bt, gt = [], []
    for rep in range(args.reps):
        set_base0(g, b, base0_api(rep))
        bt.append(timed(brix_cmd(), b))
        gt.append(timed(gradle_build(g, build_cache=True), g))
    set_base0(g, b, BASE0_ORIG)
    run(brix_cmd(), b)
    run(gradle_build(g, build_cache=True), g)
    record("5. API change", bt, gt)

    # 2. clean build, warm cache
    print("scenario 2: clean build, warm cache")
    bt, gt = [], []
    for _ in range(max(2, args.reps - 1)):
        brix_wipe_outputs_keep_cache(b)
        bt.append(timed(brix_cmd(), b))
        for m in sources():
            shutil.rmtree(g / m / "build", ignore_errors=True)  # keep build-cache/
        gt.append(timed(gradle_build(g, build_cache=True), g))
    record("2. clean, warm cache", bt, gt)

    # 1. clean build, cold cache (slowest; fewer reps)
    print("scenario 1: clean build, cold cache")
    bt, gt = [], []
    for _ in range(2):
        brix_wipe_all(b)
        bt.append(timed(brix_cmd(), b))
        gradle_stop()
        gradle_clean_all(g)
        gt.append(timed(gradle_build(g, build_cache=False), g))
    record("1. clean, cold cache", bt, gt)

    # -- report --------------------------------------------------------------
    order = [
        "1. clean, cold cache",
        "2. clean, warm cache",
        "3. no-op",
        "4. impl-only change",
        "5. API change",
    ]
    print("\n## Results (best-of-N wall-clock, seconds)\n")
    print(f"| {'scenario':22s} | {'brix':>8s} | {'gradle':>8s} | {'winner':>8s} |")
    print(f"|{'-'*24}|{'-'*10}|{'-'*10}|{'-'*10}|")
    lines = []
    for name in order:
        bm = results[name]["brix"][0]
        gm = results[name]["gradle"][0]
        winner = "brix" if bm < gm else "gradle"
        ratio = (max(bm, gm) / max(min(bm, gm), 1e-6))
        row = f"| {name:22s} | {bm:8.2f} | {gm:8.2f} | {winner:>8s} |"
        print(row)
        lines.append(f"{name}: brix {bm:.2f}s vs gradle {gm:.2f}s -> {winner} ({ratio:.1f}x)")
    print(f"\nworkspace kept at: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
