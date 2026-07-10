# Building JUnit 5 with brix

A real-world acceptance test: re-implement a famous Gradle project's compile
graph in brix, so the multi-module ABI story (`identity="jvm-abi"`) and the
warm javac worker run against code we didn't write. This is the dogfood that
found a real jvm-abi bug (String-valued annotation elements — every
`@API(since="…")` class — silently fell back to byte hashing, defeating ABI
avoidance; fixed in the same change that added this example).

## Scope

The seven pure-Java `junit-platform-*` modules:

    commons → engine → launcher → suite-commons → suite-engine
                    ↘ suite-api ↗
    launcher → testkit

`jupiter-*` (Kotlin + code generation), `console`/`reporting` (extra jars,
multi-release + shadow packaging), and `vintage` (JUnit 4) are out of this
first cut. External deps are just `apiguardian` + `opentest4j` (both tiny),
plus `assertj-core` + `byte-buddy` for `testkit` alone (it exposes AssertJ in
its public API). brix has no dependency resolver by design — you hand it the
jars — so `fetch-deps.sh` pulls them from Maven Central once.

## Setup

    # clone the checkout (not committed; ~/Projects/brix_test keeps clones
    # out of the Projects root):
    git clone --filter=blob:none https://github.com/junit-team/junit5.git \
        ~/Projects/brix_test/junit5
    git -C ~/Projects/brix_test/junit5 checkout r5.14.4

    examples/junit5/fetch-deps.sh          # the four external jars -> lib/

Override the checkout location with `BRIX_JUNIT5_ROOT`.

## Run

    python examples/junit5/brix.py build            # all seven jars
    python examples/junit5/brix.py build engine     # one module + its deps
    python examples/junit5/brix.py status
    python examples/junit5/brix.py build --no-daemon # no cross-build worker

The build uses the brix in this repo (it puts `../../src` on `sys.path`) and
the Rust engine by default, with the warm javac worker persisting between
builds unless `--no-daemon`.

## What it demonstrates

- **Multi-module dependency inference** from `classpath=` jar overlaps — no
  hand-declared build order.
- **ABI avoidance**: an implementation-only edit in `commons` (a private
  method, a method body) recompiles `commons` alone; the six dependents
  *replay* — their fingerprints carry `commons.jar`'s `jvm-abi` digest, which
  an impl change leaves untouched. An API edit (new public method, changed
  signature) cascades.
- **Warm javac worker** across edit-compile cycles (the JVM stays hot between
  builds; `docs/java-fast-builds.md`).

## Benchmark vs Gradle

`benchmark.py` runs the `docs/java-fast-builds.md` acceptance scenarios for
brix, and (with `--gradle`) the same seven `:module:jar` tasks under Gradle:

    python examples/junit5/benchmark.py            # brix only
    python examples/junit5/benchmark.py --gradle   # also Gradle

Best-of-3 wall-clock, one machine (JDK 26 for brix's javac worker; Gradle 8.14
needs a Java 21 toolchain, provisioned locally under `~/Projects/brix_test/jdk`
since only JDK 26 is installed):

| scenario                     | brix   | Gradle | 
|------------------------------|--------|--------|
| clean build, cold cache      | ~4.3s  |   —    |
| clean build, warm brix cache | ~1.9s  |   —    |
| no-op                        | ~0.09s | ~1.1s  |
| impl-only change (warm)      | ~0.37s | ~1.8s  |
| impl-only change (no daemon) | ~0.81s |   —    |
| API change (warm)            | ~1.3s  | ~1.6s  |

Reading it: on the **edit-compile loop** — no-op and implementation-only
change, the cases that dominate day-to-day work — brix is **~5–12× faster**
than Gradle on the same modules. On a full API-change cascade the two are
close (~1.3s vs ~1.6s). The daemon earns its keep: an impl-only change is
0.37s with the warm worker vs 0.81s spawning javac cold — and even the
cold-worker number beats Gradle's warm daemon, because brix has no
per-invocation daemon/configuration tax (a Python start plus the Rust engine,
~0.1s, vs Gradle's ~1s warm-daemon floor).

Caveat, stated plainly: Gradle's `:module:jar` does strictly more per module
(multi-release sources, `module-info`, OSGi manifests, resource processing),
so the absolute times are not a pure compile race. But the incremental
scenarios measure incremental-detection overhead plus JVM warmth — which *is*
the edit-compile-loop metric — and that overhead is exactly what brix's model
avoids.
