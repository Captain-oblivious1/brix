# Design: minimal correct test selection (test-impact analysis)

**Status (2026-07-25): Java, C++, and TS all select tests in real builds — all by
runtime discovery.** Java was rebuilt from static prediction to discovery
(2026-07-25), retiring the `plan_suite`/provider subsystem — see
[test-impact-java-per-class-proposal.md](test-impact-java-per-class-proposal.md),
now the authoritative design for Java. The sections below describing the static
Java provider (`JavaProvider`, `plan_suite`, the escape audit) are **historical**:
they document the approach that discovery replaced, kept for the reasoning.

| Increment | State | Where |
|---|---|---|
| 1a — language-agnostic core (`ProviderView`, closures) | **done** | `src/brix/testimpact.py` (C++/TS use `ProviderView`/`_reachable`) |
| Java selection by runtime discovery | **done** | `JUnitTestAction` (`ExpandAll` + `-Xlog:class+load` depfile), `_java/junit_classload.py`, `examples/tested_lib`, `tests/test_testimpact_java_discovery_oracle.py` |
| 1c — C++ provider + selection in a real build | **done** | `src/brix/testimpact_cpp.py`, `cpp.RunTestAction`, `examples/tested_cpp` |
| 1d — TS provider + selection in a real build | **done** | `src/brix/testimpact_ts.py`, `ts.RunTestAction`, `TsProvider.test_input_sets`, `examples/tested_ts` |
| 2 — sub-class (per-method) coverage | largely obviated | discovery already selects finely (databind ~1.5%); see roadmap |

All three targets select by the same principle — **discover what a test actually
used, re-run only when that changes** — never static prediction: C++ per binary
(`-MMD` + link graph), TS per file (import closure), Java per class
(`-Xlog:class+load`). Proven on jackson-databind, where static class-level
selection skipped ~0% (reflective `ObjectMapper` reaches everything) yet discovery
selects ~1.5%. No CMake.

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

## The C++ provider

`src/brix/testimpact_cpp.py`, proven against real gtest binaries in
`tests/test_testimpact_cpp_oracle.py`. It is the same soundness model as Java at
C++'s binding unit, and — as predicted before it was built — C++ turned out to be
the *easier* language: static linking and `#include` graphs, none of Java's
reflect-by-string dynamism as the normal case.

- **Binding unit for a test: the executable, not the case.** A gtest binary
  links a fixed set of objects, and every `TEST()` in it links the same objects,
  so nothing static can attribute one case to a subset of them. The executable is
  the finest *sound* static granularity; per-case selection needs coverage (gcov,
  increment 2). Overshooting to the whole binary is the safe direction.
- **The graph is re-pointed, not recomputed.** Each object's `-MMD` depfile
  (`<obj>.d`, which brix already emits and the incremental layer already scans)
  gives `object → {source, user headers}`; the link graph gives `test binary →
  {objects}`. A header edit re-stales exactly the objects that include it, so it
  selects exactly the binaries that transitively compile it in — and nothing
  else. Verified: editing an inline in `util.h` selects `a_test` (which includes
  it via the depfile) and never `b_test`.
- **`dlopen` is the reflection analogue.** An object whose symbols reference
  `dlopen`/`dlsym`/`dlmopen` (read straight off the object with `nm -u`, the way
  the Java provider reads bytecode) binds code by name at run time, so it is
  marked *dynamic* and every test linking it always runs. This subsumes the
  config-named-plugin case for v1: the loader is dynamic, so its tests never rely
  on the link graph seeing the plugin.
- **One thing the Java shortcut hid.** In Java the changed units *are* the graph
  keys (classes); in C++ the changed units are **source and header files**, which
  are edge *targets* (a compile's prerequisites), not keys. So `known` (the
  new-or-renamed baseline) must be the full vocabulary — keys **and** targets —
  or every source edit reads as new code and forces a full run. The wiring must
  carry this.

**Wired — and simpler than Java.** A test binary is a build product, so brix's
existing `-MMD` + link tracking already makes it stale exactly when code it
statically links changes. `cpp.RunTestAction` runs a binary as a tracked,
exit-code-only action that replays on the binary's fingerprint; one per binary is
therefore already impact selection — **the incremental engine does the selecting**,
because the run unit (a binary) *is* the selection unit. No `plan_suite`, no
index, no eval-time analysis, no CMake. Proven in `examples/tested_cpp` and
`tests/test_cpp.py`: editing one unit re-runs only the binaries that link it. The
`select_args` `ctest -R` filter remains for users who drive CTest; the direct
per-binary path is the default. The one gap — code loaded at run time (`dlopen`),
invisible to the link graph — is declared via `RunTestAction(runtime_inputs=…)`,
and `references_dl` (an `nm -u` scan) is the detector for it.

### Windows / toolchain portability (scope)

The C++ selection is engine-native, so the only OS-specific surface is *running*
the binary and *detecting* runtime loading. Both are now correct **by
construction** on Windows, though unverified there (no Windows CI):

- **Running the binary.** `RunTestAction` execs argv directly (no shell), so a
  path with a directory separator — every build output — runs as a path on both
  POSIX and Windows with no `./` prefix; the prefix is added only for a bare name
  in the cwd, and never on Windows. The executable path must carry its real
  extension (`.exe`) — brix does not synthesize it.
- **Detecting runtime loading.** `references_dl`'s symbol set covers both the
  POSIX (`dlopen`/`dlsym`/…) and Windows (`LoadLibrary*`/`GetProcAddress`/
  `LoadPackagedLibrary`) entry points, matched as substrings so import thunks and
  A/W variants are caught; it fails **safe** when `nm` is absent or odd (MinGW),
  treating the object as dynamic. Note this detector is a helper, not wired into a
  build — runtime-loaded code is declared via `runtime_inputs=` regardless.

**Out of scope, and the real "C++ on Windows" blocker:** header tracking. brix
discovers `#include`s from `-MMD` depfiles, which GCC and Clang (incl. clang-cl)
emit; **MSVC `cl.exe` uses `/showIncludes`** with a different format, unsupported
here. A project on GCC/Clang/MinGW is covered; one on MSVC needs a `/showIncludes`
adapter first — a separate, compile-side item.

## The TypeScript provider

`src/brix/testimpact_ts.py`, proven against real vitest in
`tests/test_testimpact_ts_oracle.py`. Binding unit: the **module (file)**, like
C++ — and like C++ the graph is file-level, but it comes straight from the static
`import` graph rather than a compiler depfile.

- **`code_deps` / `test_edges` from parsed imports.** `import … from './x'`,
  side-effect `import './x'`, and `export … from './x'` are static edges; a
  relative specifier is resolved to a real workspace file (with the usual
  extension / `index` fallbacks), a bare specifier (`react`, `vitest`) is an
  external package and inert — the same treatment the Java provider gives JDK
  platform classes. A test file's closure is its imports plus everything they
  transitively import; editing `util.ts` selects the tests whose closure reaches
  it and no others.
- **`import(<expr>)` / `require(<expr>)` is the reflection analogue.** A dynamic
  import on a *computed* specifier loads a module by a run-time name, so the file
  is marked dynamic and every test reaching it always runs. A dynamic import on a
  *literal* (`import('./plugin')`) is statically resolvable and stays an ordinary
  edge.
- **Fail-open on what it can't follow.** A relative import that resolves to no
  known workspace file (a tsconfig `paths` alias, an extension not modeled)
  becomes `uncovered` → full run. It never guesses an edge away.

**Wired, C++-style.** `ts.RunTestAction` runs `vitest run <file>` per test file
as a tracked, exit-code-only action whose byte-lens inputs are the file's
transitive import closure — so the incremental engine re-runs only the test files
a change reaches, no `plan_suite`. The one TS-specific piece: a test file is not a
build product whose staleness the engine already tracks (as a C++ binary is), so
each action is handed its closure explicitly via `TsProvider.test_input_sets()`,
which widens to the whole source set for a test reaching a computed
`import()`/`require()` or an unresolved import (fail-open). Proven in
`examples/tested_ts`: edit `util.ts`, only `adder`'s test re-runs.

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
| **A unit named as *text* in a classpath resource** (`META-INF/services`, `spring.factories`, an XML bean definition) | `ProviderView.name_addressed` + the `config-named-unit-changed` fail-open — see below |

### What the audit method found

Working down from route 2 exposed a gap neither existing rule covers. Both of
them key on the **reader**: mark the class that *performs* the loading, then run
the tests that reach it. That fails whenever the reader is not in any closure —
a platform factory (`DriverManager`, an XML factory, `Charset`) consulting
`META-INF/services`, or, worse, **the harness itself**, which direction
deliberately excludes. A JUnit extension registered only in a service file is
loaded by the launcher, changes what every test observes, and is named in no
bytecode anywhere. Both rules miss it, by construction.

Enumerating every such loader is unbounded — it is every JDK and framework entry
point that consults configuration, and the list grows with every library. So
**invert it and key on the target instead**, which is bounded and mechanical:
harvest dotted names out of the non-class files on the classpath, keep those
that match a scanned unit, and fail open when the change set touches that set's
closure. The closure matters — once a class can be loaded from anywhere, so can
everything it depends on.

The economics are the point. `dynamic_units` is a property of the *reader* and
costs the tests reaching it on **every** build; `name_addressed` is a property of
the *target* and costs **nothing until it changes**. A plugin registry that sits
still is free.

Proven in `tests/test_testimpact_java_oracle.py` against a real JUnit run: a
service-registered `BeforeEachCallback` that no test references. With the rule
disabled the oracle reports `UNDER-SELECTION editing com/acme/Stamp: the full
run fails ['com/acme/StampTest'] but selection ran []`.

### Obligation 1, restated — and why the call-graph version was a trap

The first framing was "enumerate every JDK entry point that can load a class,"
to be derived by reachability to `ClassLoader.defineClass` inside `java.base`.
**That is unbuildable and would be useless if built.** Soundness demands
over-approximating virtual dispatch — any call to an overridable method could
land on any override — and under that assumption nearly everything in the JDK
transitively reaches a class loader. The derived answer would be "almost every
JDK method can load a class," every test would go dynamic, and selection would
die. Precisely the failure shape as the JUnit launcher: an analysis so cautious
it conveys nothing.

The obligation was **stated wrong**. The question is not whether a JDK method
can load *a* class; it is whether it can load **one of our units** — and for
that, the JDK must first *obtain the name*. Enumerating name **sources** is
bounded where enumerating loaders never was:

| Where a name can come from | Handling |
|---|---|
| Our own code — a `Class` literal or a string in our bytecode | the closure itself (a `Class` literal is a `CONSTANT_Class`); a class computing names reflectively is already dynamic |
| Configuration shipped with the code — service files, manifests, XML | `name_addressed`, via the classpath resource scan |
| Test **data** — a fixture file naming a class | `name_addressed`, via `resource_roots` |
| **Runtime input** — a JVM system property, an environment value, a command-line argument | `name_addressed`, via `runtime_values` |

All four are now handled, and all four are proven against a real JUnit run.
The property case in particular is not hypothetical:
`-Djavax.xml.parsers.DocumentBuilderFactory=com.acme.MyFactory` causes the
platform's factory finder to instantiate one of our classes with **no file and
no bytecode naming it anywhere**; with `runtime_values` unscanned the oracle
reports `UNDER-SELECTION editing com/acme/MyFactory: the full run fails
['com/acme/XmlTest'] but selection ran []`.

### The residual, stated plainly

**A name assembled at run time out of data brix never sees** — read from a
database, fetched over the network, computed from the clock — is not detectable
by any static means. No amount of scanning closes it, and it is recorded as a
known limit rather than papered over. The mitigation that *does* exist is
already in place: the class performing such a load is calling a reflective API,
so it is marked dynamic and every test reaching it runs unconditionally. The
residual is therefore narrow — it requires a loader that is reflective (so,
dynamic, so covered) yet reached by no test, which for a class that actually
affects a test's outcome is close to a contradiction. Increment 2's dynamic
coverage is the only thing that would close it fully.

**Two invocation-dependent findings**, both worth knowing before anything
changes how brix launches tests: `ToolProvider.findFirst` and
`java.util.logging`'s `config.class` both resolve through the **system** class
loader, which brix's `java -jar console.jar --class-path …` invocation leaves
empty of application classes — so that whole family of name-based loading cannot
reach user code today. Consumers going through the *context* class loader (the
XML factory finder above) do reach it. Switching to `java -cp … ConsoleLauncher`
would widen the exposure; the `runtime_values` scan is correct either way, which
is why it is not conditioned on the invocation.

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

## The remaining slices

*(Originally reserved for Fable. Fable is no longer available (2026-07-23), so
this is Opus work — but the **ordering stands on its own merits** and is not a
consequence of who is doing it: sealing cache bookkeeping around a dependency
graph not yet known to be complete buys nothing. C++ likewise stays unstarted
until the audit lands, so its provider inherits the conclusions instead of
repeating the same gaps in a second language.*

*What the reservation was really buying was **more care than a normal pass**.
Substituting a stronger method for a stronger reviewer: slice 1 derives its
answer downward from how the JVM can load a class **at all** — a closed set —
rather than upward from remembered tricks, which is exactly how the
`@MethodSource` hole came to be missed; and slice 2 gets a mechanical
cache-equivalence oracle rather than a review pass, which is the better defence
against a silent stale pass anyway. `/code-review ultra` is available as a
scrutiny gate, but only Duane can trigger it.)*

### Slice 1 — escape-completeness audit (Java)

**The question:** enumerate every way a JUnit test's pass/fail verdict can depend
on code outside its static class closure, and argue the enumeration is *closed* —
not "here are some more cases," but a structural argument for why there are no
others. Use the direction lens above: the runner above the test is not a source;
anything at or below it is.

**The method — work down from class loading, not up from tricks.** A class can
enter a running JVM by exactly two routes (JVMS 5.3): *resolution* of a symbolic
reference from some constant pool, or an *explicit loading call*
(`ClassLoader.defineClass` and everything funnelling into it — `Class.forName`,
reflection, `ServiceLoader`, `Proxy`, `MethodHandles.Lookup`, JNI `DefineClass`,
an instrumentation agent). There is no third route. Route 1 *is* the closure.
So the enumeration closes if, and only if, two things hold for route 2:

1. every JDK entry point that loads a class **named by its caller** is in the
   detection vocabulary; and
2. the class making that call is always *inside* the closure — otherwise it is
   the harness, which sits above the test and is handled by direction.

Every framework trick (Spring's scan, JUnit's extension discovery, cglib
generation) *must* bottom out in route 2 — there is no other door — so a
framework class doing the loading is itself in the closure and already marked
dynamic. That reduces an open-ended hunt to two checkable obligations.

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
