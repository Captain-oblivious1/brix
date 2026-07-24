# tested_lib — running tests under brix

A minimal, self-contained example of how you declare and run tests in a brix
build, and what makes it different from Maven or Gradle.

## The idea in one sentence

**A test suite is a cacheable action.** brix runs a suite only when something
the suite actually reads has changed; otherwise it restores the previous
*passing* result from cache, exactly like it restores a compiled jar. Typing
`build` on an unchanged tree runs zero tests.

## What's here

```
src/com/acme/Adder.java        # unit under test
src/com/acme/Greeter.java      # a second, independent unit
test/com/acme/AdderTest.java   # exercises Adder only
test/com/acme/GreeterTest.java # exercises Greeter only
brix_build.py                  # two jars, two suites
```

Two library jars (`adder.jar`, `greeter.jar`) and one test suite wired to each.
The suites are independent on purpose — that independence is what the demo shows.

## How you declare a suite

The entire authoring surface is one action:

```python
java.JUnitTestAction(
    name="adder tests",
    test_sources=d.file("test/com/acme/AdderTest.java"),  # the .java tests
    classpath=[d.file("build/libs/adder.jar")],           # the code under test
    console_jar=CONSOLE,                                  # the JUnit runner
    javac_worker=worker,
    options=["--release", "17", "-encoding", "UTF-8"],
)
```

You do not tell brix *when* to run it. brix works that out from the inputs.

## Run it

```
python brix_build.py build
```

Requires a `junit-platform-console-standalone` jar (the runner). Set
`BRIX_JUNIT_CONSOLE_JAR=/path/to/...jar`, or drop one under
`~/Projects/brix_test/*/lib/`.

### First build — everything runs

```
run: run adder tests
run: run greeter tests
BUILD OK (10 done)
```

### Build again with no changes — nothing runs

```
ok: run adder tests (replayed)
ok: run greeter tests (replayed)
BUILD OK (10 done)
```

Both suites are `(replayed)` — restored from cache. This is the part Maven and
Gradle don't do: an unchanged suite is not re-run.

### Edit `Adder`'s implementation — only its suite re-runs

Change the body of `Adder.add` (e.g. `return a + b + 0;`) and build:

```
run: javac(worker) -> build/classes/adder      # Adder recompiled
ok:  compile adder tests (replayed)            # tests NOT recompiled
run: run adder tests                           # Adder suite RE-RUN
ok:  run greeter tests (replayed)              # Greeter suite untouched
```

Three things happened, and each is deliberate:

| Observation | Why |
|---|---|
| The Adder suite **re-ran** | The test *run* depends on `adder.jar` through its **bytes**. Behavior may have changed, so the suite must run again. |
| The Adder tests were **not recompiled** | The test *compile* depends on Adder through its **ABI**. `+ 0` changed no signature, so recompiling would be wasted work. |
| The Greeter suite **stayed cached** | Its classpath is `greeter.jar`, which nothing touched. There is no input path from an Adder edit to the Greeter suite, so it cannot be stale. |

That two-lens split — **ABI for compile, bytes for run** — is what lets brix
skip the compile without ever skipping a run that might matter.

## What this example does *not* yet show

This is suite-level caching: the unit brix skips is a whole suite. Editing a jar
that a suite depends on re-runs **all** of that suite's test classes, even ones
that don't touch the changed code.

Finer-grained **per-test-class selection** — "edit one class, re-run only the
test classes that could observe it" — is being built (see
`docs/test-impact-selection.md`). When it lands, the same `JUnitTestAction`
authoring surface stays; only the granularity of what gets skipped improves.
