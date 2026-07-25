# Java test selection by runtime discovery (retired `plan_suite`)

**Status: IMPLEMENTED (2026-07-25).** `JUnitTestAction` selects per test class by
runtime discovery; the static `plan_suite`/provider subsystem was deleted. Proven
by `tests/test_testimpact_java_discovery_oracle.py` and the migrated
`examples/tested_lib` + `examples/jackson/databind_testimpact.py`. **`JvmFactory`
was designed here but NOT built** — measured evidence (cold ~58 s / incremental
~7 s on databind under the 24-wide parallel scheduler, and no JIT reuse for
single-use JVMs) showed a pre-warm pool would be speculative complexity. The
sections below are kept as the design record; the `JvmFactory` section describes a
deliberately-unbuilt option. Companion to
[test-impact-selection.md](test-impact-selection.md); does not change the
governing invariant (a selected run must reach the same verdict as the full run;
fail open when unproven).

## The idea, in one sentence

Run each test class as its own action that **records which `.class` files the JVM
actually loaded**, and let brix's existing depfile machinery re-run it only when
one of those files changes — exactly how `cpp.CompileSourceAction` uses `-MMD` to
re-compile only when a header it actually read changes.

## Why this replaces the shipped `plan_suite`

`plan_suite` selects at build-file *evaluation* time: it statically analyzes
compiled bytecode to predict each test's class closure, then hands JUnit one
`--select-class` list run as a single suite action. It works, but it made Java a
special case with a train of complications no other language needs — the
selected-class list baked into the run action's identity, a global prior-failure
signal, a command-oscillation patch, a persisted per-suite index, `known`
vocabulary bookkeeping. All of that exists to *predict, without running,* what a
test depends on.

C++ and TS don't predict — they **discover**. A `cpp` compile writes a depfile of
the headers it read; a `ts` compile records the files `tsc` loaded. brix folds
those into the fingerprint, so the action re-runs only when something it truly
used changed. This proposal makes Java tests work the same way: *run once, record
what was loaded, re-run when a loaded thing changes.*

## The design

**`JUnitTestAction.lower()` returns an `ExpandAll` over the test glob** — the same
fan-out `cpp.CompileSourceAction` uses over its source glob — so the engine
resolves the glob **at realization time** and instantiates one child run action
per test class, binding `{name}` to the class (`test/{name}.java` →
`com/acme/AdderTest` → `--select-class com.acme.AdderTest`). Consequences:

- **No build-file helper, no eval-time root problem.** Enumeration happens in the
  engine, which has the workspace root — a plain constructor, like every other
  brix action.
- **Generated tests just work.** A test class produced by an earlier build step
  exists by the time the glob is realized, so it is discovered and run — a
  static eval-time enumerator would miss it entirely.

**Each child run action discovers its own dependencies at runtime.** The child
runs the test class under the JUnit console launcher with the JVM recording every
class it loads (`-verbose:class`), wrapped by a small recorder that writes the
**loaded `.class` file paths as a depfile** — the same channel `-MMD` headers and
`tsc --listFiles` outputs already flow through. brix folds them into the action's
fingerprint (`depfile=`, `set_discovered_deps`), so on the next build the test
**replays** if none of the classes it loaded changed, and **re-runs** if any did.
Per-class selection and per-class failure-retry, entirely from machinery that
already exists — no new engine concept, no boundary change.

## What discovery handles for free (that static analysis had to special-case)

- **Reflection.** A reflectively loaded class *is* a loaded class — it lands in
  the depfile like any other. No "dynamic" escape, no fail-open-to-full-run,
  none of `plan_suite`'s reflection machinery. The runtime tells the truth.
- **Exploded classes vs jars.** `-verbose:class` reports where each class loaded
  *from*: an exploded `.class` file (fingerprint it → per-class selection on your
  own code) or a jar (whole-jar byte-lens → coarse but sound for third-party
  libs). The right granularity falls out of the load path with no eval-time
  decision — and no need to explode library jars (the graph stays small; a
  future jar-member-hash refinement remains available if lib-upgrade re-runs ever
  bite).
- **Cold builds / brand-new tests.** No cached result → the action runs (correct)
  and produces its first depfile; subsequent builds select against it. Identical
  to a C++ source with no depfile yet.
- **Soundness of a discovered (not predicted) set.** Same basis as C++ headers: a
  change that would make a test load a *new* class arrives via a change to
  something the test *already* loaded (the edit that adds the reference is itself
  in the loaded set), which re-stales the test, which re-runs and re-discovers.
  A loaded class that changed → re-run; a class never loaded cannot affect the
  result. The residual (non-deterministic class loading) is the test-independence
  boundary below, not a hole unique to this model.

## Execution: single-use JVMs from a `JvmFactory`

Each test class runs in its **own fresh, single-use JVM** — never reused — so
isolation is airtight by construction (no cross-test or cross-build state leak).
Where the JVMs come from is a Java-side performance knob, mirroring `javac_worker`:

```python
# Default: nothing to allocate — each test gets a fresh JVM made on the fly.
graph.add(java.JUnitTestAction(test_sources=data.glob("test/{name}.java"),
                               classpath=[...], console_jar=CONSOLE,
                               javac_worker=worker))

# Opt-in speed: keep N fresh JVMs pre-warmed and idling, so a test never waits
# for boot (they boot during compile — a JVM can load the launcher and JIT-warm
# before the test classes exist, then receive classpath+class per request).
jvms = java.JvmFactory(graph, warm=4)
graph.add(java.JUnitTestAction(..., jvm_factory=jvms))
```

- `warm=n` is a **pure performance knob** — identical verdicts at any `n` (like a
  parallelism count); never touches fingerprints or identities.
- Lives in `brix.java` alongside `javac_worker`; takes `graph` (bootstraps a
  single-shot `TestRunnerWorker.java`: boot, wait for one job, run, exit). Nothing
  touches the generic `Action`/`Graph`/`Data`.
- **Honest cost:** always-per-class means N boots on a full/cold run of a big
  suite (databind ~668 vs one JVM today); pre-warming hides *latency*, not
  aggregate CPU. Escape hatch if it bites: a future `JvmFactory` mode running the
  full-run set in one JVM (correct — the full run *is* the equivalence baseline).
  Noted, not built now.

## The real boundary: test independence

The hard case is not the JVM model or discovery — it is tests that share mutable
state so B's result depends on whether/when A ran. **No selection can equal the
full run when tests are order-dependent** (`plan_suite` had this limit too; all
test-impact assumes independence). Single-use per-class JVMs make this the *safe*
direction: a hidden dependency makes B, run alone, **fail loudly** (missing setup)
rather than silently diverge — and our invariant forbids only the false *pass*
(shipping a regression), never a false *failure* (which just surfaces a bad test).
Policy: default isolate → fail loud; persistent shared state (DB/file) declared as
an ordinary resource edge; a legitimate in-memory ordered cluster is an opt-in
declared co-run in one JVM (brix cannot discover a hidden shared-state edge).

## What gets deleted

Discovery makes the static prediction machinery unnecessary:

- **`plan_suite`** and its satellites: the per-suite index, `SCAN_ALL`/
  `--scan-class-path` selection, `changed_since_last_build`, the `prior_failed`
  parameter, the `has_failed_actions()` use, `select=` on `JUnitTestAction`.
- **Most of the static provider** (`testimpact_java`): the class-closure walk,
  reflection detection, name-addressed/uncovered escapes, `test_input_sets`.
  Runtime discovery replaces all of it. (The bytecode scanner `jvmabi.class_refs`
  stays — it powers the `jvm-abi` compile lens, a separate feature.)

This is a net *removal* of code and concepts, not an addition.

## What does NOT change

- **The authoring API** — `java.JUnitTestAction(...)` stays a constructor, minus
  the now-unnecessary `select=`; build files get *simpler* (no `_select()` /
  `plan_suite` boilerplate). `JvmFactory` is the only addition, a pure perf knob.
- **The governing invariant**, and the equivalence-oracle discipline (a real
  JUnit run, teeth-checked).
- **C++ and TS**, already in this discover-don't-predict shape.

## Risks / open questions (to settle before coding)

1. **`-verbose:class` parsing.** Format is JVM-specific but stable and
   documented; the recorder filters JDK/platform and console-jar classes (like
   the existing platform-prefix filter) and maps each loaded class to its load
   path (`.class` file or jar). Confirm on the target JDK.
2. **Selection ≡ full run under per-class isolation.** Prove a selected subset,
   each class in its own single-use JVM, reaches the same verdict as the full
   run, on a shared-state suite (jackson-databind is a good adversary); confirm
   isolation-induced divergences are *failures*, never false passes.
3. **Discovery completeness.** Verify `-verbose:class` captures reflectively and
   `ServiceLoader`-loaded classes on a real suite (it should — they are still
   class loads); note any loader that bypasses it.
4. **Full-run boot cost.** Measure N cold boots on databind to decide whether the
   one-JVM full-run `JvmFactory` mode is worth building early.
5. **Migration.** `examples/tested_lib` and the jackson dogfoods move to the new
   shape; the equivalence + cache-equivalence oracles re-point at per-class
   discovery-driven actions.

## Rollout

1. A discovery recorder: wrap the console-launcher run to capture `-verbose:class`
   and emit a depfile of loaded `.class`/jar paths (sibling of the C++ `-MMD`
   flow and the TS `--listFiles` wrapper).
2. `JUnitTestAction.lower()` → `ExpandAll` over the test glob; each child is one
   discovery-wrapped test-class run with `depfile=` set; drop `select=`.
3. `java.JvmFactory(graph, warm=n)` (pre-warmed single-use pool) + the default
   on-the-fly factory; `JUnitTestAction(jvm_factory=…)`; single-shot
   `TestRunnerWorker.java` over the existing worker transport.
4. Re-point the equivalence + cache-equivalence oracles; add the isolation oracle
   (selected subset == full run, divergences are failures) on a shared-state
   suite.
5. Delete `plan_suite` and the static-prediction bulk of `testimpact_java`;
   migrate examples; update `test-impact-selection.md` (Java row → "runtime
   discovery, like C++/TS").

Net: minus a Java-only prediction subsystem, plus a `-verbose:class` recorder and
a `JvmFactory`. Java stops predicting and starts discovering — three languages,
one model.
