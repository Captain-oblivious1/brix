# Building and testing jackson-core with brix

The selective-testing dogfood: build *and run the tests of* a famous real
Java project, so a suite re-runs only when its inputs actually change. Along
the way it exercises three things a real Java build needs — a generated
source, packaged resources, and a JUnit 5 + AssertJ test suite.

## What it covers

- **Generated source**: `PackageVersion.java` is produced from a `.in`
  template (jackson's maven-replacer step) by a small tool action, and feeds
  the compile.
- **Resources in the jar**: `src/main/resources` (the `META-INF/services`
  `JsonFactory` registration) via `JarAction(resource_dir=…)`.
- **One compile dep**: `ch.randelshofer:fastdoubleparser` (jackson shades it
  into the distributed jar; for us it's a plain classpath entry).
- **Selective test running** via `java.JUnitTestAction`: the test *compile*
  depends on the classpath through the `jvm-abi` lens, the test *run* through
  the **bytes**, so:
  - nothing changed → the suite replays (skipped)
  - a main implementation change → jackson-core recompiles and the suite
    **re-runs** (its runtime classpath bytes changed)
  - a comment-only edit anywhere → recompiles but the byte-identical output
    lets the run **replay**
  - a real test change → the main jar replays, only the tests recompile + run
  - editing a file under `src/test/resources` → the suite re-runs
    (`test_resource_dirs` declares test data as inputs)

## Setup

    git clone --filter=blob:none https://github.com/FasterXML/jackson-core.git \
        ~/Projects/brix_test/jackson-core
    git -C ~/Projects/brix_test/jackson-core checkout jackson-core-2.19.4
    examples/jackson/fetch-deps.sh          # JUnit console + AssertJ + fastdoubleparser

Override the checkout with `BRIX_JACKSON_ROOT`. No atomic layer here: the
JUnit console launcher reads the built jar by its real path.

## Run

    python examples/jackson/brix.py build     # generate + jar + compile + run tests
    python examples/jackson/brix.py status

jackson-core's real suite (232 test sources, JUnit 5 + AssertJ) compiles and
passes; edit a source and rebuild to watch exactly the right work re-run.
