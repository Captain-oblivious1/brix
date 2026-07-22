#!/usr/bin/env python3
"""Acceptance benchmark: brix (and optionally Gradle) build-times on the
JUnit 5 platform modules, for the scenarios in docs/java-fast-builds.md.

    python examples/junit5/benchmark.py            # brix only
    python examples/junit5/benchmark.py --gradle   # also run Gradle

Each scenario prints best-of-N wall-clock seconds. The brix numbers are
apples-to-apples across scenarios; the Gradle column is a rough reference —
Gradle's `:module:jar` does strictly more per module (multi-release sources,
OSGi manifests, module-info, resources), so it is not a pure compile race.
The scenarios that matter for the edit-compile loop are no-op, impl-only, and
API change, where both tools should do minimal work and the question is
incremental detection + JVM warmth.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("BRIX_JUNIT5_ROOT", os.path.expanduser("~/Projects/brix_test/junit5")))
BRIX = [sys.executable, str(HERE / "brix_build.py")]
COMMONS_REL = "junit-platform-commons/src/main/java/org/junit/platform/commons/util/Preconditions.java"
COMMONS = ROOT / COMMONS_REL

MODULES = [
    "junit-platform-commons", "junit-platform-engine", "junit-platform-suite-api",
    "junit-platform-launcher", "junit-platform-testkit", "junit-platform-suite-commons",
    "junit-platform-suite-engine",
]

ANCHOR = "\tprivate Preconditions() {\n\t\t/* no-op */\n\t}\n"


def run(cmd, cwd=None, env=None) -> tuple[float, bool, str]:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    dt = time.perf_counter() - t0
    return dt, r.returncode == 0, r.stdout


def best_of(cmd, n=3, cwd=None, setup=None, env=None) -> tuple[float, str]:
    best, last = float("inf"), ""
    for _ in range(n):
        if setup:
            setup()
        dt, ok, out = run(cmd, cwd=cwd, env=env)
        last = out
        if not ok:
            return -1.0, out
        best = min(best, dt)
    return best, last


def git_restore():
    subprocess.run(["git", "-C", str(ROOT), "checkout", "--", COMMONS_REL],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def edit_commons(kind: str):
    """impl: a private method body (ABI-neutral). api: a public method (ABI change)."""
    git_restore()
    text = COMMONS.read_text()
    stamp = int(time.time() * 1000)
    if kind == "impl":
        member = f"\n\t@SuppressWarnings(\"unused\")\n\tprivate static int brixBench() {{ return {stamp % 1000}; }}\n"
    else:
        member = f"\n\tpublic static int brixBench{stamp}() {{ return {stamp % 1000}; }}\n"
    COMMONS.write_text(text.replace(ANCHOR, ANCHOR + member, 1))


def kill_daemons():
    base = ROOT / "build/.brix/workers"
    if base.exists():
        for ptr in base.rglob("*.sock"):
            try:
                sock = Path(ptr.read_text().strip())
                os.kill(int(sock.with_suffix(".pid").read_text()), signal.SIGKILL)
            except Exception:
                pass
    subprocess.run(["rm", "-rf", str(base)], check=False)


def rm(*rel):
    for r in rel:
        subprocess.run(["rm", "-rf", str(ROOT / r)], check=False)


def brix_scenarios() -> dict[str, float]:
    out: dict[str, float] = {}

    # 1. clean build, cold cache, cold worker.
    kill_daemons()
    rm("build/.brix", "build/.brix-cache", "build/libs", "build/classes", "build/.brix-staging")
    dt, log = best_of(BRIX + ["build", "--no-daemon"], n=1)
    out["clean, cold cache"] = dt

    # 2. clean build, warm brix cache (keep .brix-cache, drop metadata+outputs).
    rm("build/.brix", "build/libs", "build/classes", "build/.brix-staging")
    dt, _ = best_of(BRIX + ["build", "--no-daemon"], n=1)
    out["clean, warm cache"] = dt

    # 3. no-op (everything fresh).
    out["no-op"], _ = best_of(BRIX + ["build", "--no-daemon"], n=3)

    # Warm the cross-build worker for the edit scenarios.
    run(BRIX + ["build", "--worker-timeout", "300"])

    # 4. impl-only change in the lowest module, warm worker.
    out["impl change (warm worker)"], _ = best_of(
        BRIX + ["build", "--worker-timeout", "300"], n=3, setup=lambda: edit_commons("impl"))
    # 4b. same, no daemon (cold worker each build) — the daemon's value.
    out["impl change (no daemon)"], _ = best_of(
        BRIX + ["build", "--no-daemon"], n=3, setup=lambda: edit_commons("impl"))

    # 5. API change in the lowest module, warm worker (dependents cascade).
    run(BRIX + ["build", "--worker-timeout", "300"])
    out["API change (warm worker)"], _ = best_of(
        BRIX + ["build", "--worker-timeout", "300"], n=3, setup=lambda: edit_commons("api"))

    git_restore()
    run(BRIX + ["build", "--worker-timeout", "300"])  # leave tree clean+fresh
    return out


def gradle_scenarios() -> dict[str, float]:
    # Gradle 8.14 needs a Java 21 toolchain (JUnit's daemon requirement); JDK
    # 26 is the system default. Use a locally-provisioned 21 (see the README).
    jdk = next(iter(sorted(
        Path(os.path.expanduser("~/Projects/brix_test/jdk")).glob("jdk-21*"))), None)
    if jdk is None:
        return {"(no local JDK 21 — see benchmark README)": -1.0}
    env = dict(os.environ, JAVA_HOME=str(jdk), PATH=f"{jdk}/bin:{os.environ['PATH']}")
    base = [str(ROOT / "gradlew"), f"-Porg.gradle.java.installations.paths={jdk}", "-q",
            *(f":{m}:jar" for m in MODULES)]
    gw = base + ["--offline"]  # measured runs: no network freshness checks
    out: dict[str, float] = {}
    run(base, cwd=ROOT, env=env)  # warm daemon + resolve all deps online (not measured)
    out["no-op"], _ = best_of(gw, n=3, cwd=ROOT, env=env)
    out["impl change (warm daemon)"], _ = best_of(
        gw, n=3, cwd=ROOT, env=env, setup=lambda: edit_commons("impl"))
    out["API change (warm daemon)"], _ = best_of(
        gw, n=3, cwd=ROOT, env=env, setup=lambda: edit_commons("api"))
    git_restore()
    run(gw, cwd=ROOT, env=env)
    return out


def fmt(dt: float) -> str:
    return "FAILED" if dt < 0 else f"{dt:6.2f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gradle", action="store_true", help="also benchmark Gradle")
    args = ap.parse_args()

    print(f"JUnit 5 @ {ROOT}\n7 platform modules, best-of-N wall-clock\n")
    brix = brix_scenarios()
    width = max(len(k) for k in brix) + 2
    print(f"{'scenario':<{width}}  {'brix':>8}")
    for k, v in brix.items():
        print(f"{k:<{width}}  {fmt(v):>8}")

    if args.gradle:
        print("\n--- Gradle (:module:jar x7; does more per module) ---")
        grad = gradle_scenarios()
        for k, v in grad.items():
            print(f"{k:<{width}}  {fmt(v):>8}")


if __name__ == "__main__":
    main()
