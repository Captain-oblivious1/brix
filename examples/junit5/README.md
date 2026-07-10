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
