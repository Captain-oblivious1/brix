# tested_lib — running tests under brix

A minimal, self-contained example of how you declare tests in a brix build, and
the two things brix does that Maven and Gradle do not:

1. **A test is a cacheable action.** An unchanged tree re-runs zero tests.
2. **Per-class impact selection, by discovery.** When something *does* change,
   brix re-runs only the test classes that actually used the changed code.

## What's here

```
src/com/acme/Adder.java        # two trivial, independent units
src/com/acme/Greeter.java
test/com/acme/AdderTest.java   # one test per unit
test/com/acme/GreeterTest.java
brix_build.py                  # a jar + a JUnitTestAction
```

`AdderTest` uses only `Adder`, `GreeterTest` only `Greeter`. That independence is
what selection exploits.

## How you declare the tests

One action — no selection parameter, nothing special:

```python
java.JUnitTestAction(
    name="lib tests",
    test_sources=d.glob("test/com/acme/{name}.java"),  # the .java tests
    classpath=[d.file(LIB_CLASSES)],                    # the EXPLODED code under test
    console_jar=CONSOLE,                                # the JUnit runner
    javac_worker=worker,
    options=["--release", "17", "-encoding", "UTF-8"],
)
```

It lowers to the test compile plus **one run action per test class** (an
`ExpandAll` over the compiled classes). Each run executes its class under the
JVM's class-load logging (`-Xlog:class+load`) and records the `.class` files it
loaded as a depfile — the same channel a C++ compile uses to record the headers
it `#include`d. brix folds those loaded classes into the run's fingerprint, so a
test re-runs only when a class it actually loaded changes. There is no eval-time
analysis and no `--select-class` planning: the incremental engine does the
selecting, because each test class is its own action.

Note `classpath` points at the compiler's **exploded** `build/classes/lib`, not
the packaged jar — so a single class's change is visible on its own and selection
can be per-class. (Third-party libraries stay whole-jar: coarse but sound.)

## Run it

```
python brix_build.py build
```

Requires a `junit-platform-console-standalone` jar. Set
`BRIX_JUNIT_CONSOLE_JAR=/path/to/...jar`, or drop one under
`~/Projects/brix_test/*/lib/`.

### The sequence, and what brix does

```
# 1. cold build — every test runs (nothing discovered yet)
run: run lib tests com/acme/AdderTest
run: run lib tests com/acme/GreeterTest
BUILD OK

# 2. build again, no changes — both replay from cache
ok: run lib tests com/acme/AdderTest (replayed)
ok: run lib tests com/acme/GreeterTest (replayed)

# 3. edit Adder's implementation, build
ok:  compile lib tests (replayed)               # tests NOT recompiled — Adder's ABI is unchanged
ok:  run lib tests com/acme/GreeterTest (replayed)   # never loaded Adder
run: run lib tests com/acme/AdderTest           # loaded Adder → re-runs

# 4. build again, no changes — both replay
```

| Observation | Why |
|---|---|
| Step 2 runs nothing | Each run is a cacheable action; no loaded class changed, so its recorded pass is restored. |
| Step 3 does **not** recompile the tests | The test *compile* depends on `Adder` through its **ABI**; an implementation edit changes no signature. |
| Step 3 re-runs `AdderTest` | Its depfile records that it loaded `Adder.class`, which changed. |
| Step 3 replays `GreeterTest` | It never loaded `Adder`, so it is provably unaffected. |

## The guarantee

A selected run reaches the same verdict as a full run. Discovery makes this the
*safe* direction: a test re-runs whenever a class it loaded changed, and a class a
test never loaded cannot affect its result. Reflection needs no special handling —
a reflectively loaded class is just a loaded class, so it lands in the depfile
like any other. The equivalence (no test that would fail is ever skipped) is held
down by an oracle that runs real JUnit:
`tests/test_testimpact_java_discovery_oracle.py`.

## Scope

Per-test-**class** selection for Java. C++ (`tested_cpp`) and TypeScript
(`tested_ts`) select the same way — each test its own tracked action — over the
same discover-don't-predict model (`docs/test-impact-selection.md`).
