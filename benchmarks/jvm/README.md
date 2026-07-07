# JVM build benchmark: brix vs Gradle

`bench.py` compares brix (JVM phase 2: per-build javac worker) against Gradle
on the five scenarios from `docs/java-fast-builds.md`. Run it with a JDK and
`gradle` on PATH:

```
python3 benchmarks/jvm/bench.py [--reps N]
```

It generates a temp workspace with the **same** multi-module Java project in
two forms — a Gradle build and a brix build — times each scenario as a
subprocess (so each system pays its real startup cost), and prints a table.

## The project

`base` (the lowest module) ← `lib1`, `lib2`, `lib3` ← `app`. Default package
throughout, cross-module references resolved on the classpath.

The dependency wiring is **matched** between the two builds so they recompile
the same modules in every scenario: `base` is on the libs' compile classpath
but **not** on app's (Gradle `implementation project(':base')`; brix
`classpath=[base.jar]` on the libs only). Consequently:

- an **implementation-only** change to `base` recompiles only `base` (both
  systems do ABI-based compile avoidance), and
- an **API** change to `base` recompiles `base` + the libs — the libs' own ABI
  is unchanged, so `app` stays fresh in both systems.

`App` prints a fixed number (276); the harness asserts both builds produce it
before timing, so a broken build can't masquerade as a fast one.

## The scenarios and how each is set up

| # | scenario | brix reset | Gradle reset |
|---|----------|------------|--------------|
| 1 | clean, cold cache | remove `build/` entirely | `gradle --stop`, remove module `build/`, `.gradle/`, `build-cache/`; build `--no-build-cache` |
| 2 | clean, warm cache | remove outputs + metadata, keep `build/.brix-cache` | remove module `build/`, keep `build-cache/`; build `--build-cache` |
| 3 | no-op | (already built) | (already built), warm daemon |
| 4 | impl-only change | rewrite `base/Base0.java`, **novel each rep**, same ABI | same edit |
| 5 | API change | rewrite `base/Base0.java`, **novel method each rep** | same edit |

Two methodology points that matter for honesty:

- The brix build uses a `CacheBuilder(IncrementalBuilder(...))` stack, so
  scenario 2 actually **restores from the cache** (file copies, zero JVMs)
  rather than recompiling.
- Scenarios 4 and 5 apply a **unique** edit on every repetition. A persistent
  cache would otherwise turn the second rep of a repeated edit into a cache
  *hit*, measuring a replay instead of a real recompile. Novel edits keep them
  honest: every timed build there is a genuine cache-miss compile — the same
  situation as a developer's own new change (Gradle's build cache misses on it
  too).

Gradle keeps its daemon warm except in scenario 1; brix starts a **cold**
javac-worker JVM on every build (the per-build worker of phase 2 — the phase 3
cross-build daemon is what would keep it warm *between* builds).

## What to expect, and the honest headline

This is phase-2 brix, so the expected shape (per the design doc) is:

- brix wins **no-op** and **warm-cache clean build** outright — it launches
  *zero* JVMs (replays / cache restores), while Gradle's daemon still runs.
- **cold clean build** is roughly a tie — both are bounded by `javac`.
- **impl-only** and **API change** are close to parity: both systems recompile
  the same modules, and brix's per-build cold JVM roughly offsets Gradle's
  warm daemon. Turning these into wins is exactly what phase 3 (the cross-build
  daemon, keeping the worker warm between builds) is for.

## Sample run

One run on this dev machine (Gradle 9.6.1, JDK 26, `--reps 3`, best-of-N
wall-clock seconds) — treat the ratios, not the absolutes, as the signal:

| scenario            |  brix | gradle | winner |
|---------------------|------:|-------:|--------|
| 1. clean, cold cache| 11.19 |  28.97 | brix   |
| 2. clean, warm cache|  0.53 |   3.27 | brix   |
| 3. no-op            |  1.15 |   3.22 | brix   |
| 4. impl-only change |  2.47 |   3.18 | brix   |
| 5. API change       |  6.16 |   3.38 | gradle |

Reading it:

- **1, 2, 3** — brix wins big: cold clean is bounded by `javac` for both but
  Gradle also pays daemon/config startup; warm-cache and no-op launch zero (or
  few) JVMs for brix while Gradle's daemon still walks the task graph.
- **4 (impl-only)** — brix wins even here: an implementation change to `base`
  recompiles only `base` in both systems, and brix's targeted single-module
  recompile (cold worker included) comes in under Gradle's per-invocation
  overhead.
- **5 (API change)** — Gradle wins: the ABI change recompiles `base` **plus the
  three libs** (four modules), and brix compiles all four through a JVM it
  cold-starts *this build*, while Gradle's daemon is already warm. This is the
  one case where the per-build cold JVM costs more than it saves — and it is
  exactly what **phase 3** (the cross-build daemon, keeping the worker warm
  between builds) is designed to fix. Expect scenario 5 to flip to brix once
  phase 3 lands.

So phase-2 brix already wins four of five, losing only the multi-module
recompile that the daemon would rescue. Caveats: small project (5 modules,
~24 classes), single machine, one run; larger module graphs shift the
compile-bound scenarios and add per-task overhead that tends to favor brix's
targeted rebuilds. Gradle 9.6.1 here vs the doc's "Gradle 8" — same story.
