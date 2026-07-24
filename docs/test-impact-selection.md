# Design: minimal correct test selection (test-impact analysis)

**Status (2026-07-23): core + Java provider implemented; handed to Fable.**

| Increment | State | Where |
|---|---|---|
| 1a — language-agnostic core + equivalence oracle | **done** (Opus) | `src/brix/testimpact.py`, `tests/test_testimpact.py` |
| 1b — Java provider, proven against a real JUnit run | **done** (Opus) | `src/brix/testimpact_java.py`, `jvmabi.class_refs`, `tests/test_testimpact_java*.py` |
| Escape-completeness audit (Java) | **next — Fable** | "The Fable slice" below |
| Fingerprint / cache wiring into `JUnitTestAction` | Fable, *after* the audit | "The Fable slice" below |
| 1c — C++ provider | Opus, *after* the audit | deliberately not started, see below |
| 2 — dynamic per-test coverage | later | "Granularity, per provider" |

Nothing here is selected in a real build yet: the provider computes a selection,
but no action consumes it. That wiring is the fingerprint-crossing slice and is
Fable's, by the reserved-review rule.

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

## Direction: what separates a real escape from a phantom one

*(Duane, 2026-07-23 — this supersedes an earlier framing that asked whether the
test runner should be granted an exemption from the fail-open rule. It should
not, and it does not need one.)*

The runner reflects **once**, at the entry, to load a class **whose name brix
itself wrote on the command line**. It is the test's *caller*, sitting above it.
Everything the test does from there — instantiating collaborators, calling
through interfaces — is ordinary static binding, already covered by the closure.
So the launcher's internals were never part of what a test depends on.

The first implementation nonetheless marked every test dynamic, because the
reference walk wandered *upstream*: a test legitimately calls `Assertions`,
`Assertions` shares a utility class with the launcher, and the launcher contains
`Class.forName`. That is a path through **reference edges that are not call
edges** — unreachable at run time. Excluding the harness is therefore a **bug
fix, not a carve-out**, and decision 3 (no user-vouching override) stands
untouched.

Two consequences, both load-bearing:

- **The harness designation is a role, not a reputation.** It attaches to the
  *artifact* brix launched the suite with (`JavaProvider.harness_roots`, fed from
  `JUnitTestAction.console_jar`) — never to a package name, which anyone can
  claim, and never to a list of libraries brix recognizes. Withhold the artifact
  and the identical bytes are ordinary code whose reflection makes every test
  always-run: safe, useless, and the correct direction to fail.
- **Look below the test, not above it.** Reflection *at or under* the test class
  is a genuine escape: a component scan the test triggers, a service-loaded
  extension, or an annotation naming code by string. That reading is what
  surfaced the `@MethodSource` hole (below) — and it is the lens the
  completeness audit should use.

## Identity is content, not labels

*(Duane, 2026-07-23.)* An earlier idea — treat a versioned external package as
unchanged until its version pin moves — was withdrawn on the right grounds: the
same coordinates can be re-pushed, so the version is a label somebody types while
the hash is what the artifact *is*. brix already hashes every input, which
catches it regardless.

Two things follow. Versioning is not load-bearing for correctness (it never was —
an external package's reflection reaching *your* changed code is a separate
question that pinning does not touch). And the per-artifact analysis cache — so a
50-service monorepo parses a shared jar's constant pool once, not once per
service — is keyed by **content hash**, which needs no invalidation logic at all
(same bytes ⇒ same reference graph), dedupes across differing version labels, and
works for artifacts with no version at all.

## Known escapes, and the one that was missed

The Java provider currently handles four ways a test's verdict can depend on code
its class closure does not cover:

| Escape | Handling |
|---|---|
| Reflection / `ClassLoader` / `ServiceLoader` / JNI reachable from the test | referencing class marked dynamic ⇒ its tests always run |
| A reference resolving to nothing scanned (and not JDK) | same — an un-supplied library might reflect back into the code under test |
| `@MethodSource` / `@FieldSource` naming code by string | resolved against the scanned universe: a real edge when it resolves, dynamic when it does not |
| Test class not statically discoverable (`@Nested`, inherited tests) | grouped to the top-level unit; "carries tests" closed down the subtype graph |

**The third was found by argument, not by method** — it was noticed while
reasoning about direction, not produced by any systematic enumeration. Left
unhandled it produced a genuine false pass, reproduced against real JUnit: edit
a data provider no test references, the full run goes red, selection runs
nothing. That is precisely the failure the governing invariant forbids.

The honest conclusion: **the escape list is not proven complete.** That is now
the top open risk of the feature, and it is why the audit precedes the wiring.

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

## The Fable slice (handed off 2026-07-23)

**Ordering changed from the original plan, deliberately: the audit comes first.**
The `@MethodSource` hole showed the graph can be missing edges we have no method
for finding, and there is no value in making the bookkeeping airtight around an
incomplete graph. C++ is also held until the audit lands, so its provider
inherits whatever the audit concludes rather than repeating the same mistakes in
a second language — that is Duane's explicit reason for handing off at this
point rather than after C++.

### Slice 1 — escape-completeness audit (Java)

**The question:** enumerate every way a JUnit test's pass/fail verdict can depend
on code outside its static class closure, and argue the enumeration is *closed* —
not "here are some more cases," but a structural argument for why there are no
others. Use the direction lens above: the runner above the test is not a source;
anything at or below it is.

Candidates to confirm or dismiss, none of them yet verified — this list is a
starting point, **not** a scope boundary:

- service-loaded JUnit extensions (`junit.jupiter.extensions.autodetection.enabled`,
  `META-INF/services`) — registered without the test naming anything;
- Spring `@SpringBootTest` / `@ContextConfiguration(locations=…)` — beans named
  by string in XML or discovered by component scan;
- `Class.forName` in a **static initializer** of something in the closure;
- classpath *resources* that name code (properties files, service files) —
  note `JUnitTestAction` already declares `test_resource_dirs` as byte-lens
  inputs, so establish whether that stays sufficient once a selection narrows
  the footprint;
- `-javaagent` / instrumentation, and classes generated at run time;
- annotations taking a `Class` literal (`@ExtendWith`, `@ArgumentsSource`) —
  believed safe because the literal is a `CONSTANT_Class`; confirm.

Each confirmed escape must land as either a real edge or a fail-open, with a test
in `tests/test_testimpact_java.py` and — where it can turn a real suite red — a
case in `tests/test_testimpact_java_oracle.py`.

**Verify the existing rules actually fire**, rather than trusting them: the
reflective-entry-point list in `jvmabi._REFLECTIVE_METHODS`, the deliberate
exclusion of `LambdaMetafactory`/`StringConcatFactory` (marking those dynamic
would disable selection everywhere — check the exclusion is sound, not just
convenient), the `PLATFORM_PREFIXES` guess (the non-guessy version is to ask the
JDK what it ships and key that to the `java` tool fingerprint), and the
`_STRING_CODE_REF` shape match.

### Slice 2 — fingerprint / cache bookkeeping (the reserved-review kernel)

Only after slice 1. Wire selection into `java.JUnitTestAction` and the persisted
index. The properties that must hold:

- a test **provably skipped** counts as green for status and cache — it is not
  "unknown," it is proven unaffected;
- a **narrowed input footprint must never serve a stale pass.** The hazard is
  concrete: today the run action takes the whole under-test classpath as a byte
  lens and declares no outputs, so it replays purely on its input fingerprint. If
  selection narrows that footprint, a test skipped today because unrelated code
  changed must not, tomorrow, find a cached pass keyed on a fingerprint that
  omitted the code it *does* depend on;
- the persisted test→code closure index (sibling to the `affected` index) needs a
  version, and staleness must fail open — `SelectionInput.known` exists for
  exactly this and is currently supplied by callers, not persisted;
- `prior_failed` must be plumbed from real recorded failures (`BuildMetadata.
  has_failed_actions()` is the analogue `affected.py` uses).

### What is deliberately NOT in scope

Dynamic per-test coverage (increment 2), the optional speed/correctness dial
(designed-for, not built), and any relaxation of decision 3. If the audit
concludes some escape can only be recovered by coverage, record that and fail
open — do not introduce an annotation or setting by which a user asserts their
code is clean.

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
