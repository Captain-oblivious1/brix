# Design sketch: minimal correct test selection (test-impact analysis)

**Status: proposal / design sketch (2026-07-22, Opus). Nothing implemented.**
For Duane to react to before any code. It ends by drawing the explicit Opus/Fable
line so the handoff point is decided up front.

## The problem

Given a code change, run only the tests that change could affect — not the whole
suite. The motivating example: a jar with `A.class`, `B.class`, `C.class`, 100
tests each; editing `A.java` should run the tests that exercise `A` **plus** any
tests of code that transitively depends on `A`, and never the `B`/`C`-only tests
that cannot be affected. But the feature is **not** Java-shaped — the design is a
language-agnostic core with a thin per-language provider, and C++/TS/Rust plug in
the same way.

### What brix does today

For Java, `java.JUnitTestAction` already makes a *suite* incremental, but at
whole-suite granularity: all tests compile into one dir, run via
`--scan-class-path`, with the **entire** under-test classpath as a byte-lens
input — so any implementation change to any class re-runs the **whole** suite.
Correct, but coarse. Two facts make finer selection tractable, and both
generalize past Java: (1) test runners already support subsetting (JUnit
`--select-class`, CTest `-R`, `gtest_filter`, jest path patterns, cargo filters);
(2) brix's *discover-don't-declare* work already computes a code-dependency graph
for every language — the exact input this feature needs.

## The governing invariant (non-negotiable)

> **Running the selected subset must be provably equivalent to running the full
> suite.** Whenever that equivalence cannot be proven, fail open — run
> everything.

This is the same absolute guarantee brix already makes for incremental builds
matching clean builds, extended to tests: a user must be able to trust the
incremental test result exactly as much as a from-scratch run. The risk we are
eliminating is **under-selection** — skipping a test that would have caught a
regression — and a false "all clear" is categorically unacceptable, not merely
undesirable.

**Correctness is therefore not a knob.** There is no speed-vs-safety dial in the
default. The *only* variable is how *often* we can prove a safe subset — i.e.
how much speedup we get. Aggressive fail-open means more full runs and less
speedup, but it is never wrong. (A future *optional* speed/correctness dial is
possible as a controlled relaxation of the fail-open triggers — see "Future" — but
it is designed-for, not built, and never touches the default.)

This inherits `src/brix/affected.py`'s stance for package loading verbatim
(`return None` = "load everything" the moment a subset can't be proven safe):
an optimization under a safety net, never a correctness authority.

Equally load-bearing: **selection must not weaken caching.** A test skipped
*because it was provably unaffected* still counts as green for status/cache; but
a *narrowed* input footprint must never let a later unrelated change find a stale
"pass." That bookkeeping crosses the fingerprint path — the subtle,
build-breaking failure ordinary tests miss.

## The soundness model (the crux — uniform across languages)

Soundness is **not** a property of the language. It is a property of
**(binding time × selection granularity)**. The same rule governs every provider:

- **Coarse "binding-closure" granularity is safe-by-over-selection.** Select at
  the unit the language *statically binds*: C++ the **link closure** (which
  object files/libs a test binary links), Java/TS the **class/module closure**
  (what the compiled unit references). Indirect dispatch does **not** break this:
  a C++ function pointer or virtual call still requires the symbol to be *linked*,
  so its object file is in the closure; a Java virtual call still references the
  type. Over-select, never under-select.

- **Every language has exactly one runtime-dynamic-loading escape hatch** that
  *does* break even the coarse closure, because it binds by name at runtime with
  no static edge: C++ `dlopen`/`dlsym`, Java reflection / `Class.forName` /
  `ServiceLoader`, TS dynamic `import()`/`require`. Detected ⇒ **fail open**
  (that module runs its full suite). (Java's is worse than C++'s:
  `Class.forName("A")` escapes even *class*-closure, whereas C++'s link-closure
  holds for everything short of `dlopen`.) The win on such modules is recovered
  **only** by *sound dynamic coverage* (observed execution — see below), **never**
  by a human vouching a module is clean. A wrong promise silently skips a real
  test, which the equivalence invariant forbids outright.

- **Fine granularity (function/method-level) is unsound for all of them**
  statically — a fn-pointer/virtual call in C++ and a reflective/dynamic-dispatch
  call in Java are equally opaque. Method-level selection is **dynamic-coverage
  only**, identical in spirit across languages.

No language gets an "inherently safe" pass. The provider's job is to declare *its*
binding unit and *its* one dynamic-loading trigger; the core applies the same
rule to all.

## Architecture: generic core + per-language provider

The feature is pure graph work once two graphs exist, so the core is
language-free:

```
change set
  → code-dependency graph:  unit → units it depends on   (reverse it)
  → affected-code closure:  every unit transitively reaching a changed unit
  → test→code map:          which test units reach affected code
  → fail-open check:        provable superset? runtime-load trigger? else run all
  → runner selector:        emit "run only these"
  → run  (collapses to the full run under any fail-open trigger)
```

The **core** owns: the closure algebra, the **enumerable fail-open trigger set**
+ the policy that evaluates it (first-class, so a future optional dial is just
"which triggers are active" — see "Future"), the cache bookkeeping, and the
equivalence oracle. It is parameterized by a small **provider** interface —
roughly:

- `code_dependency_graph()` — the unit→unit graph (backed by what brix already
  discovers, below);
- `test_edges()` — test-unit → code-units it references;
- `binding_unit` — the coarse granularity this provider is sound at;
- `dynamic_load_trigger(module)` — detect the one runtime-loading escape hatch;
- `select(test_units) -> runner args` — the subsetter adapter.

Each seam is filled by machinery brix mostly **already has**:

| Language | Code-dep graph brix already computes | Runner subsetter | Binding unit | Dynamic-load escape |
|---|---|---|---|---|
| **C++** | `-MMD` `#include` depfiles + the **link graph** | CTest `-R`, `--gtest_filter`, Catch2 tags | object file / TU (link closure) | `dlopen`/`dlsym` |
| **Java** | `jvm-abi` bytecode constant-pool refs | JUnit `--select-class` | class (class closure) | reflection / `forName` / `ServiceLoader` |
| **TypeScript** | `tsc --listFiles` import graph | jest/vitest path patterns | module | dynamic `import()`/`require` |
| **Rust** | cargo owns within-crate; brix wraps a crate as one action | cargo test filters | crate | (cargo's problem below the crate) |

So test-impact selection is largely **re-pointing dependency graphs brix already
discovers** at "test→code," plus a per-runner subset adapter and a per-provider
fail-open trigger. This is the same shape as brix's existing language integration
(per-language module over a shared action model) — not a new pattern.

## Granularity, per provider

- **Coarse first, everywhere** (link/class/module closure) — the safe default.
  Java class-level, C++ TU/object-level, TS module-level, Rust crate-level.
- **Fine (function/method) later, dynamic-coverage-only** — JaCoCo (JVM),
  gcov/llvm-cov (C++), c8/istanbul (JS) all produce per-test coverage; the map
  differs, the concept is identical. Coverage is *observed execution*, so it is
  the only **sound** way to recover selection on dynamic-loading code. But it is
  **not** a free pass: a change can introduce a new dynamic path the last map
  never saw, so the map is itself stateful and must **fail open** on its own
  staleness triggers — new test, new/renamed code, a changed reflection surface.
  Same equivalence rule, one level down; coverage buys speedup *where provable*,
  full suite otherwise.

## The equivalence oracle (how we prove it)

The method that validated lazy loading (`tests/test_affected.py`): across an edit
alphabet — edit `A`, edit a transitive dependent, add a test, rename a unit,
introduce a dynamic-load call, delete code under test — assert the
**selected-subset run reaches the same pass/fail verdict as the full run.** If a
subset ever passes where the full run fails, the oracle has caught
under-selection. One oracle, run against every provider.

## The Opus / Fable line

**Opus (mechanism + scaffolding):**
- the **language-agnostic core**: closure algebra, provider interface, the
  persisted test→code closure index (sibling to the `affected` index), and the
  enumerable fail-open trigger set + policy (first-class, per the "Future" note);
- the **first two providers** — Java (`jvm-abi` refs + `--select-class`) and C++
  (`-MMD` + link graph + CTest/gtest filter) — since they're the Camp-1 languages
  where brix owns the graph;
- the equivalence-oracle harness and the edit alphabet.

**Fable (the correctness kernel + its review):**
- the **uniform soundness model** — that coarse binding-closure selection is a
  provable superset, and that every provider's runtime-dynamic-loading trigger
  and the shared fail-open triggers (new/renamed unit, prior failure, stale/​
  missing edges) actually fire;
- the **cache/fingerprint bookkeeping** — that a provably-skipped test stays
  green without ever letting a narrowed footprint serve a stale pass. This is the
  slice that crosses the fingerprint path, i.e. the reserved-review criterion.

Reordered from the first draft: the first Opus increment is *the generic core +
provider interface + oracle*, and Java and C++ arrive as the first two providers
— not "build a Java feature, then generalize."

## Decisions (2026-07-22, with Duane)

1. **Coarse-first; dynamic coverage as increment 2.** Coarse binding-closure is
   the safe, already-derivable default; dynamic coverage is the precision upgrade
   once the fail-open scaffolding exists. A global answer — the soundness model
   is uniform across providers.
2. **First two providers: Java + C++** — the Camp-1 pair brix owns the dependency
   graph for, and two structurally different binding units (class-closure vs
   link-closure) that stress the provider interface hardest.
3. **No user-vouching override.** A dynamic-load module fails open to its full
   suite, full stop; the only sound recovery is dynamic coverage (increment 2),
   never a human assertion. This follows directly from the equivalence invariant.
4. **Default posture: 100% equivalence, always.** Selection is opt-in during a
   soak period (while the oracle and the fail-open trigger set earn trust across
   real projects), then the default flips to on — consistent with incremental
   builds being the trusted default. Either way the *result* is equivalent to the
   full run; opt-in only governs whether the speedup is applied, never
   correctness.

## Future: an optional speed/correctness dial (designed-for, not built)

A speed/correctness dial, if ever wanted, is a **controlled relaxation of the
fail-open trigger set** — each notch disables specific named triggers (e.g.
"accept a stale coverage map without re-verifying"). It is a *subtraction* from
the safe default, reusing all the same machinery, so it is cheap to add later and
never moves the baseline for anyone who doesn't touch it.

To keep that option free, **build the fail-open triggers as a first-class,
enumerable policy set** — named rules a central selection policy evaluates — not
as `if uncertain: run_all` checks scattered through each provider. Day-one policy
is simply "all triggers on, no dial exposed"; that costs nothing now and makes a
future dial "which triggers are active for this mode" rather than a refactor. The
equivalence oracle can then be run per-policy to *measure* exactly what a given
dial position guarantees. **Caution:** exposing a dial reintroduces the
possibility of a missed test for anyone who turns it — so design for it, but do
not surface it without a deliberate, separate decision.
