# tested_lib — running tests under brix

A minimal, self-contained example of how you declare tests in a brix build, and
the two things brix does that Maven and Gradle do not:

1. **A test suite is a cacheable action.** An unchanged tree re-runs zero tests.
2. **Impact selection.** When something *does* change, brix runs only the test
   classes that change could reach — not the whole suite.

## What's here

```
src/com/acme/Adder.java        # two trivial, independent units
src/com/acme/Greeter.java
test/com/acme/AdderTest.java   # one suite, one test per unit
test/com/acme/GreeterTest.java
brix_build.py                  # one jar, one suite, selection wired in
```

`AdderTest` depends only on `Adder`, `GreeterTest` only on `Greeter`. That
independence is what selection exploits.

## How you declare the suite

The authoring surface is one action. The only line specific to selection is
`select=` — everything else is how you'd write any test target:

```python
java.JUnitTestAction(
    name="lib tests",
    test_sources=d.glob("test/com/acme/{name}.java"),  # the .java tests
    classpath=[d.file(LIB_JAR)],                        # the code under test
    console_jar=CONSOLE,                               # the JUnit runner
    javac_worker=worker,
    options=["--release", "17", "-encoding", "UTF-8"],
    select=_select(),   # which classes to run this build (see below)
)
```

`_select()` runs at build-file evaluation time — the same place brix's monorepo
lazy-loader decides which packages to load. It reads what changed since the last
build (from brix's own file-hash cache — no VCS required), asks the Java provider
which test classes that reaches, and returns a JUnit `--select-class` list. When
proof runs out — a cold build, a new or renamed class, a change it cannot pin to
a unit — it falls open to `--scan-class-path`, running everything.

## Run it

```
python brix_build.py build
```

Requires a `junit-platform-console-standalone` jar. Set
`BRIX_JUNIT_CONSOLE_JAR=/path/to/...jar`, or drop one under
`~/Projects/brix_test/*/lib/`.

### The sequence, and what brix does

```
# 1. cold build — nothing to select against, so run everything
run: run lib tests
BUILD OK

# 2. build again, no changes — the suite replays from cache
ok: run lib tests (replayed)
BUILD OK

# 3. edit Adder's implementation, build
ok:  compile lib tests (replayed)          # tests NOT recompiled — Adder's ABI is unchanged
run: run lib tests                         # suite RE-RUNS,
  selected: --select-class com.acme.AdderTest   #   but only AdderTest; GreeterTest is skipped

# 4. build again, no changes — replays cleanly, nothing runs
ok: run lib tests (replayed)
BUILD OK
```

Every step is deliberate:

| Observation | Why |
|---|---|
| Step 2 runs nothing | The suite is a cacheable action; no input changed, so its recorded pass is restored. |
| Step 3 does **not** recompile the tests | The test *compile* depends on `Adder` through its **ABI**; an implementation edit changes no signature. |
| Step 3 re-runs the suite | The test *run* depends on the jar's **bytes**; behavior may have changed, so it must run. |
| Step 3 runs **only** `AdderTest` | `GreeterTest`'s one dependency, `Greeter`, did not change — so it is provably unaffected and skipped. |
| Step 4 replays cleanly | Selection reuses the previous command when nothing changed, so a no-op build stays a pure cache hit — no spurious re-run. |

## The guarantee

Selection never trades correctness for speed. The rule that makes it safe: it
changes only *which classes the runner executes*, never *what the suite watches
for staleness* — the suite is still fingerprinted against the whole classpath.
So any change that could matter still re-fires the suite; selection only ever
shrinks the run down to what the change provably reaches. Whenever that proof is
incomplete — reflection, a class named only by a string or by configuration, a
new or renamed unit, a prior failure — it runs everything.

That equivalence (a selected run reaches the same verdict as a full run, across
a whole sequence of edits) is held down by an oracle that runs real JUnit:
`tests/test_testimpact_java_oracle.py`.

## Scope

This is per-test-**class** selection for a single Java suite. Finer-grained
per-method selection, and the other language providers (C++, …), build on the
same core (`docs/test-impact-selection.md`).
