# Multi-module Jackson builds and selective tests

This dogfood builds the real Jackson 2.19.4 core modules as one Brix graph:

```text
jackson-annotations ──┐
                     ├──> jackson-databind
jackson-core ─────────┘
```

All 46 annotations sources, 135 core sources, and 480 databind sources compile
into Brix-owned jars. Each module has a small JUnit smoke suite. The production
sources are real Jackson; the small suites keep this example focused on build
selection rather than recreating every third-party test dependency from the
projects' Maven builds.

## What it proves

Compilation sees dependent jars through Brix's `jvm-abi` lens, while test runs
see their complete runtime bytes. Therefore an implementation-only core edit:

- recompiles and re-jars `jackson-core`;
- does not recompile `jackson-databind` or either affected test suite, because
  core's public ABI is unchanged;
- reruns the core and databind suites, because both execute against core;
- leaves the annotations compile and suite replayed, because annotations is not
  downstream of core.

The graph also retains the original dogfood coverage: generated
`PackageVersion` sources, packaged `META-INF/services` resources, the
fastdoubleparser compile dependency, the warm javac worker, caching, and three
real-project jars.

## Setup

```sh
mkdir -p ~/Projects/brix_test
git clone --filter=blob:none --branch jackson-core-2.19.4 \
  https://github.com/FasterXML/jackson-core.git \
  ~/Projects/brix_test/jackson-core
git clone --filter=blob:none --branch jackson-annotations-2.19.4 \
  https://github.com/FasterXML/jackson-annotations.git \
  ~/Projects/brix_test/jackson-annotations
git clone --filter=blob:none --branch jackson-databind-2.19.4 \
  https://github.com/FasterXML/jackson-databind.git \
  ~/Projects/brix_test/jackson-databind
examples/jackson/fetch-deps.sh
```

Set `BRIX_JACKSON_ROOT` to a different directory containing those three sibling
checkouts. For compatibility, it may also point directly to `jackson-core`.

## Run

```sh
python examples/jackson/brix_build.py build
python examples/jackson/brix_build.py status
python examples/jackson/verify_selection.py
```

`verify_selection.py` first establishes a fresh baseline, temporarily adds a
private method to `JsonFactory`, and checks the exact actions that run. It
restores the source in a `finally` block and rebuilds, leaving the checkout and
Brix metadata fresh even if an assertion fails.

## The real databind suite (per-class test-impact selection at scale)

`databind_testimpact.py` goes further than the smoke suites above: it compiles
and runs Jackson's *actual* `databind/src/test/java` tree — 708 test files,
~3,400 tests — as one cached `JUnitTestAction` with `plan_suite` per-class
selection (the same mechanism as `examples/tested_lib`, at real scale). A
handful of files needing test-only libraries we don't stage are dropped via the
glob `-` operator.

```sh
python examples/jackson/databind_testimpact.py build        # ~5.5s; unchanged tree replays
python examples/jackson/characterize_databind_selection.py  # what selection actually saves here
```

The characterization is the honest part: on databind, class-level selection
skips almost nothing — nearly every test builds an `ObjectMapper`, which uses
reflection, so it must always run; and even ignoring reflection, `ObjectMapper`
statically pulls in most of the module, so a typical class edit reaches ~the
whole suite. Selection stays provably sound (a peripheral class narrows to a
single test with no missed referencers) — it just has little to skip. How much
class-level selection saves is a property of the codebase's coupling, not the
algorithm; a decoupled framework like JUnit 5 skips ~96% of its suite on a median
edit. This is the concrete case for per-method (runtime-coverage) selection.
