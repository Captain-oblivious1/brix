# tested_cpp — running C++ tests under brix

The C++ counterpart of `tested_lib`. Two independent units (`adder`, `greeter`)
and a gtest binary for each. It shows the two things brix does that a plain
`make`/`ctest` loop does not:

1. **A test run is a cacheable action.** An unchanged tree re-runs zero tests.
2. **Impact selection — for free.** Editing one unit re-runs only the test
   binaries that link it. No selection index, no analysis pass: the incremental
   engine does it.

## Why selection is free in C++

A test binary is itself a build product. brix already tracks the whole chain:

```
source/header --(-MMD depfile)--> object --(link graph)--> test binary --> RunTestAction
```

Each object's `-MMD` depfile makes it stale when a header it includes changes;
the link graph makes a binary stale when an object it links changes. A
`cpp.RunTestAction` replays on the binary's fingerprint — so it re-runs a test
**exactly when code that binary statically links changes, and never otherwise.**
One `RunTestAction` per binary is therefore already impact selection: the run
unit (a binary) *is* the selection unit, so the engine selects.

That is the whole difference from Java: there, one JUnit process runs many
classes, so a separate eval-time selector (`plan_suite`) narrows the class set;
here, each binary is its own process, so the engine's per-action staleness is the
selection with nothing added.

## How you declare it

```python
cpp.LinkApplicationAction(
    object_files=d.file("build/obj/src/adder.o") + d.file("build/obj/test/adder_test.o"),
    output_file=d.file("build/test/adder_test"),
    link_options=["-pthread"],
    deps=cpp.pkg_config(["gtest_main"]),   # -lgtest_main -lgtest, after the objects
)
cpp.RunTestAction(test_binary="build/test/adder_test")
```

## Run it

```
python brix_build.py build
```

Needs `g++` and the gtest libraries (`gtest_main` discoverable by `pkg-config`).

```
# 1. cold build — both binaries build and run
run: run build/test/adder_test
run: run build/test/greeter_test

# 2. build again, no changes — both replay, nothing runs
ok: run build/test/adder_test (replayed)
ok: run build/test/greeter_test (replayed)

# 3. edit src/adder.cpp — only adder_test re-runs
run: compile adder
run: link build/test/adder_test
run: run build/test/adder_test
ok:  run build/test/greeter_test (replayed)   # provably unaffected — skipped
```

(Use a change that actually alters the object — the compiler folds a no-op like
`+ 0` away, and brix correctly sees an unchanged object and re-runs nothing.)

## The one thing the link graph cannot see

Code loaded at **run time** — a `dlopen`'d plugin `.so`, a data file read by
path — is not in the link graph, so editing it does not re-stale the binary.
Declare those as `runtime_inputs=` on the `RunTestAction` so a change re-runs the
test:

```python
cpp.RunTestAction(test_binary="build/test/plug_test",
                  runtime_inputs=["build/plugins/libx.so"])
```

`brix.testimpact_cpp` detects the `dlopen` case from an object's symbols
(`nm -u`); a test that loads code by a name it computes at run time, and cannot
enumerate what it might load, belongs in a suite that always runs. This is the
C++ analogue of the reflection boundary the Java provider draws.

## Scope

Per-**binary** selection. Per-`TEST()`-case selection (via `--gtest_filter`) is
sound only with runtime coverage (gcov) — a later increment — because every case
in a binary links the same objects. See `docs/test-impact-selection.md`.
