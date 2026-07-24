# Development log

- **Test-impact selection wired into a real build (`JUnitTestAction`), oracle
  first.** The selection machinery was proven but inert — nothing in a build
  called it. Landed in two moves. First the safety net: a **cache-equivalence
  oracle** that drives an adversarial edit *sequence* against warm state
  (`known` grows, `prior_failed` carries) and asserts a believed-verdict map —
  green until a *selected* run says otherwise, never refreshed for a skipped
  test — equals a from-scratch full run at every step. Teeth-verified: blinding
  the change detector trips it exactly at the stale-pass moment. Then the wiring,
  on the design's **footprint rule** — selection changes only which classes the
  runner executes, never what the suite fingerprints (still the whole classpath),
  so any change that could matter still re-fires the suite and a stale pass is
  impossible. Selection is decided at **build-file evaluation time** (the
  `load_affected` seam), not in a new action type: `plan_suite` reads the
  provider's view of the *last build's* compiled artifacts — whose vocabulary
  **is** the set of units that existed last build, so `known` comes live from the
  view with no persisted-list drift and no warm-up lag — maps changed **source
  paths** to **units** (widening to inner classes; failing open on anything it
  cannot attribute), calls the core, and emits either `--select-class …` or the
  fail-open `--scan-class-path`. Two non-obvious correctness points found while
  building: (1) a missing artifact root lands in `uncovered`, so a cold build
  fails open through the incomplete-graph trigger — correct, if cosmetically
  leaking paths into `known`; (2) recomputing the command each build would
  **oscillate** (select→scan→select) and defeat the engine's replay, so a change
  that maps to nothing reuses the last command, keeping a no-op build a clean
  cache hit. Proven end to end on `examples/tested_lib`: edit `Adder` →
  `--select-class com.acme.AdderTest`, `GreeterTest` skipped, and the very next
  no-op build replays with nothing run. `prior_failed` from
  `BuildMetadata.has_failed_actions()` (coarse but safe for v1). 614 passed.

- **Escape-completeness audit (Java), part 2: all four name sources closed.**
  The first framing of obligation 1 — derive the JDK's class-loading entry
  points by reachability to `ClassLoader.defineClass` — was **withdrawn as a
  trap**. Soundness requires over-approximating virtual dispatch, under which
  nearly everything in the JDK reaches a class loader; the derived answer would
  be "almost every JDK method can load a class", every test would go dynamic,
  and selection would die — the same failure shape as the JUnit launcher, an
  analysis so cautious it conveys nothing. The obligation was stated wrong. The
  question is not whether a JDK method can load *a* class but whether it can
  load **one of our units**, and for that the JDK must first *obtain the name*.
  Enumerating name **sources** is bounded where enumerating loaders never was,
  and there are exactly four: our own code (the closure — a `Class` literal is a
  `CONSTANT_Class`, and a class computing names reflectively is already
  dynamic), configuration shipped with the code (classpath resource scan),
  **test data** (new: `resource_roots`, since a fixture can name a class and
  only code and libraries were being scanned), and **runtime input** (new:
  `runtime_values` — a JVM system property, env value or CLI argument). All four
  now feed `name_addressed`; all four are proven against a real JUnit run.
  The property case is not hypothetical: `-Djavax.xml.parsers.DocumentBuilder
  Factory=com.acme.MyFactory` makes the platform's factory finder instantiate
  one of our classes with **no file and no bytecode naming it anywhere**; teeth
  verified — blinded to `runtime_values` the oracle reports `UNDER-SELECTION
  editing com/acme/MyFactory: the full run fails ['com/acme/XmlTest'] but
  selection ran []`. **Residual, stated rather than papered over:** a name
  assembled at run time from data brix never sees (a database row, a network
  response) is undetectable statically; the existing mitigation is that such a
  loader is calling a reflective API and is therefore already dynamic, which
  makes the residual require a loader that is reflective yet reached by no test
  — near-contradictory for one that actually affects a verdict. Increment 2
  closes it fully. **Two invocation-dependent findings** worth knowing before
  anything changes how brix launches tests: `ToolProvider.findFirst` and
  `java.util.logging`'s `config.class` both resolve via the **system** class
  loader, which brix's `java -jar console.jar --class-path …` leaves empty of
  application classes, so that family cannot reach user code today; context-class
  loader consumers (the XML factory finder) do. Moving to `java -cp …
  ConsoleLauncher` would widen exposure — the scan is deliberately not
  conditioned on the invocation. 602 passed.

- **Escape-completeness audit (Java), part 1: the name-addressed fail-open.**
  First result of doing the audit *by method* instead of by recall. The method:
  a class enters a running JVM by exactly two routes (JVMS 5.3) — resolution of
  a constant-pool reference, or an explicit loading call — so the enumeration
  closes iff every caller-named JDK loading entry point is in the detection
  vocabulary **and** the class making the call is inside the closure. Working
  that second obligation found a gap both existing rules miss **by
  construction**: both key on the *reader* (mark whoever performs the load, run
  the tests reaching it), which fails when the reader is in no closure — a
  platform factory consulting `META-INF/services`, or **the harness itself**,
  which the direction rule deliberately excludes. A JUnit extension registered
  only in a service file is loaded by the launcher, changes what every test
  observes, and is named in no bytecode anywhere. Enumerating such loaders is
  unbounded (every JDK/framework entry point that reads configuration), so the
  fix **inverts it and keys on the target**, which is bounded: harvest dotted
  names from non-class classpath resources, keep those matching a scanned unit
  (`ProviderView.name_addressed`), and fail open when the change set touches
  that set's **closure** — once a class is loadable from anywhere, so is
  everything it depends on. The economics are what make it affordable:
  `dynamic_units` is a property of the reader and costs the tests reaching it on
  *every* build, while `name_addressed` is a property of the target and costs
  **nothing until it changes** — an untouched plugin registry is free. Proven in
  the real-JUnit oracle with a service-registered `BeforeEachCallback` no test
  references; teeth verified — with the rule disabled it reports
  `UNDER-SELECTION editing com/acme/Stamp: the full run fails
  ['com/acme/StampTest'] but selection ran []`. (Two dead ends worth recording:
  `ToolProvider.findFirst` pins the *system* class loader, which the console
  launcher's own loader shadows, so it cannot demonstrate this under brix's
  real `java -jar` invocation; and a monkeypatched `DEFAULT_TRIGGERS` does
  nothing, because `select_tests` binds it as a default argument at definition
  time — the first teeth-check passed spuriously until the wrapper was used
  instead.) **Still open: obligation 1** — the JDK caller-named loading list is
  enumerated, not derived; deriving it needs method-level reachability to
  `ClassLoader.defineClass`, which the class-level graph cannot express.
  599 passed.

- **Test-impact selection: direction fixes the harness question, and closes a real
  under-selection hole.** Duane's observation reframed the JUnit problem entirely:
  the runner reflects *once*, at the entry, to find a class **whose name brix
  supplied on the command line** — it is the test's *caller*, sitting above it,
  and everything the test does from there is ordinary static Java. So the
  launcher's internals were never a dependency of the test. The earlier reading
  ("should JUnit get an exemption from the safety rule?") was the wrong question;
  the reference walk was simply wandering upstream — from `Assertions` through a
  shared utility into the launcher — following *reference* edges that are not
  *call* edges, and then treating what it found as unknowable. Excluding the
  harness is a bug fix, not a carve-out. Mechanically that made the fix better
  too: `HARNESS_PREFIXES` (package-name matching, which anyone can claim — the
  same weakness Duane had just rejected in version numbers) is replaced by
  `harness_roots`, the **artifact** brix launched the suite with. A **role, not a
  reputation**: the designation follows whatever occupies the runner slot in a
  command brix generated. Withhold it and the same bytes are ordinary code whose
  reflection makes every test always-run — safe and useless, the correct
  direction to fail. Pinned by a test asserting both halves on identical
  bytecode. **The same reframing said where to actually look — at or below the
  test class — and that turned up a genuine hole:** `@MethodSource("com.acme.
  Providers#args")` makes a test's verdict depend on a class its bytecode never
  references, so the class closure would silently skip it. Now resolved —
  `STRING_CODE_ANNOTATIONS` + a well-formed-name match over the constant pool
  (annotation string values live in raw Utf8, not CONSTANT_String) — becoming a
  real edge when the named class resolves, and marking the test dynamic when it
  does not. The **real-JUnit oracle now covers it end to end**: a parameterized
  test whose provider is named only by a string, edited for real. Teeth verified —
  with the resolution disabled the oracle reports `UNDER-SELECTION editing
  com/acme/Providers: the full run fails ['com/acme/ParamTest'] but selection ran
  []`, i.e. exactly the false pass the invariant forbids. Still open and now
  higher priority: the escape list is demonstrably **not** proven complete —
  this one was found by argument, not by method — so the completeness audit
  should go to Fable *before* the fingerprint/cache wiring, not alongside it.

- **Test-impact selection, increment 1b: the Java provider, proven against real
  JUnit.** The first real provider behind `testimpact.Provider`, at Java's coarse
  binding unit — the class. `jvmabi.class_refs` reuses the existing class-file
  parser to extract each class's *runtime* type-reference set: the constant pool
  is the sound answer because the JVM resolves every symbolic reference through
  it, so a class cannot statically bind to a type its pool omits. This
  deliberately keeps what the ABI lens drops (private members, method bodies) — a
  private method's call is still a runtime dependency — and adds a descriptor
  scan, since `void f(Bar b)` binds `Bar` with no `CONSTANT_Class` of its own.
  `src/brix/testimpact_java.py` turns that into a `ProviderView` and emits JUnit
  `--select-class` args. Escapes are detected, not assumed away: reflection /
  `ClassLoader` / `ServiceLoader` / JNI mark the referencing class **dynamic**, as
  does a reference we cannot resolve to any scanned class (an un-supplied library
  might reflect back into the code under test) — both degrade *locally*, so the
  tests touching that class always run while the rest still select. Test units
  are top-level classes (what `--select-class` addresses), with `@Nested` grouped
  in and **inherited** tests discovered by closing "carries tests" down the
  subtype graph — `class FooTest extends AbstractContractTest` has no annotation
  of its own and missing it would silently skip a real test.
  `tests/test_testimpact_java_oracle.py` is the equivalence oracle against a
  **real junit-platform-console-standalone**: edit code, run the suite for real,
  assert every test failing in the full run is selected. Teeth verified — a
  one-hop (non-transitive) selector is caught missing `BTest` when `A` changes.
  **Open ruling for Duane:** the oracle exposed that JUnit's own launcher is
  wall-to-wall reflection and sits in *every* test's closure, so treating it as
  ordinary code collapses selection to a permanent full run. Resolved with an
  explicit `HARNESS_PREFIXES` boundary (junit/opentest4j/apiguardian treated as
  inert like the JDK), justified on the grounds that brix builds the run command
  and therefore knows the launcher reflects only on the classes it was handed —
  brix knowing its own tool, not a user vouching for their code. Application
  frameworks get no such pass (Spring's component scan stays dynamic). Flagged
  because it sits next to decision 3 ("no user-vouching override") and needs a
  ruling before `docs/test-impact-selection.md` records it. Not yet wired into
  `JUnitTestAction` — that crosses the fingerprint path and is the reserved Fable
  slice.

- **Test-impact selection, increment 1a: the language-agnostic core + equivalence
  oracle.** First buildable slice of the test-impact design
  (`docs/test-impact-selection.md`), proven against a synthetic provider before
  any real Java/C++ wiring. `src/brix/testimpact.py` is language-free: it operates
  on a pure `ProviderView` (code-dep graph + test→code edges + the units that do
  runtime dynamic loading) and returns the provably-equivalent set of tests to
  run, or fails open. The soundness model from the design is realized directly —
  a test runs iff its static code closure intersects the change set; a test whose
  closure reaches a **dynamic-loading** unit runs unconditionally (its real deps
  are unknowable), the only sound stance short of coverage. The **fail-open
  triggers are first-class and enumerable** (`new-or-renamed-unit`,
  `incomplete-graph`, `prior-failure`) so a future optional speed/correctness dial
  is a controlled relaxation, not a rewrite; any firing ⇒ run all. The
  **equivalence oracle** (`tests/test_testimpact.py`) is the ground truth: a
  synthetic world with real verdicts, where a test's *true* runtime deps can
  exceed its statically-visible ones (the dynamic case), asserts across
  break-every-unit (incl. 300 randomized worlds) that **no test failing in the
  full run is ever skipped**. Verified the oracle has teeth — it catches a naive
  static-only selector under-selecting the dynamic case. Inherits `affected.py`'s
  over-select-harmless/under-select-fatal invariant; no engine change (pure
  Python), not yet public API. Next: the first real provider (Java `jvm-abi` +
  `--select-class`), then C++.

- **Machine-readable status: `status --json` for frontends.** `brix_build.py
  status --json` emits the whole build state as one JSON document on stdout, so a
  web UI / IDE panel / CI report can consume it without scraping the text form.
  The shape is a *stable, versioned external contract* — `schema:
  "brix.status/v1"` (`STATUS_SCHEMA_VERSION`), deliberately decoupled from the
  internal engine↔Python wire boundary, with an additive-fields compatibility
  rule (consumers ignore unknown keys). It carries each action's
  `state`/`detail`/`fresh` and `last_result` (with derived `ok`/`summary`),
  `counts`, `pending_deletions`, the `staging` block, and the folded-in
  `environment` diff so a frontend gets everything in one call. Pure
  inspection, same exit code as text `status` (0 clean / 1 pending; errors go to
  stderr and return 2, keeping stdout pure JSON). Implemented as
  `StatusReport.to_public_dict()` — engine-agnostic, since `compute_status`
  builds the same dataclass on both engines (no Rust change) — plus a CLI
  `--json` flag. `tests/test_status_json.py` (both engines); schema documented in
  `docs/status-json.md`.

- **Env phase-3 Fable review: positive-with-fixes, two holes closed.** The
  reserved Fable review of the phase-3 env diff (the piece that crosses the
  fingerprint path) confirmed the design and found two real correctness holes,
  both now fixed. **(1) `UNSET` was identity but not fingerprint.** `env_unset`
  was in `action_key` but absent from `action_fingerprint`, and `CacheLayer`
  keys purely on the fingerprint — so a cached artifact built while a variable
  was ambiently visible could be served, on a cache hit, to a later build that
  had since declared `env={"X": UNSET}`, silently defeating the intent. Added
  `env_unset` to `action_fingerprint()` in both engines
  (`src/brix/incremental.py`, `rust/brix-core/src/content.rs`), only when
  non-empty (no pre-existing fingerprint moves), with a new fingerprint golden
  case and two pins: `test_unset_is_fingerprint_material` and the end-to-end
  cross-engine `test_unset_flip_misses_the_cache`. **(2) A pinned env on a
  worker-routed action failed silently, not loudly.** A warm `javac`/`kotlinc`
  worker's environment is fixed at launch, so a literal `env=` or `UNSET` can
  never reach it — yet it still entered the fingerprint as if it had. Now a
  constructor-time `ValueError` (message "fixed at launch") rejects `worker=`
  combined with a literal value or `UNSET`; `SYSTEM`/`PASS` stay legal
  (`src/brix/actions.py`, `test_worker_routed_action_rejects_pinned_env`). Also
  added a `BASE_ENV` Python↔Rust parity test
  (`test_base_env_matches_the_rust_engine`) against silent drift. **Ruling:**
  the `scrub` flag is deliberately not fingerprinted — it only removes
  undeclared ambient variables, and everything that can affect output is already
  declared and fingerprinted, so scrub can never change a correct result. It is
  provenance, not a cache key. All green: 552 Python passed, Rust workspace ok,
  9 goldens. (Merged onto the branch alongside the `brix_build.py` rename.)

- **Env phase-3 complete: the scrub runtime (authoritative environment).** The
  final piece, and what turns "no silent env" from *mitigated* into *guaranteed*.
  `build_stack(scrub=True)` / `IncrementalBuilder(scrub=True)` runs each process
  with an environment built **from scratch** — `BASE_ENV` plumbing
  (`PATH`/`HOME`/`TMPDIR`, PASS) + the action's declared allowlist (its `SYSTEM`
  and `PASS` names, resolved from the ambient env) + literal `env=`, minus
  `UNSET` — with nothing inherited. So an undeclared ambient variable is simply
  **absent** and cannot silently affect the build. Opt-in; scrub off is the
  unchanged additive default. The build-wide flag rides the incremental layer:
  Python's `execute` reads it and sets `NodeContext.scrub`; the Rust engine emits
  a `scrub` layer-spec flag read in `lib.rs` and set on its `NodeContext`. Both
  executors share the same `scrub_env` construction (`env_clear` in Rust,
  build-from-empty in Python), the allowlist names (`env_pass`/`env_used`)
  crossing the wire. Proven identical on both engines in `tests/test_env_system.py`
  (an undeclared var absent under scrub, declared `SYSTEM` + `PATH` present; and
  the scrub-off counterpart where it leaks). Goldens unaffected. **The whole
  phase-3 env diff (SYSTEM/UNSET/PASS + scrub + per-language defaults) is now ready
  for the Fable review**, since it crosses the fingerprint path.

- **Env phase-3, increment 2: per-language default env maps (java, rust).** Each
  language module now tracks its toolchain's behaviour-affecting environment by
  default, via `SYSTEM` — `brix.java` compiles track `JAVA_HOME`,
  `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS` (the last two are applied to *every* JVM
  launch, so they silently reach `javac`); `brix.rust` cargo builds track
  `RUSTFLAGS`, `RUSTUP_TOOLCHAIN`, `CARGO_HOME`, `CARGO_NET_OFFLINE`. So editing
  one re-stales the build automatically — the module names the latent trap for
  the user, and the payoff lands even with scrub off. User `env=` layers on top
  (a pinned var wins and moves from tracked to set). It reuses the blessed
  `SYSTEM`≡`env_used` path (no new mechanism, no Rust change), and the tracked
  vars are unset in a normal environment (null snapshot → no spurious rebuilds),
  proven on both engines in `tests/test_env_defaults.py`. **cpp's overlay is
  deferred**: its actions appear in the cross-engine goldens, so it needs a
  golden regeneration (clean, since the vars — `CPATH`, `SOURCE_DATE_EPOCH`, … —
  are unset at generation → null), handled with the `PASS`/`UNSET`/scrub work
  under the Fable review. `LANG`/locale is also deferred (usually set → would make
  cross-machine fingerprints noisy; it belongs with the plumbing base map).
  `src/brix/java.py`, `src/brix/rust.py`.

- **Env phase-3, increment 1: the `SYSTEM` sentinel (opt-in inherit-and-track).**
  First buildable piece of the phase-3 env design (`docs/environment-recording.md`).
  `env={"X": SYSTEM}` on an `ExternalProcessAction` means "take X from the ambient
  environment and fingerprint it, so a change re-stales the action" — without
  pinning it to a constant. It is implemented as pure **desugaring into the
  phase-2 `env_used` mechanism** (`brix.env.SYSTEM`; `actions.py` folds `SYSTEM`
  entries into `env_used`), so it produces **byte-identical fingerprint material**
  to `env_used=["X"]` — the already-Fable-reviewed, cross-engine path. That's the
  safety argument for shipping it as Opus work with **no fresh fingerprint-path
  review**: `tests/test_env_system.py` pins the equivalence, the env-vs-env_used
  split, and the behavioral re-stale on both engines. Being fingerprint-only,
  `SYSTEM` also works on the warm-worker path (a persistent worker can be tracked
  but not set). **Deferred to the next, Fable-gated increment** (they change
  fingerprints): the per-language default maps (auto-`SYSTEM` the behaviour vars —
  changes every Java/C++/Rust action's fingerprint, needs golden regen), the
  `PASS`/`UNSET` sentinels, and the authoritative-env scrub. `src/brix/env.py`,
  `src/brix/actions.py`.

- **`brix.rust` — wrap cargo, and self-host (brix builds brix).** brix supported
  C/C++/Java/Kotlin/TS but not Rust — the language its own engine is written in.
  `brix.rust` closes that. It is deliberately a *different* model from the others:
  Rust ships **cargo**, a mature build tool that already does crate/dependency
  resolution and within-crate incremental compilation, so brix does not replace
  it — `rust.cargo(manifest)` reads `Cargo.toml` (the adopt-the-manifest pattern,
  like `java.pom`) and wraps `cargo build` as one tracked action, inferring the
  output artifact from `[lib].crate-type` and a conservative source footprint
  (every `.rs` in the workspace + manifests + lock). A crate becomes a node in a
  polyglot graph — cached, lazy-loaded, wired to other languages by its artifact.
  Two honest limits, both documented: skip granularity is **crate-level** (cargo
  owns the finer work — brix can't see inside a `cargo build`), and a cargo action
  wants the **plain incremental stack** because cargo writes its own `target/`,
  which atomic staging can't redirect (the foreign-resolver rule, like tsc's
  node_modules). It is also the **first wrapper to thread `env=`/`env_used=`**
  through to the action (cargo is env-sensitive: `RUSTFLAGS`, offline registry),
  setting the precedent the JVM/C++ wrappers can adopt. Proven two ways:
  `examples/rust_ffi/` links a Rust C-ABI `staticlib` into a C program and shows
  the derived **cargo → link** edge (editing the Rust source reruns cargo and
  relinks the C app while the unchanged C compile replays); and
  `examples/self_host/` **builds brix's own engine** (`rust/brix-py`, a cargo
  workspace) — the self-hosting dogfood, verified to produce `libbrix_py.so`.
  `tests/test_rust.py` (lowering, manifest inference incl. the self-host artifact,
  a real crate build, and the gated FFI relink). `src/brix/rust.py`,
  `docs/dependencies.md`.

- **`ts.npm` — the node_modules dependency resolver for TypeScript.** Fills the
  one ecosystem the resolver adapters were missing (Java had `pom`/`maven`/`gradle`,
  C++ had `pkg_config`, TS had nothing). `ts.npm([names])` reads an installed
  `node_modules` and collects each package's `.d.ts` type declarations — its own
  type tree plus `@types/<name>` for DefinitelyTyped packages, with scoped names
  (`@scope/pkg`) mapped to DefinitelyTyped's mangled `scope__pkg` and nested
  `node_modules` skipped — into a `DepSet`. `ts.CompileAction(deps=…)` folds them
  into the dependency list, **ts-dts-lensed**, so a dependency upgrade whose
  types changed re-stales the compile while a comment-only `.d.ts` change
  replays. The design mirrors C headers and TS imports: **tsc resolves
  `node_modules` off the real filesystem itself**, so the `.d.ts` are *tracked
  inputs*, not command arguments — which also means no new fingerprint material
  and no new lens, so it is Opus work, not a Fable review (the `java.pom` /
  `cpp.pkg_config` adapter pattern, applied to npm). brix does not run npm; you
  `npm install` and the adapter consumes the result, raising actionably on a
  package with no discoverable types. `tests/test_ts_npm.py` lays out a fake
  `node_modules` (own-types, `@types`, scoped, nested) and checks resolution and
  the ts-dts tracking with no tsc needed. `src/brix/ts.py`, `docs/dependencies.md`.

- **Polyglot Java+TS monorepo dogfood — cross-language lazy loading.** The
  single-language dogfoods (`hierarchical/` for the generic tool, `monorepo_dogfood.py`
  for C) never exercised the trial's actual shape: a mixed Java+TS workspace.
  `examples/polyglot_monorepo/` fills that — a Java backend (`libs/model`,
  `services/orders`, `java.JarAction`) and a TypeScript frontend (`shared`,
  `app` where `app` depends on `shared` via the ts-dts lens, `ts.CompileAction`)
  in one `load_workspace`. The claim it proves that the others don't: **lazy
  loading partitions a *mixed-language* workspace** — `load_affected` on a
  backend edit loads no frontend package and vice versa, while the intra-stack
  `shared → app` edge is still honored. Selection is graph structure, so it is
  verified with no build tools (proven here for all five change sets); the
  harness then builds for real what the toolchain allows — the Java slice here,
  the TS slice where `tsc` is present. Git-driven selection (`changed=None`) is
  the same call, already proven in `monorepo_dogfood.py`, so this uses explicit
  change sets for determinism. Built as the closest available rehearsal of the
  air-gapped ~50-service Java+TS target while that repo is access-gated.
  `verify_polyglot.py`; `tests/test_polyglot_monorepo.py` guards the partition
  (both engines) and the Java build.

- **Starter templates — a copy-and-go Java service and TypeScript service.**
  `examples/templates/java-service/` and `examples/templates/typescript-service/`:
  minimal, generic, *working* projects to copy as the start of your own, not
  feature demos. Each is a `brix_build.py` on the recommended `build_stack` (atomic +
  cache + incremental for Java; cache + incremental for TS, since `tsc`
  self-resolves imports off the real filesystem) with a `build`/`run`/`status`
  CLI, a target alias, and one placeholder source — change a couple of names and
  drop in your code. Two bootstrap subtleties are handled and documented: a
  marked "delete in your copy" block that only lets the template import brix from
  this repo in-tree. The `brix_build.py` name avoids shadowing the installed
  `brix` package. `tests/test_templates.py` copies each template out (so the repo stays
  clean and the copied-out path is what runs), does a tool-free parse-and-plan
  check on both, and a gated end-to-end build+`run` (Java verified here; TS gated
  on `tsc`/`node`). Indexed in `examples/README.md`. Follow-up: C++ and Kotlin
  templates.

- **"Always re-check file contents" mode — the general stat-collision escape
  hatch.** The default detector trusts `(mtime, size)` to skip re-hashing, so a
  content change that leaves both unchanged is missed — the general form of the
  collision the symlink-repoint fix closed for one case. `verify_contents=True`
  (on `IncrementalBuilder`/`build_stack`) forces a re-read+re-hash of every file,
  trading speed for certainty. Opt-in and **fingerprint-compatible**: it never
  changes the returned digest for an actually-unchanged file, so a genuine no-op
  still replays (proven), and it only changes *when* brix reads, never *what* it
  computes. Both hash paths (`hash_file`, `identity_digest`/`content_key_digest`)
  skip the stat cache — and the git vouch — when set. Configures the default
  detector and crosses the boundary as a `verify_contents` wire flag so the Rust
  detector honors it natively. `tests/test_verify_contents.py` documents the
  fast-path blind spot and proves the mode catches it, on both engines. Follow-up:
  a `build --verify-contents` CLI flag for one-off runs. `src/brix/metadata.py`,
  `src/brix/builders.py`, `rust/brix-core/src/detector.rs`, `rust/brix-py/src/lib.rs`.

- **Symlink repoint detection — close a stale-build hole in the stat cache.** A
  symlinked source was already stat'd *through* to its target, so a target
  content change moved its (mtime, size) and rebuilt normally. The subtle hole:
  a symlink **repointed** to a *different* file sharing the old target's
  (mtime, size) — the release-style `current -> vN` swap, with mtimes preserved
  by `tar`/`rsync -a`, which turns an astronomically-rare stat collision into a
  plausible one — collided and served the old target's cached hash (a stale
  output; reproduced before fixing). Fix: `TimeAndHashChangeDetector.stat_signature`
  now folds the link target into the signature (`_symlink_target_sig`: the first
  8 bytes of sha256 of the `readlink` target, appended *only* when
  `path.is_symlink()`), so repointing busts the hash cache even under a stat
  collision. It is **symlink-only** — a regular file's signature and its cached
  hash are untouched, so nothing re-hashes spuriously — and a **pure performance
  layer**: it validates the stat cache and never enters a fingerprint (like the
  git detector), so it needs no Fable review. Mirrored natively in the Rust
  detector under the byte-identical rule (both compute the same target int; a
  non-UTF-8 target may diverge across engines, which only forces a safe re-hash,
  never a stale hit). `tests/test_symlink_detection.py` — repoint-with-colliding-
  stat rebuilds, target-content-change rebuilds, stable-target replays — runs on
  both engines. `src/brix/metadata.py`, `rust/brix-core/src/detector.rs`.

- **Doc-generation actions for the *user's* project (javadoc/typedoc/doxygen).**
  Three typed actions that run an ecosystem's own API-doc generator over a
  project's sources as an ordinary, tracked, incremental build step:
  `java.JavadocAction`, `ts.TypedocAction`, `cpp.DoxygenAction`. Each lowers to
  a single `ExternalProcessAction` that declares the sources as **inputs** (so
  the doc set rebuilds only when a source changes, replays otherwise, and caches
  like any output) and its whole emitted tree as one `FileData` output dir plus
  a recursive `{name}` glob (so it stages atomically; a removed page is
  reflected). `JavadocAction` accepts an abi-lensed `classpath` (impl-only
  dependency change → replay); `DoxygenAction` is config-driven
  (`doxygen <Doxyfile>`), so the sources are declared inputs but not command
  arguments, and the caller passes `output_dir` explicitly to match the
  Doxyfile's `OUTPUT_DIRECTORY` since brix can't read it out of the config. All
  three are added to their modules' curated `__all__`, so they land in the
  public API site automatically (verified in the rebuilt `site/`). Tests:
  lowering-shape assertions for all three plus a real-`javadoc` end-to-end test
  proving regenerate-on-edit / replay-on-no-op (`tests/test_docgen.py`). This is
  doc generation offered *to* the projects brix builds — distinct from brix's
  own HTML site below. v1 threads no classpath/tool-path resolution and has no
  warm daemon; a Python `pdoc` action is a follow-up. `src/brix/java.py`,
  `src/brix/ts.py`, `src/brix/cpp.py`, `tests/test_docgen.py`.

- **HTML documentation site (public API only), first version.** A MkDocs +
  Material + mkdocstrings site: a hand-written landing page (purpose + tenets,
  distilled from the spec's design rules into user-facing form) plus an API
  reference **generated from the source docstrings**, so it never drifts from
  the code. Scoped to the public authoring surface only — the prerequisite
  `__all__` curation on the eight public modules is the boundary, and a build
  check confirmed mkdocstrings honors it (the C++ page shows its four public
  symbols and zero leaked imports). Structure: `mkdocs.yml` + `docs/site/`
  (`index.md`, `api/*.md` with `:::` mkdocstrings directives grouped as core,
  data, the language helpers, CLI, and the extension seams). Builds fully
  offline (`docs/requirements.txt`; Material bundles its assets, no CDN) so it
  runs on the air-gapped machine; `site/` output is gitignored (build on
  demand via `mkdocs serve`/`build`, wired into the README). Known cosmetic
  follow-up: the docstrings' RST-ish inline markup renders readably but not
  perfectly under Markdown. `mkdocs.yml`, `docs/site/`, `docs/requirements.txt`.

- **End-to-end monorepo dogfood — the whole stack, the trial's workflow.**
  `examples/monorepo_dogfood.py` runs a git-backed hierarchical C monorepo
  (`services/util` static library + `services/app` that links it) the way the
  air-gapped trial will: git-driven `load_affected` (changed=None → git
  enumerates what changed) selects the packages to load, and a git-accelerated
  incremental build — native on the default Rust engine — rebuilds only what
  changed. It runs a real edit sequence with `cc`/`ar` and asserts *what
  actually rebuilt*: cold build compiles everything (output 42); a no-op loads
  zero packages and runs nothing while preserving the binary; editing `util`'s
  source recompiles util + relinks app but replays app's compile (git-vouched
  unchanged, output 100); editing `app` rebuilds only app while util does no
  work (101); editing the shared header full-loads (a discovered depfile input,
  unattributable to a package) and rebuilds both includers. This exercises
  tiers 1–3, derived cross-package deps, the git-detection Rust mirror plus its
  cross-engine metadata interop (the Rust build writes the baseline the Python
  change enumeration reads), and C header tracking — all composed, with no test
  scaffolding. Guarded by `tests/test_monorepo_dogfood.py` (skips without
  cc/ar/git). **Finding:** the build output dir must be gitignored, or git-driven
  detection sees the untracked outputs as changes and defeats the no-op —
  recorded as a trial precondition in `docs/monorepo-scaling.md`.

- **Selector fix: persist the cross-package edges (kill the O(n²) selection).**
  The lazy-loading benchmark showed selection re-deriving every cross-package
  edge per call — O(packages² × modules²), 11 s at 100 packages — which dwarfed
  the construction it saved. Fixed by persisting the derived `depends_on` in the
  index (v3): the edges are computed once on a *full* load (the one load that
  has the complete graph) and stored, so `load_index_data` reads them
  O(packages) instead of re-deriving (`_derive_deps_from_entries` and its
  helper are gone). A subset save carries the persisted edges forward — sound
  because a subset build only happens when no `brix_build.py` changed, so patterns are
  stable — and drops them (so the next build full-loads and re-derives) only
  when a footprint-driven reload actually changed a loaded package's
  input/output patterns; edges depend solely on those patterns, so this is
  exact. Selection dropped from 11 s to 52 ms at 100 packages (~220×) and is now
  linear (~0.5 ms/package, mostly the discovery walk), so lazy loading is a net
  win at every scale measured. Regression-guarded in `tests/test_affected.py`
  (edges are persisted; a missing-edges index forces a full load; a source edit
  carries edges forward while a footprint pattern-change drops them) with the
  equivalence oracle still green on both engines. `src/brix/workspace.py`,
  `examples/lazy_loading_benchmark.py`.

- **Quantified the lazy-loading win — and found the selector is O(n²).**
  `examples/lazy_loading_benchmark.py` isolates graph-construction time (the
  cost lazy loading removes, above incrementality), full-load vs affected-only,
  across a package-count ladder. Result: the design thesis holds — full-load
  construction grows ~linearly with package count (2.9→77 ms for 10→100
  packages) while affected-only stays flat (~0.3→1.3 ms), so the construction
  speedup *rises* with repo size (8.6× at 10 packages, 62× at 100). **But the
  benchmark exposed a real defect**: `select_affected` re-derives every
  cross-package edge on each call (`_derive_deps_from_entries`,
  O(packages² × modules²), regex-compiling), so selection grows quadratically —
  82 ms at 10 packages, 11 s at 100 — and dominates past ~a dozen packages,
  dwarfing the construction it saves. The loading win is real; the selector is
  the bottleneck. Fix (not yet done): persist the derived `depends_on` edges in
  the index at full-build time so selection reads them O(packages) instead of
  re-deriving. Recorded in `docs/monorepo-scaling.md`; the benchmark prints the
  finding. No production code changed — this is measurement.

- **Git-accelerated change detection, mirrored into the Rust core.** The git
  detector was a Python-engine-only layer, so the default (Rust) engine — the
  one the air-gapped trial will run — paid the full per-file `stat` scan.
  `rust/brix-core/src/gitdetect.rs` now ports the vouch: git subprocess helpers,
  a `GitVouch` (tracked ∖ changed-since-baseline ∖ previous-build-dirty), and a
  `prepare()` that records the next build's (HEAD, working-tree dirty set)
  baseline pair — including the revert-hole fix — and returns a vouch only when
  a prior pair exists and every git call succeeds (else degrade to the stat
  scan). It is folded into `TimeAndHashChangeDetector` as an optional `git`
  field rather than a trait refactor: a vouched file returns its last-build
  hash/identity from metadata with no `stat` or read, and the detector is shared
  with the cache layer exactly as before. The metadata gained `git_baseline`/
  `git_dirty` and signature-free `hash_by_path`/`identity_by_path` under the
  same JSON keys as Python, so the shared `metadata.json` round-trips between
  engines. Opt-in and unchanged at the authoring layer: `detector=`
  `GitChangeDetector` now accelerates *both* engines — the class can't cross the
  boundary, so `IncrementalBuilder.layer_specs` emits a `git_detection` flag
  that turns on the native mirror (`GitChangeDetector.wire_git_detection` is the
  marker). Proof: the git/stat equivalence oracle in `tests/test_git_detect.py`
  now runs on **both** engines (a pure performance layer must not change a
  rebuild decision), plus a guard test that the Rust path is genuinely engaged
  (only it writes the baseline — otherwise a silent fall-back to stat would pass
  the equivalence check unnoticed), and six native vouch tests in
  `rust/brix-core/tests/gitdetect.rs` mirroring the Python targeted cases. Rust
  suites green, clippy clean on the new code; 486 Python tests.

- **TypeScript automatic import-dependency tracking.** Closed the stale-build
  hole where a module `import`s a shared `.ts` outside its declared `sources`
  glob (or an ambient `.d.ts`) — the TS analogue of the C/C++ header problem.
  `ts.CompileAction` now defaults to `track_imports=True`, wrapping `tsc` in
  `src/brix/_ts/tsc_listfiles.py`, a `--listFiles` recorder that writes a
  **Makefile-format depfile** of the workspace `.ts`/`.d.ts` the compile
  actually read. That reuses the existing discovered-deps channel wholesale —
  both engines already scan Makefile depfiles (`scan_depfile`) — so there is
  **zero engine change**, confirmed by running the new integration test on both
  the Rust and Python engines. The recorder excludes the module's own `out_dir`
  and each *declared* dependency's directory, so a declared upstream `.d.ts`
  stays governed by the ts-dts lens (impl-only upstream change → replay);
  byte-tracking it via the depfile would have defeated that shortcut, so the two
  mechanisms are kept strictly complementary — the depfile is only for the
  undeclared source imports the lens doesn't cover. Absolute lib/`node_modules`
  and out-of-tree paths are dropped, exactly as the C/C++ scanner drops absolute
  system prerequisites. Proof: `tests/test_ts_import_tracking.py` drives the
  whole channel with an executable **fake tsc** (models `// import:` comments,
  emits `.js`/`.d.ts`, prints an absolute `--listFiles` list) — editing an
  undeclared imported source re-runs the compile, an unimported sibling does
  not, and the depfile records only workspace sources — plus a gated real-tsc
  case in `tests/test_ts_equivalence.py`. `track_imports=False` restores the
  bare single-spawn. `src/brix/ts.py`, `src/brix/_ts/tsc_listfiles.py`,
  `tests/test_ts.py`.

- **Worked example for hierarchical/lazy loading (`examples/hierarchical/`).**
  The distributed-`brix_build.py` feature was tested but had no user-facing surface;
  this is the runnable end-to-end demonstration and the adoption template a
  real service tree copies. A `core → api → web` dependency chain plus an
  independent `docs` package, split into per-package `brix_build.py` files sharing
  one `tools/step.py`; the root `brix_build.py` is the workspace entry point
  (`load_workspace(ROOT).graph`, the safe full-load default) and documents the
  one-line `load_affected` opt-in. `verify_lazy.py` is the proof: it drives
  `load_affected` with explicit change sets (no git state needed) and checks
  both the loaded package set and every subset build's outputs, byte-for-byte,
  against a from-scratch full build — a leaf-source edit loads the chain and
  skips `docs`; an independent edit loads only `docs`; editing the shared tool
  loads everything (the soundness guarantee, since it lives in no package's
  subtree — ownership-based selection would wrongly skip); an unreferenced
  edit loads nothing. Idempotent (snapshots and restores every file it
  touches). Registered in `examples/README.md`; pointer added from
  `docs/monorepo-scaling.md`.

- **`env_used=` phase-2 review (Fable) — verdict positive, one bypass
  closed.** The scoped checklist held: unset-vs-empty deterministic and
  engine-identical; no process-env or command leak; verbatim snapshot under
  nested expansion; golden genuinely exercises `_brix_env_used` (Rust
  re-derives the pinned fingerprint). Found: `substitute`-idempotence keyed on
  the reserved key's *presence* in `inputs`, so an author-supplied
  `_brix_env_used` silently replaced the live snapshot with a constant — the
  exact stale-direction bypass the guard exists to prevent. Fixed by
  rejecting the reserved key loudly and threading the frozen snapshot through
  a private `_env_used_snapshot` parameter in `substitute`. Behavior- and
  fingerprint-identical for legitimate use (golden regeneration: zero diff);
  pinned by a new rejection test. Miss-only note: surrogate-escaped (non
  UTF-8) env values fail loudly at the boundary, never silently.
  `src/brix/actions.py`, `tests/test_env_used.py`,
  `docs/environment-recording.md`.

- **Monorepo scaling, tier 3 — the lazy-loading completeness core (Fable),
  plus a real staleness fix in the git detector.** `src/brix/affected.py`:
  `select_affected` decides the provably complete set of packages to load for
  a change set, `load_affected(root, changed=None)` wires it (composition, no
  builder wrapper: enumerate → select → subset-load → refresh index). Two
  load-bearing findings shaped it. (1) **Ownership-based selection is unsound**
  — packages legitimately reference paths outside their subtree (a shared
  `tools/` script in no package's subtree), so "nearest owner of the changed
  file" under-selects; selection instead matches changed paths against
  **persisted per-package input/output patterns** (index v2 of
  `workspace-index.json`), extracted with the planner's own atom helpers
  (worker inputs included, glob excludes dropped = safe over-match), plus each
  package's construction footprint, then closes over dependents; the
  cross-package edges are themselves derived from the persisted patterns
  (`patterns.overlaps`, no shared bindings — conservative), so matching and
  edges cannot drift apart and a subset load refreshes only its own entries
  (`save_index` merges). FULL-load fallbacks: no index; any `brix_build.py`
  added/removed/edited; a changed path in the discovered-deps table (per-action
  keys, unattributable without loading); out-of-tree changes; failed-build
  recovery via `last_selected` (written at load time, before the build).
  (2) **The shipped `GitChangeDetector` had a revert-staleness hole**: a file
  dirty at build N is hashed with content git never saw; reverted to HEAD
  before build N+1, git calls it clean and `baseline..HEAD` is empty, so the
  stale cached hash was vouched — the extended git/stat equivalence sequence
  (dirty-build-then-revert) catches it, and the fix persists each build's
  working-tree **dirty set** paired with the baseline (a previously-dirty path
  is never vouched; if either half is unknowable both are cleared).
  `changed_paths()` — the graph-independent enumeration for the selector —
  includes that dirty set for the same reason (a reverted file *did* change
  since the last build). Gate: `tests/test_affected.py` replays the real
  `load_affected` flow against a full load across the whole edit alphabet
  (multi-hop lib→mid→app ripple, shared-tool edit, dynamic-config footprint
  reload, glob-member deletion, new package, dependency rewired via `brix_build.py`
  edit → FULL, irrelevant edit → empty selection), proves laziness (a lib edit
  selects exactly {lib, mid, app}), covers every FULL fallback, and keeps a
  negative control (footprint-blind selector must diverge). Opt-in; the full
  load stays the default. Suite: 483 Python (new surface green on both
  engines); 15 Rust suites clean.

- **Monorepo scaling: the tier-3 scaffolding (Opus half of the Fable-worthy
  tier).** The lazy-loading *decision* core is Fable-authored (a mis-loaded
  package = silent stale build), but the mechanical infrastructure it needs is
  safe Opus work, and it is now in place. Three pieces: (1) the **subset-load
  primitive** `load_workspace(root, only={packages})` — loads exactly the
  packages it is told and merges just those, no decision (unselected packages'
  outputs are assumed already fresh on disk from a prior full build); (2)
  **loading-footprint tracking** — a process-wide audit hook (cheap when
  inactive) records the files a package's `create_graph` reads at construction,
  so a build that decides dependencies dynamically (`open("deps.txt")`) can be
  reloaded when that file changes — the case a static glob pattern doesn't cover
  — filtered to repo inputs, excluding the package's own build file and brix's
  machinery; (3) the **equivalence oracle** (`tests/test_workspace_oracle.py`),
  the safety gate: `assert_selective_equals_full` replays an edit sequence two
  ways — a full load and a *selector*-driven selective load — and asserts the
  build outputs are byte-identical. The tests prove it is a real gate: it passes
  a correct selector (owners of changes + their transitive dependents from the
  persisted index) and **catches** a deliberately stale-producing one (owner
  only, omitting the dependent). The Fable-authored piece is the *selector*
  itself — the provably-complete affected-set computation — plus the
  `DistributedBuilder` that wires it, authored against this oracle, post-trial.
  `src/brix/workspace.py`, `tests/test_workspace_oracle.py`.
- **Hierarchical build files, tier 2: the derived cross-package index.** Builds
  on tier 1 (docs/monorepo-scaling.md). `brix.workspace` now derives the
  package-level dependency graph — "which package consumes which package's
  outputs" — straight from the actions' data references, using the planner's own
  `infer_dependency` (the exact edge logic it applies action-to-action, which it
  runs on raw actions including `ExpandAll`, so this is not an approximation).
  No cross-module dependency *declarations*: the information is already in the
  graph, so it is derived, not restated. `Workspace.depends_on` /
  `dependents_of` expose it and `save_index`/`load_index` persist it to
  `build/.brix/workspace-index.json` (repo-relative), so a future build that
  loads only a subset can consult it without a full load. Safe to build ahead of
  the trial because **nothing consumes the index yet** — it is derived and saved,
  never used to skip a load, so there is no staleness surface (the same
  "record now, act later" shape as environment-recording phase 1). The
  loading-footprint half of tier 2 (reload a package if a glob/file its
  construction read changed) is deferred to land with the lazy-loading tier,
  since it is only load-bearing once the index is actually consumed.
  `src/brix/workspace.py`, `tests/test_workspace.py` (derivation from data
  overlap, the reverse index, and the persistence round-trip).
- **Hierarchical build files, tier 1 (monorepo scaling).** The safe foundation
  of the plan in `docs/monorepo-scaling.md`: a large repo can split its one root
  `brix_build.py` into several placed at module/service boundaries, each a *package*
  owning its subtree until interrupted by a deeper `brix_build.py` (the Bazel package
  model). `brix.workspace` provides discovery (`find_packages`), nearest-ancestor
  ownership (`owner`), a graph union that remaps the index-based `forced_orders`
  (`merge_graphs`), and `load_workspace` — which imports every package's
  `create_graph()` and merges them into one ordinary `Graph` that plugs straight
  into the existing builders (`cli.main(lambda: load_workspace(ROOT).graph, …)`),
  so no builder change was needed. This tier is deliberately **non-lazy**: every
  package is loaded and merged, which is exactly why the load-bearing test proves
  a hierarchical workspace builds **byte-identically to the equivalent monolith**
  — the "monolith is the degenerate one-root-file case" contract, made mechanical.
  The ownership map it computes now is what the deferred tiers (derived reverse-
  dependency index, then lazy loading) will consume to load only the packages an
  edit touches; nothing yet skips a load, so there is no staleness surface here.
  Paths stay workspace-root-relative (the merge is a pure union, no rebasing).
  `src/brix/workspace.py`, `tests/test_workspace.py`.
- **Git-accelerated change detection (opt-in reference path).** brix finds what
  changed by stat-ing every declared input; on a large working tree those stats
  become the floor. The change detector is an injected seam —
  `IncrementalBuilder(..., detector=GitChangeDetector)`, threaded through
  `build_stack(detector=...)` too — and `brix.gitdetect.GitChangeDetector`
  wraps the default stat detector, trusting a *tracked*,
  git-unmodified file's stored hash without stat-ing it, and falls through to
  stat/hash for everything git can't vouch for — untracked files, ignored files
  (crucially the build's own generated intermediates), and inputs outside the
  repo. It is a pure performance layer that must never change a fingerprint; the
  correctness obligation is exactly the vouch ("tracked and unmodified since the
  recorded baseline ⇒ content equals the last build's, so the cached hash is
  valid"), proven by an equivalence oracle that replays an edit sequence (clean
  build, no-op, edit-tracked, add-untracked, commit, edit-now-tracked) under
  both detectors and asserts byte-identical rebuild decisions at every step.
  Design decisions worked through before building: git is the authority on its
  own ignore rules (the vouchable set is `git ls-files`, never a reimplemented
  `.gitignore` — brix already knows its inputs from the graph, so it only asks
  git to classify a small known set); "since last build" (not "since last
  commit") is the working-tree diff unioned with `git diff <baseline>..HEAD`
  where the baseline is the HEAD recorded at the previous build, so commits
  between builds are caught and no one is ever forced to commit; and it degrades
  to the stat scan on any git absence or failure, always in the safe direction
  (never over-vouch). It inherits git's fsmonitor speedup for free when the user
  has enabled it — no Watchman dependency, no sudo — which is what makes it
  viable on a locked-down or air-gapped box. Deliberately shipped opt-in and
  Python-engine-first: change detection is the one component where a miss is a
  silent stale build, so it earns default-on only after soak against real repos,
  and not before the air-gapped trial. Follow-ups: mirror into the Rust core (it
  only accelerates `BRIX_ENGINE=py` today) and, later, the default-for-git-repos
  flip. `src/brix/gitdetect.py`, `src/brix/metadata.py` (baseline +
  signature-free lookups), `tests/test_git_detect.py`.
- **Guard: AtomicBuilder must be the outermost builder.** The staging overlay
  and the P0 staging-hazard guard both hang off `AtomicBuilder`, so burying it
  inside another builder — `CacheBuilder(AtomicBuilder(...))` — silently
  bypasses the hazard guard (a hazardous foreign-resolver graph that the correct
  nesting *refuses* would build anyway) and puts commit/discard out of reach,
  even though the layer specs self-order so the build superficially works. Both
  facts were reproduced before fixing. Two guards now prevent it, in the spirit
  of "typed language would make it a compile error, Python should at least raise
  and hint": (1) a nominal type hierarchy — a `NestableBuilder` marker base that
  `CleanBuilder`/`IncrementalBuilder`/`CacheBuilder` inherit and `AtomicBuilder`
  deliberately does **not**, so the wrapping builders' `inner: NestableBuilder`
  annotation makes a type checker (and any IDE) flag a buried stack as it's
  typed, backed by a runtime `TypeError`; (2) the language-neutral form of the
  rule in the Rust core — `build_layers` rejects an `atomic` spec entry that
  isn't outermost (last). The core check is the piece that keeps the polyglot
  future uncornered: the rule is stated once in the shared core and documented
  in `docs/boundary-schema.md`, so a future JS/TS frontend inherits it (and adds
  its own branded-type ergonomics) instead of reimplementing it. Interesting
  wrinkle: Python needs *nominal* typing here (a structural `Protocol` wouldn't
  exclude `AtomicBuilder`, whose shape matches the others) — the same reason a TS
  frontend would need branded types rather than a plain interface.
  `tests/test_builder_ordering.py`, Rust unit tests in `brix-py`. A
  `build_stack(...)` factory came with it — it assembles the correct
  `AtomicBuilder(CacheBuilder(IncrementalBuilder(...)))` order and toggles
  layers by argument (`cache_dir=None` / `staging_dir=None`), so the common case
  never hand-nests; dogfooded in `examples/leveldb/brix_build.py`.
- **Deleted-source cleanup after a failed build.** brix cleans up a deleted
  source's leftover outputs by remembering, between builds, which collection
  members existed. That record was committed only on *success*, so a member
  *added by a build that ultimately failed* — its output already on disk — was
  forgotten; if its source then vanished, the output leaked from incremental
  cleanup (only a full `clean` reclaimed it). The leak is real only for unstaged
  builds: atomic mode wipes a failed build's staging before it reaches the
  workspace, so any output that reaches the workspace got there through a
  success that also committed the membership. The fix collapsed to one
  write-policy change rather than a new metadata structure: on a failed build,
  the membership record becomes the *union* of what the last build knew and what
  this build realized (never a replace). The union carries both invariants — an
  added-then-deleted member is remembered and therefore cleaned, and a member
  pending deletion is retained until a *successful* build settles it (the retry
  invariant, which the old success-gating provided and the union preserves
  because it only ever adds). Mirrored in both engines' incremental
  `on_build_end` (Python reference + Rust core; no schema change, so nothing to
  migrate), and it also closes the nested case the reaping code's own docstring
  had flagged as "the one unrecoverable case" — a subtree realized only by a
  failed build is now enumerable, because its inner membership was recorded by
  that failed build's union write. `tests/test_delete_cleanup.py` covers both
  engines: the unstaged leak, the retry invariant, an atomic-mode regression
  (already protected, unperturbed), and the nested video/scene case.
- **`env_used=` — environment as fingerprint material (phase 2: the guard).**
  Phase 1 records the environment; phase 2 makes a declared subset a real
  dependency. `env_used=["JAVA_HOME", …]` on an `ExternalProcessAction`
  snapshots those variables so a change re-stales the action — proven
  end-to-end on both engines (a tracked variable changing re-runs a step no
  file touched; unchanged replays). The design keeps it **off the Rust neutral
  path**: each named variable is read from `os.environ` on the Python side and
  materialized into a single fingerprint-only input (`_brix_env_used`, a
  `ValueData`) — the same `classpath_track` trick of an unreferenced input that
  is hashed but never rendered. Because both engines already fingerprint a
  `ValueData` input identically, only the resolved *value* crosses the boundary
  as ordinary data, Rust needs no change, and a golden fingerprint case
  (`env_used tracked variable`) pins the parity mechanically — the Rust golden
  test re-derives the identical digest from the stored spec. Load-bearing
  distinctions from `env=`: the snapshot is **never set** in the process
  environment (it tracks, it does not inject) and **never** substituted into
  the command; an **unset** variable is JSON `null`, distinct from set-to-empty;
  and it is copied **verbatim** through `substitute`, so an env value holding a
  `{binding}`-shaped substring survives expansion unrewritten. Resolved once at
  construction, which — since `graph_supplier` runs per build — is per build.
  Because it touches the fingerprint path, it is queued for a Fable review (the
  reserved role, as with `content_key`), with the scope spelled out in the
  roadmap. `src/brix/actions.py`, `tests/test_env_used.py`,
  `rust/golden/generate.py`, `docs/environment-recording.md`.
- **Recording the build environment (phase 1: the "works on my machine"
  detective).** brix fingerprints what a build *declares* and is blind to what
  a tool reads from the *inherited* environment without anyone declaring it —
  `JAVA_HOME`, `CC`, a locale — which can change output while brix, seeing no
  declared change, serves the result as fresh. A build now captures its
  filtered environment to `build/.brix/environment.json`, and `brix_build.py env`
  prints the last recorded build environment plus a diff against the current
  one (exit 1 when they differ, like `status`); a one-line note also folds into
  `status`. It is a *detective, not a guard*: it records and shows, and
  deliberately does **not** rebuild, fingerprint, or split a cache — so it
  cannot slow a build or cause a spurious rebuild. That framing keeps it pure
  diagnostic *state* (docs/content-key.md's state-vs-semantics rule), which is
  why it lives beside the metadata, is Python-owned at the CLI boundary (the
  Python process *is* the build's environment, on either engine), and needs no
  Rust/graph/wire change at all. The noise filter is a conservative
  ignore-list (completeness beats quiet for a diagnostic): it drops session
  volatiles (`PWD`, `SHLVL`, `TERM`, `SSH_*`, `XDG_SESSION*`, …) and keeps
  everything else, with `cli.main(..., env_ignore=[...])` extending it per
  project. Capture is per-build (the environment is uniform across a build) and
  runs even on failure (a failed build's environment is equally worth
  diffing). `clean` removes the record with the rest of the metadata directory
  on both engines. Deferred and bounded in the design note: phase 2 (`env_used=`
  promotes a declared subset to fingerprint material — the guard, crossing the
  byte-identical path) and phase 3 (auto-detect read variables via `getenv`
  tracing, folded in like depfile headers). `src/brix/environment.py`,
  `tests/test_environment.py`, `docs/environment-recording.md`.
- **The warm kotlinc worker.** A cold `kotlinc` pays JVM startup *and* reloads
  and re-JITs the entire Kotlin compiler on every call — a far heavier tax
  than `javac` — so keeping it warm is the Kotlin edit-loop win.
  `kotlin.kotlinc_worker(graph, kotlin_compiler=…)` mirrors `java.javac_worker`:
  it bootstrap-compiles a small worker and returns a `WorkerSpec` that
  `JarAction(kotlinc_worker=…)` routes the compile through. The worker
  (`src/brix/_java/KotlincWorker.java`) is plain Java that invokes the Kotlin
  compiler in-process by reflection into
  `org.jetbrains.kotlin.cli.jvm.K2JVMCompiler` — so it compiles with plain
  `javac` (no Kotlin classes on the compile classpath, exactly like
  `JavacWorker`); the compiler is supplied on the *launch* classpath as
  `lib/kotlin-compiler.jar`. `K2JVMCompiler.exec(PrintStream, String…)`
  returns an `ExitCode` without calling `System.exit`, which is what makes
  repeated in-process compilation safe; the worker also redirects
  `System.out`/`System.err` for the duration of each `exec` so stray compiler
  output can't corrupt the line protocol. The worker protocol gained
  `--plugin`/`--plugin-option` (path-resolved plugin jar → `-Xplugin=`, preset
  → `-P`) on top of the javac worker's `--sources`/`--classpath`/`--options`,
  and path-bearing args are absolutized against the request cwd so the worker
  stays stateless. Because the worker is pure transport, the classpath stays
  kotlin-abi-lensed identically to the spawn path — verified end-to-end: an
  inline-free implementation edit still skips the dependent, an inline-body
  edit still recompiles it, over the worker, under both engines. It rides the
  existing cross-build daemon (`brix-workerd`, crossing the Python↔Rust
  boundary), so the warm compiler survives *between* builds — measured on
  `examples/kotlin_multimodule` at ~0.3 s for the second edit-rebuild versus
  ~2.1 s cold. One JDK subtlety: the worker is built with `--release 17` to
  decouple the compile JDK from the (Kotlin-compatible) JDK that launches it.
  Known parity note, shared with the javac worker: the worker path does not
  fold the compiler tool identity into the compile fingerprint (spawn does via
  the `{kotlinc}` tool), so a compiler upgrade is not tracked on the worker
  path — a cross-cutting item for both workers, not specific to Kotlin.
  `tests/test_kotlin_worker.py`; `examples/kotlin_multimodule/brix_build.py`.
- **`brix_build.py help` — the build file's live configuration.** `-h` only ever
  listed the built-in subcommands; everything project-specific lived inside
  the `cli.main(...)` call and was invisible without opening the file. `help`
  introspects the builder chain — it walks each wrapper's `.inner` reading
  plain attributes (never instantiating a layer or touching disk) — and prints
  the active stack outermost-first with each layer's path (Atomic → Cache →
  Incremental), the target aliases, and the project's build-then-act
  `commands=`. Whether staging is wired shows up twice: a summary line and a
  per-command flag, so `commit`/`discard` are marked unavailable when no
  `AtomicBuilder` is present. Two small authoring enablers came with it: an
  optional description for an alias via `cli.Alias(target, description)` — a
  drop-in for a bare `str`/`list[str]` value, so existing build files are
  untouched — and each `commands=` callable's first docstring line as its
  one-line summary. It is a pure client printout (the engine never prints), so
  it is engine-agnostic — identical output under `BRIX_ENGINE=rust`.
  `tests/test_cli_help.py`; dogfooded in `examples/leveldb/brix_build.py`.
- **Fable review of the `content_key` relocation (`634c115`): verdict
  positive; one golden gap closed.** The reserved-Fable check on the
  fingerprint-byte-identical claim. Every load-bearing piece verified: the
  golden diff proves digests unchanged across the `identity`→`content_key`
  wire rename (the field *name* never enters fingerprint material — only the
  versioned lens tag does, so no cache split); byte hashing's 3-element
  material shape vs a lens's 4-element `[..., tag, digest]` shape cannot
  collide (length plus tag prefix); the `:raw` fallback stays inside the
  lens's tag namespace; digest caching keys on the versioned tag plus the
  same stat signature in both engines, with a missing file encoding
  identically as `[tag, Null]`; substitution reconstructs via `type(self)` /
  preserves the lens on the Rust side too; and the golden cross-check is
  non-circular (Python generates, Rust re-derives every entry). The one
  actionable finding was a same-day gap: no fingerprint golden pinned
  `kotlin-abi@1` tag parity across engines. Closed in `b58dd6a` — a
  `kotlin-abi jar` case built from committed fixture bytes
  (`rust/golden/tree/ktlib/`, fixed zip timestamps, own directory so the
  historic `lib/{n}.jar` glob case keeps its pinned digest) — along with a
  deliberate refresh of the cpp golden specs that had drifted after the
  `deps=` feature (golden.rs replays stored specs, so parity never actually
  broke). Non-blocking, miss-only notes for the record: a lens parse that
  fails is re-attempted each build (perf, not correctness); Python
  `identity.register()` silently replaces a builtin where Rust has no such
  hook; a version bump applied to one engine but not the other diverges them
  toward rebuilds, never staleness.
- **The `kotlin-abi` lens: cross-module Kotlin skipping, soundly.** Recovers
  the skip that byte-tracking the Kotlin classpath gave up (previous entry).
  Ground-truth experiments shaped it: an inline-body edit changes consumers'
  bytecode, and so does a *line shift alone* — three comment lines added above
  an `inline fun` changed the consumer's output, because inlined copies carry
  the declaration's line-number tables (SMAP) — while the byte changes stay
  confined to the class that declares the inline member. So the lens works per
  class: jvm-abi counting (already sound for `const val` via `ConstantValue`,
  and declaration-sensitive because it hashes `@Metadata`) unless the class's
  Kotlin metadata declares an inline function or inline property accessor —
  then the whole class counts by bytes. Classes nested under a flagged name
  (`$` prefix: the lambdas/anonymous objects kotlinc regenerates into
  consumers when inlining) and any metadata the parser will not vouch for
  (unknown kind or major version, legacy 8-to-7 string packing, pre-1.0
  `old_flags`) also count by bytes — **fail closed: wrong in the slow
  direction, never stale.** The metadata facts (BitEncoding UTF-8 mode; the
  varint-delimited string table before the `Class`/`Package` protobuf;
  `Function.flags = 9` with IS_INLINE at bit 10; `Property.getter_flags =
  7`/`setter_flags = 8` with IS_INLINE_ACCESSOR at bit 8) are pinned from the
  Kotlin compiler's own sources and verified against kotlinc 2.4 fixtures.
  Implemented twice — `src/brix/kotlinabi.py` (reference) and
  `rust/brix-core/src/lenses/kotlinabi.rs` — with 14 golden digests identical
  across engines (`tests/fixtures/kotlinabi/`, `tests/test_kotlinabi.py`,
  `rust/brix-core/tests/identity.rs`). `kotlin.JarAction`'s `classpath_track`
  flipped from the byte union to `kotlin.abi(...)` — zero user-API change.
  The end-to-end equivalence oracle (`tests/test_kotlin_multimodule.py`, both
  engines) pins all three directions: a normal-body edit in an inline-free
  class now **replays** the dependent and its output equals a clean rebuild
  byte-for-byte (the recovered skip, proven sound); an inline-body edit and a
  line-shift-only edit both recompile the dependent and match clean. A
  fourteenth fixture byte-patches the metadata version to a future major and
  must fail closed — the forward-compatibility contract: a new Kotlin release
  can only make builds slower, never stale.
- **Multi-module Kotlin composition, and a stale-result bug it caught.** A
  two-module Kotlin build (`app` depends on `core`) exposed that
  `kotlin.JarAction` was tracking its classpath through the `jvm-abi` lens —
  inherited from the `java.JarAction` code it mirrors. That lens is **unsound
  for Kotlin**: kotlinc copies an `inline fun`'s body into consumers' bytecode
  at compile time, so an ABI-lensed dependency let brix *skip recompiling a
  dependent after an inline-body change that had in fact changed its output.*
  Proven end-to-end: with the old lens, editing an inline body in `core` left
  `app` un-recompiled and stale; the ground-truth compiler diff confirmed
  `app`'s bytecode really changed. Fixed by byte-tracking the Kotlin classpath
  (`_runtime_union`, not `_classpath_union`): sound, recompiling on any
  dependency change, at the cost of the cross-module skip a future Kotlin ABI
  lens would recover. `tests/test_kotlin_multimodule.py` (kotlinc-gated) pins
  both the inline case (must recompile, must match a clean build) and the
  normal-body case (conservative recompile, still matches clean). The
  `examples/kotlin_multimodule/` teaching dogfood demonstrates it and, like the
  TypeScript example, uses the plain incremental stack because kotlinc
  self-resolves `-classpath` (the parked staged-overlay-visibility limitation).
- **Kotlin single-module builds (`brix.kotlin`)** — `kotlin.JarAction` compiles
  a whole Kotlin module with kotlinc and jars it, mirroring `java.JarAction`.
  kotlinc's `-classpath` takes one pathsep-joined token, so the resolved jars
  are baked into a value while the same jars ride as an ABI-lensed fingerprint
  input (unreferenced by the template but hashed and dependency-inferred, like
  `JUnitTestAction`'s run step). `spring_allopen=` reproduces Gradle's
  `kotlin("plugin.spring")` by wiring the all-open compiler plugin with its
  `spring` preset — a correctness feature, since Kotlin classes are `final` by
  default and Spring cannot subclass-proxy them otherwise (it compiles clean,
  then fails to boot). Proven on the new `spring-petclinic-kotlin` dogfood:
  `java.gradle(...)` adopts the KTS build's `compileClasspath`; brix compiles
  the 24 sources with all-open (non-`final` `CacheConfig` confirmed in the
  bytecode, vs `final` without the plugin); a no-op rebuild does nothing and a
  source edit recompiles the whole module while the content-aware jar step skips
  re-jarring on byte-identical output; and the module boots a full Spring
  context (Tomcat, JPA/Hibernate on H2, Spring Data) against the runtime
  classpath. This module's own outputs are byte-lensed, not `jvm-abi`-lensed:
  a Kotlin `inline fun` copies its body into cross-module call sites, so the
  lens is unsound for a *consumed* Kotlin module — multi-module Kotlin ABI
  skipping is deferred until a soundness-checked lens exists (see roadmap).
- **Multi-module Jackson selection on real production sources** — the Jackson
  dogfood now compiles all three 2.19.4 core modules: 46 annotations sources,
  135 core sources, and 480 databind sources. Three focused JUnit suites expose
  the important dependency shape without importing databind's unrelated test
  ecosystem. `verify_selection.py` makes a reversible private-method edit in
  `JsonFactory` and proves the exact result: core recompiles and its jar changes;
  core and databind suites rerun; databind, both affected test compiles, and the
  entire annotations lane replay. This needed no engine changes—the existing
  ABI compile lens and byte-level runtime lens compose correctly across modules.
- **Annotation processors are tracked as build-time code** —
  `java.JarAction(processorpath=...)` now sends a separate `-processorpath`
  through the warm javac worker. Processor jars use byte fingerprints, not the
  `jvm-abi` compile-classpath lens, because an implementation-only processor
  change can generate different output. A real-JDK integration test changes
  only processor behavior and proves the application recompiles and produces
  the new generated result. `examples/annotation_processing/` is a
  self-contained processor → generated source → executable jar dogfood.

- **Self-contained native wheels.** Maturin is now the PEP 517 backend for the
  mixed Python/Rust package: a Brix wheel contains the Python API, the
  `brix.brix_py` native engine, both Java helper sources, and an installed
  `brix-workerd` launcher. The daemon implementation is shared as the
  `brix-workerd` Rust library and compiled into the extension, so releases do
  not have to smuggle a second platform binary into wheel data. Runtime daemon
  discovery checks the active Python environment's scripts directory and does
  not depend on shell activation. `scripts/wheel_smoke.py` builds a release
  wheel, installs it without dependencies into a clean virtual environment
  outside the checkout, runs a real Rust-engine build, verifies package data,
  and reaches the daemon entry point. Full Python, Rust, HTTP-cache, and daemon
  suites pass.

- **Pinned the public Python v1 API.** The top-level `brix` package now exposes
  build authoring plus intentional extension seams, not scheduler and storage
  implementation details. Execution nodes/plans, node state tables, concrete
  layers, metadata internals, delete actions, data factories, and raw built-in
  lens modules remain available from their defining modules for Brix itself but
  are no longer advertised as authoring API. `docs/python-api.md` documents the
  boundary and an exact `__all__` contract test prevents accidental leakage.

- **Explicit ordering without a shared file (`Graph.force_order`).** A graph
  can now say `graph.force_order(setup, tests)` when two actions must be ordered
  but exchange no data. The default `on-success` trigger skips the later action
  after failure; `trigger=EdgeTrigger.ON_COMPLETION` runs it after either
  outcome. The
  relationship appears in plans as `explicit ordering`, and target selection
  pulls in the required predecessor. Both actions must already belong to the
  graph. The optional index-based wire shape is implemented in the Python
  planner and Rust core, with default-engine and targeted-build tests.

- **Reject conflicting output ownership.** Planning now fails before anything
  runs when two selected actions' declared output sets can name the same path,
  instead of letting parallel execution choose the last writer. The diagnostic
  names both actions and the overlapping path expressions. Validation uses the
  same conservative pattern relation and same-collection binding refinement as
  dependency inference, catches exact regex duplicates, and runs after target
  selection so make-style shortest-stem narrowing still works. Implemented in
  both the Python reference planner and the Rust core with mirrored tests.

- **TypeScript build (`brix.ts`) — the ts-dts lens gets its consumer.** The other
  half of the Java+TS stack, and the first real user of the `ts-dts` content lens
  (previously implemented in both engines but with nothing driving it).
  `ts.CompileAction(sources=, out_dir=, options=, dependencies=)` runs one `tsc`
  spawn per module with **brix owning incrementality** (not `tsc --build`): it
  declares the sources as inputs and the emitted `.js`/`.d.ts` as recursive
  `{name}` globs, so a module re-runs only when a source or a tracked dependency
  changes. Cross-module deps are the payoff: a dependent declares an upstream
  module's built `.d.ts` via `dependencies=`, tracked through **ts-dts**
  (`ts.DtsFile`/`ts.dts`, the surface `brix.ts` owns like `brix.java` owns
  `AbiFile`). Because `tsc`'s declaration emit is API-only, an implementation-only
  change upstream re-emits a byte-identical `.d.ts` and the dependent **replays** —
  the TypeScript twin of the jvm-abi shortcut. **Proven:** a live two-module `tsc`
  build (`examples/typescript/`, and a tsc-gated equivalence test) shows an
  impl-only edit skip the dependent with byte-identical output vs. a clean build,
  and an exported-signature change cascade. One design limit surfaced and
  documented: `tsc` self-resolves `import` paths off the real filesystem, so
  cross-module builds want outputs *in place* (the plain incremental stack), not
  behind an atomic overlay brix can't redirect a foreign resolver through. Still
  open (TODO): automatic *import-graph* tracking (the C-header analog) and an
  npm/`node_modules` resolver. `tests/test_ts.py` (8) + `tests/test_ts_equivalence.py`
  (2, tsc-gated via `BRIX_TSC`).
- **Gradle dependency resolution (`java.gradle`) — adopt a Gradle build.** The
  Gradle counterpart of `java.pom`, on the critical path for the real ~50-service
  Gradle monorepo: `java.gradle(project_dir, project=":svc", configuration=...)`
  resolves a configuration's classpath (`compileClasspath` default /
  `runtimeClasspath` / `testRuntimeClasspath` — the Gradle analog of a Maven
  scope) into a `DepSet`. Naming the *engine* is load-bearing: Gradle picks the
  highest requested version on a conflict where Maven picks the nearest, so the
  same coordinates can resolve differently. Two Gradle-specific mechanics: it has
  no `dependency:build-classpath` goal, so `java.gradle` injects a temporary
  **init script** (`--init-script`, never touching the project's own build files;
  config baked in, not passed as `-P` — junit5's `build-parameters` plugin
  *rejects* unknown project properties); and Gradle spreads declarations across
  files, so the cache key hashes all of them (build scripts by glob — junit5 uses
  `<module>.gradle.kts`, not `build.gradle.kts` — plus settings, properties, and a
  `libs.versions.toml` catalog). Returns only **external** jars; a sibling
  project-dependency resolves to a class *dir* and is dropped (right for a
  monorepo — those modules are brix graph nodes, not adopted jars). **Proven:**
  drove junit5's `./gradlew` end-to-end to resolve real subproject classpaths
  (apiguardian, opentest4j), with the second call served from cache (no Gradle
  re-run). `tests/test_deps.py` grew 8 Gradle cases (25 total). `docs/dependencies.md`.
- **C++ dependency resolution (`cpp.pkg_config`) — the `DepSet` generalizes.**
  Same pattern as the JVM adapters, one axis over: `cpp.pkg_config(["zlib"])`
  queries the system's pkg-config (behind a testable seam) and returns a
  `DepSet` whose `--cflags` are `compile_flags` (`-I`/`-D`) and `--libs` are
  `link_flags` (`-L`/`-l`) — so `DepSet.flags` split into compile/link, both
  empty for a JVM classpath. `cpp.CompileSourceAction(deps=...)` adds the compile
  flags to every TU; `cpp.LinkApplicationAction(deps=...)` adds the link flags
  *after* the objects (link order is load-bearing) plus any tracked artifact
  files. System libs come as `-l` flags, not file paths, so they aren't
  fingerprinted (documented; a future Conan/vcpkg adapter yielding concrete
  `.a`/`.so` paths would populate `artifacts`, which the link step tracks).
  **Proven:** brix compiled + linked a real program against zlib via
  `cpp.pkg_config(["zlib"])` and it ran. `tests/test_deps.py` grew C++ cases (17
  total), including a regression that a flags-only `DepSet` is truthy.
- **Dependency resolution: drive a resolver, consume its output
  (`brix.deps.DepSet` + `java.pom` / `java.maven`).** brix still doesn't resolve
  or download dependencies — but you no longer hand-maintain a classpath file.
  `java.pom("pom.xml")` *adopts* an existing Maven manifest (the migration path —
  keep your declarations where they are); `java.maven(["group:artifact:version"])`
  *resolves inline coordinates* (the greenfield / single-file path). Both drive
  Maven (behind a testable seam; `mvn=`/`cwd=` support a `./mvnw` wrapper),
  memoize the classpath under `.brix-deps` (re-resolves only when the pom /
  coordinates change), and return a **`DepSet`** — a lens-neutral, ordered set of
  resolved artifacts that composes with `+` (ordered merge) and `-` (fnmatch
  exclude) and feeds `JarAction` / `JUnitTestAction` / `SpringBootJar`. Naming the
  engine on the coordinate case is deliberate (Maven vs Gradle resolve versions
  differently). Design + the C++/Gradle generalization (`cpp.conan`,
  `java.gradle`, …) in `docs/dependencies.md`. **Proven:** the spring-petclinic
  example now resolves via `java.pom("pom.xml")` instead of a committed classpath
  file — same ~90/106 jars (byte-identical fingerprints, so the build replays),
  and a second build is a 0.3s cache hit. `tests/test_deps.py` (13 tests).
- **Scaling benchmark + first per-build-overhead numbers
  (`examples/scaling_benchmark.py`).** A synthetic N-node graph (one `cp` per
  source, glob-fanned) whose no-op build does zero process work, so its wall time
  is pure brix overhead — realize globs, expand, stat/hash every input. Findings
  (one 4-core machine, in-process `build()`, **release Rust engine**):
  - **No-op scaling is linear** in node count on both engines — no O(N²) blowup in
    glob realization / expansion / freshness check. Rust **~265 µs/node**, Python
    ~2.5 ms/node — **Rust ~10× faster**. Extrapolated to ~10–15k nodes (≈50
    services): a cold no-op of **~3–4s**, plus a ~0.26s cold-CLI tax. Mild at
    scale; a resident daemon would erase it but isn't urgent.
  - **Clean builds:** the release Rust engine matches or beats the Python engine
    (e.g. 2.6s vs 3.7s at 500 nodes; 4.8s vs 8.5s at 1000). Rust wins the
    no-op/incremental loop decisively.
  - **Methodology correction, kept as a caution:** a first pass reported the Rust
    engine ~5× *slower* on clean builds and a ~1.2 ms/node no-op floor. Both were
    a **debug-`.so` artifact** — unoptimized Rust runs this hashing/JSON-heavy
    per-node work ~4–6× slower. The engine loader prefers `target/release`, so a
    debug-only build is measured by accident; the benchmark now warns to build
    `--release` first. The differential experiment (touch vs cp, `jobs=1`) that
    ruled out I/O / metadata / reporter is what exposed the flat 30 ms/node as
    suspicious enough to check the build profile.
- **Incremental-≡-clean equivalence oracles.** The suite tested each
  invalidation path against a hand-written expected value; it had no test of the
  property those are all really about — that no edit sequence ever leaves an
  incremental build diverging from a from-scratch one (a *missed edge* shipping a
  stale output, the dangerous failure of any incremental engine). Added real
  oracles: drive a graph through a sequence of edits incrementally, rebuild the
  *final* state clean in a fresh workspace, and assert the outputs are
  byte-identical (contents *and* file set, so stale leftovers are caught).
  Verified non-vacuous — the byte-lens oracle fails on a planted divergence and a
  planted stale file.
  - **Byte-lens** (`test_incremental_equivalence.py`): a glob-fanned compile +
    fan-in combine + a **generated source** (a step whose output is a compile
    input). Coverage is **bounded-exhaustive, not random** — every single edit
    and every ordered pair of edits (source add/edit/delete, tool change, shared
    fan-in value, generator spec) is checked, so one run gives full confidence
    within the bound; curated deeper sequences reach the longer interleavings.
  - **ABI-lens** (`test_incremental_equivalence_abi.py`, JDK-gated): the case
    where incremental deliberately does *less* than clean. Asserts the shortcut
    is both **taken** (an impl-only edit to a library skips recompiling its
    dependent) and **sound** (the skipped dependent's classes still equal a clean
    build), and that an API edit **cascades**.
  - **Depfile** (`test_incremental_equivalence_depfile.py`, `cc`-gated):
    discovered C header deps — editing a header recompiles exactly the unit that
    includes it (the other replays) and matches a clean build.

  Dependency-free (no hypothesis; the suite stays hermetic). The pointwise cases
  run under both engines; the large/toolchain-heavy sweeps run on the default
  (Rust) engine, with goldens pinning that the Python engine agrees.
- **Removed the duplicated content-lens built-in list (layering cleanup).** The
  set of core-native content keys (`jvm-abi`, `ts-dts`) was hand-maintained in
  three places — the Rust `content_key.rs` registry, `identity.py`'s registered
  classes, and a `_RUST_IDENTITIES` frozenset in the generic `lowering.py`,
  synced only by a comment. The Python duplicate is gone: `identity.builtins()`
  now *derives* the set from the import-time `register(...)` calls (one source of
  truth — add a built-in and it is picked up automatically), and the boundary
  (`custom_identity_names`) queries it instead of a parallel list. Pinned by a
  test that a custom plugin registered later never joins the built-in snapshot.
- **Made the Rust neutral core name zero languages.** `content_key.rs` used to
  seed its registry with two literal `insert("jvm-abi", …)` / `insert("ts-dts",
  …)` lines — the neutral module naming the built-ins. Now the `lenses` module
  owns the roster (`lenses::ALL`) and each lens owns its own name (a `name()`
  method on the `ContentKey` trait, the un-versioned prefix of `tag()`);
  `content_key.rs` builds its registry by iterating `lenses::ALL` and keying on
  `lens.name()`, so the neutral path names no language and adding a built-in is a
  one-line edit in `lenses`, nothing in the core. Behavior-preserving: all 134
  Rust tests and 237 Python tests (both engines) pass. (Dependency-free — no
  `linkme`/`inventory` link-time-registration machinery for a compiled-in set of
  two; third-party lenses arrive from the skin as `CustomIdentities` callbacks,
  not by patching the core.) The one remaining item is a deliberate won't-fix:
  `identity.py` still bundles the registry/Protocol with the reference-engine
  lens classes, but that whole module dies in phase-4c, so splitting it is moot.
- **Spring Boot executable-jar packaging (`brix.springboot`) + spring-petclinic
  dogfood.** New `SpringBootJar` action repackages a plain application jar plus
  its Maven-resolved runtime jars into one executable fat jar (nested
  `BOOT-INF/lib`, `JarLauncher` manifest) that boots with bare `java -jar`. It
  does *not* reimplement the layout: a dependency-free JDK helper
  (`_java/BootRepackager.java`) loads Spring's own `spring-boot-loader-tools`
  `Repackager` reflectively through a child class loader and drives it — the same
  code the Maven/Gradle plugins call, so the output is a standard Boot jar. Every
  jar is passed as a data-substituted argument (never a baked `-classpath`), so
  the command template stays free of machine paths and its fingerprint is
  portable. The plain jar and all bundled deps are byte-lens inputs, so a
  dependency bump rebuilds the executable jar. **Proven end to end** on
  spring-petclinic (Spring Boot 4.1, JPA/Thymeleaf/WebMVC): brix builds the fat
  jar and the app boots and serves (`/actuator/health` UP, `/vets` JSON, home
  page); a source edit cascades recompile → re-jar → re-bootjar; Rust and Python
  engines replay identically. No engine changes — pure authoring-layer Python
  that lowers to generic process actions. See `examples/spring-petclinic/` and
  `tests/test_springboot.py`.
- **Retired `identity=`; comparison is now a `content_key` Rust trait** (per
  `docs/content-key.md`, both engines). The `identity="jvm-abi"` string kwarg is
  gone. A content lens is now *intrinsic to the atom's type*: `brix.java` ships
  typed `AbiFile`/`AbiGlob` subclasses (`content_key = "jvm-abi"`), and the author
  writes `java.abi(expr)` — never the name. In the Rust core the lens is a
  name-dispatched `ContentKey` trait with a registry; the built-in lenses
  (`jvm-abi`, `ts-dts`) moved *out* of brix-core's neutral fingerprint path into
  `brix-core::lenses`, so the detector dispatches through the trait and names no
  language. The `CustomIdentities` Python-callback escape hatch is kept in reserve
  (unused), which is why dispatch is a name registry, not an `enum`. Wire field
  `identity` → `content_key` (additive, schema stays v1). **The invariant held
  mechanically:** regenerating the golden fingerprints changed only the `spec`
  strings (`identity` → `content_key`) and left **every fingerprint digest
  byte-for-byte identical** — pure relocation, no cache split. Tests rewritten to
  the typed-atom surface (same assertions; unknown-lens-is-loud is now a bad
  subclass); full suite green under both engines.
- **Glob exclusion via the `-` operator** (per `docs/content-key.md`, both
  engines): `data.glob("{name}.cc") - "*_test.cc"` (or a list). fnmatch
  right-hand side (`*` crosses `/`, `?`, `[...]`) matched against the
  workspace-relative path; `__sub__` returns an immutable copy; `find()`
  filters while `match`/`substitute`/planner overlap ignore excludes
  (conservative — an exclude only shrinks a set). Additive wire field
  `glob.exclude` (schema stays v1); joins fingerprints/identities **only when
  non-empty**, so every pre-exclude graph hashes byte-identically (no cache
  split), and editing the exclude list itself re-runs. Mirrored tests both
  engines. `examples/leveldb` now globs the real tree in place with per-dir
  excludes (source set verified identical to upstream's `add_library()` list);
  its `setup.sh` no longer stages sources, and editing the real
  `db/skiplist.h` recompiles exactly the same 5 TUs as before.
- **Selective test running + jackson-core dogfood** (`brix.java.JUnitTestAction`,
  `examples/jackson/`): a test suite is just an action, so brix already
  replays one whose inputs are unchanged — the helper wires that up for
  JUnit 5 with the key move being *two lenses on the same classpath jars*:
  the test compile depends on them through `jvm-abi` (API), the test run
  through their **bytes** (implementation). So a suite re-runs only when
  something on its runtime classpath actually changed; an impl-only change to
  a dependency re-runs the tests (which an ABI lens would wrongly skip),
  while an unrelated change re-runs nothing. Passing suites cache like any
  result; failing suites re-run until inputs change. Proven end-to-end on
  jackson-core's real 232-source JUnit 5 + AssertJ suite: no-op replays, a
  main impl change re-runs, a real test change re-runs while the main jar
  replays, editing test data (`test_resource_dirs`) re-runs. The dogfood also
  drove two real-build features into `examples/jackson`: a **generated
  source** (PackageVersion from a template, via a tool action) and its one
  shaded compile dep (fastdoubleparser) as a plain classpath entry. Pure
  action composition — no engine change, both engines. Closes the biggest
  remaining Java gap (tests). At the time, the next gaps were annotation
  processors (now complete; see the newest entry) and multi-module cross-suite
  selection (add jackson-databind).
- **C++ dogfood — fmt built by brix, header tracking validated**
  (`examples/fmt/`): the C++ counterpart to the JUnit dogfood. Compiles
  fmt's classic library sources (`format.cc`, `os.cc`) plus a small app via
  `cpp.CompileSourceAction` + `cpp.LinkApplicationAction`, and validates
  automatic `#include` header-dep tracking on real headers: editing
  `include/fmt/os.h` recompiles only `os` (the sole TU including it), editing
  the widely-included `base.h` recompiles all three — learned from the
  compiler depfile, nothing declared. A comment-only header edit yielding a
  byte-identical object correctly lets the link *replay*. `brix.cpp`'s
  header-dep tracking (the feature the old handoff notes flagged as the
  gating unbuilt piece — since implemented) now proven on a famous project.
  Checkout `~/Projects/brix_test/fmt` @ 11.1.4; `fmt.cc` (C++20 module)
  excluded. Next C++ breadth: a larger multi-source project (e.g. leveldb).
- **C++ dogfood at scale — leveldb static library built by brix**
  (`examples/leveldb/`): the larger C++ target fmt pointed to. `setup.sh`
  originally staged the exact POSIX library sources (parsed from upstream
  `CMakeLists.txt`) because brix globs had no exclusion — the friction that
  motivated the `-` operator; the staging has since been replaced by in-place
  globs with excludes (see the glob-exclusion entry above). brix compiles all **38**
  translation units in parallel (`cpp.CompileSourceAction`), bundles them with
  `cpp.ArchiveAction` into `libleveldb.a` (`ar Dcrs`, 38 members), and links a
  smoke program (open a DB, batch-put, get, iterate) against the archive via
  `cpp.LinkApplicationAction` — run with `brix_build.py run smoke` (a build-then-act
  `commands=` subcommand). Header-dep tracking holds at scale: editing
  `db/skiplist.h` recompiled exactly the 5 TUs that transitively include it
  (33 skipped), and — being a comment-only edit — left the objects
  byte-identical so `ar` and the link correctly replayed. First exercise of
  `cpp.ArchiveAction` on a real project; validates archiving + parallel-compile
  scale + the header graph. Checkout `~/Projects/brix_test/leveldb` @ 1.23
  (`7ee830d`); no compression libs (snappy/zstd/crc32c off in the generated
  `port_config.h`).
- **Acceptance benchmark: brix vs Gradle on JUnit 5** (`examples/junit5/benchmark.py`):
  the `docs/java-fast-builds.md` scenarios on the seven platform modules,
  brix vs Gradle 8.14 (`:module:jar` ×7). Best-of-3, one machine: no-op
  ~0.09s vs ~1.1s, impl-only change ~0.37s vs ~1.8s (**~5–12× faster on the
  edit-compile loop**), API-change cascade ~1.3s vs ~1.6s (tie). The
  cross-build daemon earns its keep — impl-only is 0.37s warm vs 0.81s
  spawning javac cold — and even the cold-worker number beats Gradle's warm
  daemon, since brix carries no per-invocation daemon/config tax. Honest
  caveat: Gradle's `:jar` does more per module (multi-release, module-info,
  OSGi), so absolute times aren't a pure compile race — but the incremental
  overhead brix avoids is exactly the edit-compile-loop cost. Gradle needs a
  Java 21 toolchain (JDK 26 is the box default), provisioned locally under
  `~/Projects/brix_test/jdk`.
- **JUnit 5 dogfood — real Gradle project built by brix** (`examples/junit5/`):
  re-implements the compile graph of a famous Gradle project's seven pure-Java
  `junit-platform-*` modules (`commons → engine → launcher → suite-* / testkit`,
  depth-4), each a `JarAction` through one warm javac worker with `classpath=`
  wired by `identity="jvm-abi"`. brix has no dependency resolver by design, so
  `fetch-deps.sh` pulls the four external jars from Maven Central once. Proves
  the multi-module ABI story on code we didn't write: an impl-only edit in
  `commons` recompiles `commons` alone and the six dependents replay; an API
  edit cascades. **This dogfood immediately found a real bug** (next entry). The
  checkout lives in `~/Projects/brix_test/junit5` (kept out of the Projects
  root), pinned to `r5.14.4`. Scope excludes `jupiter-*` (Kotlin + codegen),
  `console`/`reporting` (extra jars, shadow/multi-release), `vintage` (JUnit 4).
  A real acceptance test for the eventual Java+TS monorepo; also the substrate
  for the Gradle benchmark (TODO track 1).
- **jvm-abi crash fix on String-valued annotation elements** (both engines):
  the `'s'` annotation element-value tag indexes a Utf8 constant directly but
  was routed through the loadable-constant path — Python leaked raw bytes then
  crashed in `json.dumps`, Rust errored — so every `@API(since="…")` class (i.e.
  nearly all of JUnit) silently fell back to `jvm-abi@1:raw` byte hashing,
  killing ABI avoidance. Fixed to hex-encode the Utf8 like the `c`/`e` tags. No
  version bump (only converts a former error into a real digest; every other
  digest is unchanged, so no stale hit is possible). Pinned by a new
  `strannotation` fixture asserted identically in `tests/test_content_identity.py`
  and `rust/brix-core/tests/identity.rs` (cross-engine parity). Found by the
  JUnit dogfood — exactly what it was for.
- Minimal prototype, spec steps 1–10 plus data-set dependency inference —
  with tests, `examples/hello_text/` and `examples/raw_cpp/` both building
  end-to-end.
- `ExternalProcessAction.name` made optional.
- Build-layers design converged and documented in
  `docs/build-layers.html` (middleware + events; append-only/monotone
  ground rules; insertion channel; NodeContext logical/physical mapping;
  edge trigger modes). Rewritten in plain English with class and data-flow
  diagrams; the PDF version was dropped in favor of browser viewing.
- `freshStart.md` (the original spec) merged into this README, updated to
  the current understanding, and deleted.
- **Layer seams** (`src/brix/layers.py`): `BuildLayer` hook interface and
  `NodeContext`; the executor wraps every ready process node in the
  `around_node` stack and fires `on_build_begin` /
  `on_collection_realized` / `on_build_end`; rendering resolves inputs
  against `ctx.workspace_root` and maps outputs through `ctx.output_root`.
  Node status/result moved out of `ExecutionNode` (now frozen) into the
  scheduler-owned, monotone `NodeStateTable`. `CleanBuilder` runs with an
  empty stack; behavior unchanged.
- **Trivial-layer proof** (`tests/test_layers.py`): recording, skip-all,
  output-redirect, and stack-order layers exercised end-to-end — including
  through runtime-expanded nodes. Runnable demo: `examples/layers_demo/`
  (trace layer + hand-rolled staging-and-commit layer).
- **IncrementalBuilder part 1 — fingerprints and skip**
  (`src/brix/metadata.py`, `incremental.py`): persistent JSON metadata with
  a stat-validated file-hash cache (`TimeAndHashChangeDetector`); action
  identity keys (structure) vs fingerprints (content — command, tool
  identity, input hashes; also the cache layer's key); fresh nodes complete with
  their previous result, stdout/stderr replay included; failed actions
  always rerun and their outputs are deleted (clean-build parity), with the
  failure recorded for status/errors. Tests: rerun
  only what changed, byte-identical rerun keeps downstream fresh, value
  changes refingerprint, failures never replay as fresh.
- **IncrementalBuilder part 2 — deleted-source cleanup**: membership
  history per collection (applied to metadata only on build success);
  vanished members produce `DeleteFilesAction` nodes proposed by the layer
  and inserted/wired by the scheduler (`NodeProposal` — the insertion
  channel is real code now), ordered before the collection's consumers.
  Edge trigger modes landed: `on-success` (default; cascades skips) vs
  `on-completion` (fires after success or failure — for failure-triggered
  cleanup). Deletions run through the layer stack like any node.
  Runnable demo: `examples/incremental_demo/` — four builds printing how
  many processes actually spawned (4 / 0 / 2 / 1).
- **Deleted-member cleanup now recurses through nested expansions**
  (`incremental.py` `_reap_vanished`): deleting a whole outer member (e.g. an
  entire video) cleans every output of its *nested* expansion (each scene),
  not just a top-level output — matching a clean build. The inner collection
  never realizes (its subtree is pruned), so cleanup reads the vanished
  member's recorded inner membership back from metadata; the reconstructed
  inner `membership_key` is deterministic, so it matches what the previous
  build stored. The read never mutates membership records, so a failed build
  still re-detects via the (deferred) outer membership. Closes the
  "vanished nested expansions are skipped" gap the rust-port doc flagged;
  regression test `test_deleted_video_removes_nested_stale_outputs`.
- **"Never make a stale hit" invalidation-completeness tests**
  (`tests/test_incremental.py`): executable spec for the fingerprint
  invalidations a monorepo depends on and a Rust port must reproduce —
  glob-membership *growth* re-stales the consumer (mutation-verified: dropping
  member tracking makes it fail), a shared file input (build script/tool)
  change invalidates every action that used it *while* byte-identical outputs
  keep the downstream fresh (content-based, not mtime), and the command
  template is fingerprint material. Pins the production-safety property (a
  missed invalidation ships stale artifacts) ahead of phase 3.
- **Reporter seam + result provenance** (`src/brix/report.py`): the
  scheduler emits events (`build_begin`, `node_started`, `node_completed`,
  `node_skipped`, `collection_realized`, `node_inserted`, `build_end`) to a
  `Reporter` — the observational twin of `BuildLayer` — and never prints.
  Builders are silent by default (exit-code-only clients just read
  `result.success`); `ConsoleReporter` restores the live stream and marks
  non-process results (`ok: upper a (replayed)`), or with
  `show_replays=False` prints only real work (replayed failures still
  print); `ErrorsAtEndReporter` prints failures only at completion.
- **Status reporting + CLI** (`src/brix/status.py`, `cli.py`):
  `compute_status()` asks the incremental layer's freshness question
  hypothetically — nothing executes — and reports per action: fresh /
  never built / inputs changed / outputs missing / failed last build
  (with the recorded result) / outdated (upstream will rebuild —
  staleness propagates along plan edges), plus pending deletions from
  membership diffs. `brix.cli.main(create_graph)` is the project CLI:
  `build [-j N] [--silent | --quiet-replays]`, `status` (exit 0 = clean),
  `errors` (replays recorded failure diagnostics without rebuilding),
  `clean`, `graph`. Failed results are now recorded in metadata (never
  treated as fresh). Runnable demo: `examples/cli_demo/`.
- **AtomicBuilder** (`src/brix/atomic.py`): staging under
  `build/staging/add|del` with a build-scoped context redirection; the
  `NodeContext` **overlay** (`read_path`/`find`/`iter_files`) makes
  rendering, collection realization, and fingerprinting all see the
  workspace as it will look after commit — staged outputs shadow committed
  files, staged deletion markers hide them. Deletions stage as markers and
  apply at commit. Commit is journaled (COMMIT marker written first, copy
  is idempotent): a crash mid-commit is finished by the next build's
  `on_build_begin`; failed-build staging stays inspectable
  (`brix_build.py status` shows staged files/deletions and interrupted commits)
  and is discarded when the next build starts. Composes with
  IncrementalBuilder: unchanged rebuilds spawn nothing and stage nothing;
  a failed build leaves the workspace byte-identical — even the successful
  half of it. Builders gained `layers()` for wrapper composition.
- **`brix.cpp` typed actions + lifelike example**: `CompileSourceAction`
  (lowers to ExpandAll over sources; source/object patterns must share one
  binding) and `LinkApplicationAction`; `Graph.add` lowers anything with a
  `lower()` method, so typed and raw actions compose in one graph.
  `cli.main` accepts `builder_factory` for a full custom stack.
  `examples/cpp_app/`: real C++ compiled with
  `AtomicBuilder(IncrementalBuilder(...))` and the CLI — break a source
  and the old binary keeps working (atomic), `brix_build.py errors` replays the
  compiler diagnostics without rebuilding, fix it back and even the link
  stays fresh (byte-identical object).
- **`brix.java` + the composability example**: `JarAction` lowers to one
  whole-program javac step plus a jar step (optional `entry_point` →
  `jar cfe`). Established the **output-directory convention**: a dir a
  tool writes into is declared as a `data.file` *output* (renders through
  `output_root`, so it stages under atomic — this closed a real atomicity
  hole where `data.value` out-dirs wrote straight to the workspace) and
  as a `data.file` *input* on the consumer (resolves via the overlay).
  `examples/cpp_to_java/`: compile+link a C++ generator, run it to emit
  Java sources, jar handwritten + generated together — one graph, three
  action kinds, all layers on. Verified on this machine up to the JDK
  boundary (no JDK installed): the generated `.java` files land in
  staging, javac fails cleanly, the workspace stays untouched, and
  status lists every staged artifact. `ProcessResult.source` records how a
  result was obtained (`process` / `replayed` / `cache`) — stamped
  by the producer, ignored by scheduling.

- **Expandable interface + nested ExpandAll, env=, regex substitution**
  (driven by the first real adoption target: Duane's `~/Projects/animation`
  makefile — videos containing numbered scenes, `--lowRes`-style flags,
  `PYTHONPATH`). `ExpandAll.child` is any `Expandable` (`substitute` +
  `label`); a nested ExpandAll becomes an EXPAND node expanded in turn.
  Project CLI flags need no brix feature: `parse_known_args()` in the
  project's brix_build.py, rest to `cli.main(argv=...)`, flags become
  fingerprinted `data.value` inputs. Still to come from that project:
  pluggable change detectors, a post-run `DependencyScanner` hook
  (gcc -MD style discovered dependencies; the scenes reference audio
  files), and workspace-scan ignore rules (regex collections rglob venv/;
  no-op animation rebuild spends ~15-20s fingerprinting).
- **Target selection** (`src/brix/targets.py`): `select_actions` matches
  target paths against output patterns, extracts bindings
  (`output/videos/wealth.mpg` vs `{video}.mpg` → `video=wealth`), and
  substitutes them backward through the graph — an ExpandAll whose own
  element gets bound *collapses to its substituted child*. Ambiguity is
  resolved by make's shortest-stem rule (without it, `{video}.mpg`
  matching `wealth.raw.mpg` as `video=wealth.raw` manufactures an endless
  `.raw.raw...` chain — found live on the animation project). CLI: `build`
  and `status` accept targets and `aliases=`; unknown targets exit 2.
  Verified on the animation project: `./brix_build.py build wealth` plans only
  that video's 12-node chain and replays against full-build records.
- **Liveness reporting + custom subcommands** (`report.py`, `executor.py`,
  `cli.py`): new `Reporter.process_started` event emitted at the innermost
  point of the layer stack — only when a process actually spawns, so
  replays never fire it and `--quiet-replays` stays quiet;
  `ConsoleReporter` prints `run: <label>` so long renders visibly start.
  `cli.main(commands={"view": fn})` adds build-then-act subcommands: build
  the named targets (aliases apply), then call `fn` with the built
  absolute paths (exit code honored; requires at least one target). The
  animation `view` command (makefile `%.mpg.view`, plays via parole) is
  three lines in its brix_build.py.
- **CacheBuilder** (`src/brix/cache.py`): `CacheStore` is content-
  addressed under one directory (`<fp[:2]>/<fp>/entry.json` + numbered
  output blobs; temp-dir-then-rename writes make concurrent sharing
  safe). `CacheLayer` sits *innermost* — incremental-fresh nodes never
  reach it; on a stale node it computes the shared `action_fingerprint`
  (reusing the incremental layer's detector so nothing hashes twice),
  restores hits through `ctx.output_root` (atomic staging routes them),
  stamps `source="cache"`, and populates the store after real runs.
  Failures with a real exit code are cached and replayed (same inputs,
  same failure); signal-killed runs are not. Workspace scans now ignore
  any `.brix*`-prefixed directory, and `clean` leaves the cache intact —
  clean-then-rebuild is the payoff case. Recommended stack:
  `AtomicBuilder(CacheBuilder(IncrementalBuilder(...)))`. Wired into the
  animation project (`cache_dir="output/.brix-cache"`; note `make clean`
  rm -rf's output/ and takes the cache with it — brix clean doesn't).
- **Held staging / review-before-commit** (`atomic.py`, `builders.py`,
  `status.py`, `cli.py`): `build --stage` / `AtomicBuilder.build(hold=True)`
  writes a HELD marker instead of committing; `commit` (CLI or
  `AtomicBuilder.commit()`) runs the journaled commit, `discard` drops
  staging. Held staging survives `on_build_begin` and is built on top of
  (the overlay reads it as if committed; status judges freshness the
  same way). The HELD marker is *consumed* when a new build starts on
  top — the new build's outcome decides re-held / committed / debris —
  so a failed build's mixed tree can never commit (`commit` refuses
  anything without a HELD or COMMIT marker; `discard` refuses an
  interrupted commit, which must finish). Use case: review overnight
  renders before they replace good copies.
- **Class diagrams** (`docs/class-diagrams.md`): Mermaid `classDiagram`
  source plus rendered SVGs for all 48 classes in `src/brix/`, grouped
  into five diagrams (data model, actions, execution plan, builders &
  layers, support). Rendered with `@mermaid-js/mermaid-cli`; verified by
  rendering straight to PNG with `mmdc` (not `rsvg-convert`, which drops
  Mermaid's `foreignObject`-based text silently — see the gotcha noted at
  the bottom of the doc). Headless Vivaldi hung on this machine during
  verification; mmdc's own bundled Chromium was used instead.
- **Pluggable cache store** (`cache.py`, `builders.py`): `CacheBuilder(inner,
  store=...)` takes any object with the `CacheStoreProtocol` three methods
  (`load` / `restore` / `store`) as an alternative to `cache_dir` (passing
  both is a `ValueError`). Degradation now lives in `CacheLayer`: any store
  exception — unreachable remote, permission error, malformed entry — is a
  cache miss or a skipped populate, never a failed build, so a leaf store
  may let its I/O errors propagate. `TwoTierCacheStore(front, back)` puts a
  fast local store in front of a slow shared one: reads check the front
  first (a local hit costs no round-trip), a back hit is served *and
  promoted* into the front, and writes populate the front with best-effort
  back so a down remote never blocks local caching. A shared filesystem path
  already works today via an absolute `cache_dir` or `CacheStore`. Runnable
  demo: `examples/cache_store_demo/` — simulates four builds across two
  machines (seed team cache, back-hit + promote, warm-front no round-trip,
  team cache down still succeeds).
- **Remote HTTP cache backend** (`cache_http.py`): `HttpCacheStore(base_url,
  auth=..., retries=, timeout=)` — a `CacheStoreProtocol` leaf store over plain
  GET/PUT, the shared-cache win for a monorepo (CI populates it, every dev
  restores instantly). Layout **mirrors the local `CacheStore`**
  (`<fp[:2]>/<fp>/entry.json` + `files/<n>`), so local and remote are the same
  content-addressed shape; content-addressed writes are idempotent, blobs go up
  before the entry (a partial upload reads back as a miss), and reads degrade to
  a miss on any transport error (all resilience stays in `CacheLayer`).
  Transport is generic stdlib `urllib` (no new dependency) so any GET/PUT server
  works — Nexus, Artifactory, an S3 gateway, a plain file server — with a
  pluggable auth-header hook. The one field the remote carries that local
  doesn't is `file_modes`: HTTP bytes lose the executable bit the local blob
  keeps for free, so modes ride in the entry and are re-applied on restore.
  Tested against a real in-process HTTP server: caching across workspaces, the
  intended local-front/remote-back two-tier with promotion, executable-mode
  round-trip, and unreachable-endpoint degradation.
- **JVM fast builds, phase 1 — content-identity / ABI fingerprints**
  (`identity.py`, `jvmabi.py`, `data.py`, `incremental.py`, `metadata.py`):
  `data.file(path, identity="jvm-abi")` makes a file contribute an ABI
  digest to fingerprints instead of its byte hash, so a dependent
  recompiles on a dependency's *API* change but not an implementation-only
  one (compile avoidance). `ContentIdentity` is a registered plugin
  (`name` / `version` / `digest(path)`); the versioned tag `jvm-abi@1` is
  part of the fingerprint payload, so bumping a plugin cleanly invalidates
  everything it fingerprinted. `jvmabi.py` is a pure-Python class-file/jar
  parser (no JVM needed) that over-approximates the ABI — class/member
  signatures, `static final` constant values, all annotations (runtime-
  visible and CLASS-retention), throws clauses, nest/sealed structure;
  excludes method bodies, private members, debug info, and bytecode
  version — and is deterministic (constant-pool references resolved, no
  text-encoding or float-formatting hazard) so the eventual Rust `jvm-abi`
  can reproduce it against the checked-in golden fixtures. A file the
  plugin can't interpret falls back to a byte hash under a `:raw` tag
  (a cross-machine disagreement can only miss, never make a stale hit);
  an unknown identity name is a loud error. Per-identity digests cache in
  the metadata alongside the byte hash, validated by the same stat
  signature. Fixtures + golden digests under `tests/fixtures/jvmabi/`
  (regenerate with its `regenerate.sh`). Phases 2–3 (workers, daemon)
  remain; `brix.java` is untouched until phase 2 wires `classpath=`.
- **TypeScript `.d.ts` content identity** (`tsdts.py`, `identity.py`): the
  second content lens, `ts-dts` — a `.d.ts` contributes its *declaration
  surface* to fingerprints instead of its bytes, so a consumer recompiles on an
  interface change but not an implementation-only change or a doc/format edit.
  (Since the `content_key` relocation it's selected via a typed atom carrying
  `content_key = "ts-dts"`, not a kwarg; the `brix.ts` authoring surface for it —
  `ts.CompileAction` + `ts.DtsFile`/`ts.dts` — is now built, see Completed.) The
  ABI-pruning
  multiplier for the Java+TS monorepo half, and proof the content-lens seam is a
  real extension point (a second lens, no engine change). Most of the win is
  structural — `tsc`'s declaration emit is already implementation-free, so an
  impl-only change yields a byte-identical `.d.ts` — with `tsdts.normalize`
  adding robustness: strip `//` and `/* */` comments and collapse formatting
  whitespace, but preserve whitespace *inside* string/template-literal types
  (`"a  b"` ≠ `"a b"`) and never reorder declarations, so it can only
  over-approximate a change, never miss one. Non-text files fall back to
  `:raw` like `jvm-abi`. Unit + engine-wiring tests in
  `tests/test_ts_dts_identity.py`.
- **Automatic C/C++ header (`#include`) dependency tracking**
  (`cpp.py`, `actions.py`, `incremental.py`, `metadata.py`): editing a header
  now rebuilds exactly the translation units that include it — the classic make
  `.d`-file gap, and Duane's original incremental pain, closed the correct way.
  `cpp.CompileSourceAction` compiles with `-MMD -MF {depfile}` (user headers,
  not system); the incremental layer scans the emitted depfile after a run
  (`scan_depfile`) and records the reported headers per action; `action_fingerprint`
  folds those recorded deps in (content-hashed like any input) so a header edit
  re-stales the compile. This is the design's reserved **`DependencyScanner`**
  seam, now implemented on the Python side, and the general mechanism (an
  `ExternalProcessAction.depfile` marker + `BuildMetadata` deps store) works for
  any depfile-emitting tool, not just C/C++. Because both layers share one
  detector (which carries the metadata), the discovered deps reach the *cache*
  key too, so a shared-cache hit can't serve a `.o` compiled against a stale
  header. After a run the fingerprint is recomputed with the deps folded in, so
  the next unchanged build is fresh (no spurious extra rebuild), and the scan
  also runs after a cache hit so a cache-fed machine learns the headers.
  Real-compiler test (`test_header_change_rebuilds_only_dependent_object`) +
  depfile-parser unit test. Narrow documented edge: a *fresh* machine's very
  first build uses a header-blind key until it has built once — inherent to
  depfile-based tracking under a content-addressed cache (Bazel avoids it only
  by requiring declared headers).
- **JVM fast builds, phase 2 — persistent workers + javac worker**
  (`workers.py`, `executor.py`, `actions.py`, `planner.py`, `java.py`,
  `src/brix/_java/JavacWorker.java`): a worker is a long-lived process that
  executes many actions over a JSON-lines protocol, amortizing startup
  (a cold JVM is the motivating cost). `worker=` on `ExternalProcessAction`
  routes the rendered argv to a `WorkerSpec`'s pool instead of spawning —
  indistinguishable in results, provenance (`source="process"`),
  `process_started`, caching, and staging (overlay output-redirection is
  preserved). The scheduler owns worker lifecycles (lazy start, all
  terminated at build end); a crash or startup failure fails the request
  with `exit_code=None` (never cached, worker discarded) with **no** silent
  fallback to spawning. The worker's identity joins the action fingerprint,
  so a JDK/wrapper change invalidates worker-built actions; the planner
  treats a worker's inputs (e.g. a bootstrap-built worker jar) as
  dependency edges. `java.javac_worker(graph)` adds an idempotent bootstrap
  (a plain spawned javac+jar of the shipped `JavacWorker.java`, itself a
  cacheable build product) and returns the spec; `JarAction` grows
  `classpath=` (entries default to `identity="jvm-abi"`) and `javac_worker=`
  to route its compile step through the warm in-process `JavaCompiler`.
  Multi-module payoff proven end-to-end (`tests/test_java_worker.py`): a
  dependency's implementation-only change does not recompile its dependent;
  an API change does. Worker transport tested with a Python protocol worker
  (`tests/test_workers.py`). Acceptance benchmark vs Gradle in
  `benchmarks/jvm/` (`bench.py` + methodology): phase-2 brix wins 4 of 5
  scenarios (cold clean, warm cache, no-op, single-module impl change) and
  loses only the multi-module API-change recompile to Gradle's warm daemon —
  the case phase 3's cross-build daemon is meant to flip. Remaining: phase 3
  (cross-build daemon), reserved for full-strength review.
- **Rust port, phase 1 — `brix-core` bottom layer + golden tests**
  (`rust/brix-core/`, `rust/golden/generate.py`): the leaf pieces with no
  scheduler, ported to pure Rust (no PyO3 yet). `fingerprint` reproduces
  Python's `_digest` **byte-for-byte** — a hand-rolled canonical JSON
  serializer matching `json.dumps(sort_keys=True, separators=(",",":"))`
  with `ensure_ascii=True` (non-ASCII `\uXXXX`, astral surrogate pairs,
  and the `0x7f` escape boundary that Python's `[^\ -~]` rule imposes) plus
  sha256; `patterns::PathPattern` hand-matches the `{name}` glob language
  (backtracking `[^/]+`, repeated-binding consistency — no regex crate,
  which lacks backreferences) with `substitute`/`overlaps`/
  `literal_prefix_dir`; `data` carries the atom kinds and their structural
  identity; `shlex` splits POSIX-style like Python's `shlex.split`. The
  contract is **cross-language golden fixtures**: `rust/golden/generate.py`
  runs the *real* Python functions and emits JSON that `cargo test` asserts
  against (9 unit + 4 golden), so "matches Python" is verified, not
  claimed. `RegexData`'s Python-`re` matching and `action_fingerprint`'s
  file I/O are later phases. Remaining: phase 2 (planner/targets) and
  phase 3 (scheduler/layers), the latter for full-strength review.
- **Rust port, phase 2 — planner + targets** (`rust/brix-core/`:
  `spec.rs`, `actions.rs`, `plan.rs`, `planner.rs`, `targets.rs`): the
  graph spec deserializes from the `docs/boundary-schema.md` JSON (serde),
  `plan_graph` infers dependency edges from output/input atom overlap, and
  `select_actions` does demand-driven target selection with binding
  extraction and the shortest-stem rule. `ExecutionPlan.describe()` is
  byte-identical to Python's. A parity subtlety made load-bearing:
  `inputs`/`outputs` keep **insertion order** (a `Vec` of pairs, and an
  order-preserving serde deserializer), because an edge reason reports the
  *first* overlapping atom — a sorted map would change the reported reason.
  Golden fixtures extend `rust/golden/generate.py`: it serializes **real**
  Python graphs (expansion, typed C++ lowering, the shortest-stem pipeline)
  to specs and captures `plan.describe()` for several target sets; the spec
  is stored as a raw JSON string so key order survives into the Rust
  deserializer. `cargo test` is 9 unit + 5 golden. Regex-atom overlap and
  worker inputs are deferred (later phase). Remaining: phase 3
  (scheduler/layers/context, then the `BRIX_ENGINE` default flip), for
  full-strength review.
- **Scheduler-contract tests** (`tests/test_scheduler_contract.py`): the
  subtleties `docs/rust-port.md` flags as "only partially pinned by tests"
  (and reserves phase 3 for reviewing) made into an explicit executable
  spec — a net for the eventual Rust scheduler port that *either* engine
  must pass. Pins: the event-ordering contract (`build_begin` first /
  `build_end` last; per node `node_started` < `process_started` <
  `node_completed`; `collection_realized` before its children); the
  stuck-node **snapshot** rule (a 2-node cycle reports *both* nodes as
  unreachable, not one — verified by mutating the executor to the naive
  advance-then-rejudge form and watching this test catch it — and the
  all-terminal-predecessor quiet-skip); the on-success-only failure cascade
  (transitive skip along on-success edges without spurious "unreachable"
  errors, and on-completion reactivating a branch past a failure); and the
  insertion-channel ordering (a proposed cleanup runs before the
  expansion's consumer). Purpose: lower the risk of Rust phase 3 by making
  its acceptance criteria concrete rather than review-dependent.
- **Rust port, phase 2 part 1 — fingerprint keys + regex overlap**
  (`rust/brix-core/`: `keys.rs`, `data.rs`, `planner.rs`): the structural,
  content-independent identity keys — `action_key` (command + input/output/
  env shapes via `_expr_identity`) and `membership_key` (expansion element +
  collection shape) — plus `RegexData`'s Python-`re` matching and
  substitution (`\A(?:…)\z` full-match, participating named captures,
  `re_escape` on substituted values). With regex matching in hand,
  `atoms_overlap` is now faithful to Python: regex-vs-regex never overlaps,
  regex-vs-concrete uses full-match (the deferral from phase 2 is gone). New
  golden fixtures from **real** Python: `keys.json` (11), `regex.json` (2),
  and a regex-overlap plan case (a concrete producer `videos/01.py` feeding a
  regex collection, so the edge reason is exercised end-to-end). `cargo test`
  is 9 unit + 7 golden.
- **Rust port, phase 2 part 2a — filesystem primitives** (`rust/brix-core/`:
  `fsutil.rs`, `detector.rs`, `patterns.rs`): the content fingerprint's
  filesystem groundwork. `walk_files` (the file half of `rglob("*")`),
  `TimeAndHashChangeDetector.hash_file` (sha256 of bytes, `None` for missing/
  non-regular files) + `stat_signature`, and `PathPattern::find` (concrete =
  `is_file` probe; templated = walk the literal prefix dir and full-match,
  sorted). The Python `(mtime,size)→hash` memo is a perf cache that never
  changes the value, so the port hashes directly. Pinned by a **committed
  fixture tree** (`rust/golden/tree/`) + `fs.json`: `find` covers single- vs
  multi-segment bindings and extension filtering; `hash_file` covers a file, a
  directory (`None`) and a missing path (`None`). Content hashing is
  mtime-independent, so a git checkout reproduces the digests.
- **Rust port, phase 2 part 2b — content fingerprint** (`rust/brix-core/`:
  `context.rs`, `content.rs`): `NodeContext` (`read_path`/`find`/`iter_files`)
  with the staging+deletions overlay (staged outputs shadow the workspace,
  markers hide files; sorted results), and `action_fingerprint` — the cache
  layer's key — over file/glob/regex/value atoms: `command` + `inputs` (+ `env`
  when non-empty), `outputs` excluded, keys digested sorted. `tool` / `identity=`
  / `worker` are the deferred tail and **error** rather than guess, so a caller
  can never get a silently-wrong key. Golden `fingerprints.json` (5) from real
  Python over the fixture tree: single file, glob, regex-over-`iter_files`,
  value+env, and a union input. `cargo test` is 9 unit + 9 golden.
- **Recursive-by-default glob syntax (`{name}` / `{name:*}` / `{name:**}`) +
  packaged-Java stale-jar fix** (`patterns.py`, `rust/brix-core/src/patterns.rs`,
  `java.py`): a bare `{name}` matches *across* `/` (`.+`, like make's `%`),
  `{name:*}` pins one segment (`[^/]+`), and `{name:**}` is the explicit
  recursive form — so `build/classes/{rel}.class` matches `App.class` *and*
  `com/foo/Bar.class`. (This supersedes the earlier `{**name}`-prefix syntax:
  recursion is the sensible default for build outputs, and the simple case now
  writes nothing extra while single-segment is the opt-in constraint.) Fixes a
  **confirmed correctness bug**: `JarAction` tracked classes with a
  top-level-only glob, so a changed *packaged* class re-ran `javac` but not the
  jar step, leaving a stale jar (`test_packaged_class_change_updates_jar`, real
  JDK, was `xfail`, now passes). Realized through the overlay-aware `find()`
  (which walks recursively), so it sees classes staged under an atomic build —
  unlike `RegexData`/`iter_files`, which doesn't surface staged outputs.
  Implemented in *both* engines and cross-checked: `PathPattern` compiles a
  recursive binding to `.+`, the Rust matcher scans to end-of-path with the same
  greedy backtracking; new golden pattern cases pin match/substitute parity
  (`cargo test` 9 unit + 9 golden). Also fixes latent nested-source globbing
  (Java sources live in package dirs like `com/foo/Bar.java`, which the old
  single-segment default silently missed).
- **Rust port, phase 3 slice 1 — the scheduler core** (`rust/brix-core/`:
  `executor.rs`, `process.rs`, `render.rs`, `report.rs`, `layers.rs`, plus
  `NodeState`/`EdgeTrigger`/`NodeStateTable` in `plan.rs`): `execute()` ported
  structure-for-structure from `executor.py` — readiness (on-success needs
  DONE; on-completion needs DONE *or* FAILED, so a skipped predecessor
  satisfies neither), the on-success-only failure cascade, the stuck-node
  **snapshot** rule (all-terminal predecessors → quiet skip, otherwise
  "unreachable"), runtime expansion (nested `ExpandAll` included), and the
  insertion channel with layer proposals ordered before the expansion's
  consumers. Process spawning on plain scoped threads (no async runtime);
  command/env rendering and output-dir preparation mirror `render.py`; the
  `BuildLayer`/`Reporter` seams are traits with the exact Python hook set, and
  `process_started` fires only when a real spawn is imminent, from the worker
  thread. Clean builds run end-to-end in pure Rust (spec JSON → planner →
  scheduler → processes). Pinned by `rust/brix-core/tests/executor.rs` — the
  Rust mirror of `tests/test_scheduler_contract.py`, same cases, same names —
  plus a nested-expansion and an insertion-ordering case (11 tests; a worker
  panic becomes a failed node rather than a hang, the one deliberate
  divergence). Layers (incremental/cache/atomic), workers, and the PyO3
  boundary are the remaining slices.
- **Rust port, phase 3 slices 2+3 — overlay execution + the incremental
  layer** (`rust/brix-core/`: `metadata.rs`, `incremental.rs` new;
  `detector.rs`, `content.rs`, `actions.rs`, `process.rs` extended). Slice 2
  proved the phase-2b overlay under real execution: staged writes, merged
  reads, deletion markers, and redirected deletes, via a layer pointing
  `output_root` at a staging tree (3 tests in `tests/executor.rs`). Slice 3
  is the full incremental layer: `BuildMetadata` reads/writes **the same
  `metadata.json` Python writes** (version 1, sorted keys, tmp+rename;
  lenient loads where a malformed record degrades to a rerun, never a crash;
  Python-written `jvm-abi@1` identity digests survive a Rust load/save
  round trip untouched — interop pinned by a hand-written Python-format
  fixture). The change detector gained the persistent `(mtime_ns, size) →
  sha256` memo; `action_fingerprint` gained tool atoms (PATH resolution +
  stat signature) and the discovered-deps fold. `IncrementalLayer` mirrors
  Python behavior-for-behavior: fresh nodes replay their recorded
  stdout/stderr (`source="replayed"`), a failed action is never fresh and
  its declared outputs are removed, vanished expansion members become
  Delete proposals through nested membership reaping (memberships committed
  only on build success), and depfile scanning folds a tool's reported
  headers into the fingerprint *in the same build* (no spurious second
  rebuild). The `depfile` field was added to the wire Process shape in
  `docs/boundary-schema.md` (still version 1 — optional and additive; it
  was always in the Python action but missing from the pinned shape).
  Pinned by `tests/incremental.rs` — the Rust mirror of
  `tests/test_incremental.py`, same cases, same names, sh scripts standing
  in for the Python tool scripts — including the "never make a stale hit"
  invariants (added glob member re-stales its consumer; a tool change
  invalidates everything it built while byte-identical outputs keep
  downstream fresh). `cargo test` is 18 unit + 9 golden + 28 behavioral;
  clippy clean. Deliberate divergence (documented in the code): a
  fingerprint error (unported identity plugin) fails the node instead of
  crashing the build. The cache and atomic layers, workers, and the PyO3
  boundary are the remaining slices.
- **Rust port, phase 3 slice 4 — the cache layer** (`rust/brix-core/src/cache.rs`):
  reuse results across workspaces, keyed by the same `action_fingerprint`
  the incremental layer records. The store is pluggable
  (`CacheStoreProtocol` trait — load / restore / store); the built-in
  `CacheStore` shares Python's on-disk layout byte-for-byte
  (`<fp[:2]>/<fp>/entry.json` + `files/<n>` blobs, tmp-then-rename with a
  lost race discarded), **so the two engines can share one cache
  directory** — pinned in both directions: Rust restores from a
  hand-written Python-format entry, and a Rust-written entry carries the
  exact key shape Python's `ProcessResult(**d)` needs. `TwoTierCacheStore`
  is ported too (front-first reads, back hits promoted into the front, a
  down back never blocks local caching). Resilience lives in the *layer*,
  as in Python: any store error — unreachable remote, corrupt entry,
  disk-full populate — degrades to a cache miss or a skipped write, never
  a failed build. Real failures with an exit code are cached and replayed
  (`source="cache"`, exit code and stderr intact); signal-killed or
  never-started runs are not. Restored outputs write through
  `ctx.output_root`, so an output-redirecting outer layer (the future
  atomic port) routes them via staging — covered by a redirect test.
  Pinned by `rust/brix-core/tests/cache.rs`, the mirror of
  `tests/test_cache.py` + `tests/test_cache_store.py` (fresh-checkout and
  rm-rf-rebuild payoffs, freshness-beats-cache, miss-then-repopulate,
  broken/write-failing stores, two-tier promotion). `cargo test` is 18
  unit + 9 golden + 43 behavioral; clippy clean; Python suite 190. The
  atomic layer, workers, and the PyO3 boundary are the remaining slices.
- **Rust port, phase 3 slice 5 — the atomic layer** (`rust/brix-core/src/atomic.rs`):
  either the whole build lands, or nothing on disk changes. Ported
  behavior-for-behavior from `atomic.py`: `on_build_begin` recovers an
  interrupted commit (COMMIT marker → idempotent re-apply), discards
  failed-build debris, keeps held staging to build on top of (consuming
  the HELD marker so only the new build's outcome decides what staging
  becomes), then redirects `output_root`/`deletions_root` into
  `build/staging/add` + `del`; Delete actions become staged markers
  applied at commit; `on_build_end` journals COMMIT first, then copies
  `add/`, applies `del/`, and removes staging — or writes HELD instead
  under `hold`. `commit_staging` refuses failed-build debris (only a
  successful build's staging may reach the workspace) and
  `discard_staging` refuses an interrupted commit (it already committed
  and must finish). With this, **all three build layers run in pure
  Rust**: the full `[cache, incremental, atomic]` stack is exercised
  end-to-end, including cache hits restored via staging and committed
  with the build. Pinned by `rust/brix-core/tests/atomic.rs` — the
  mirror of `tests/test_atomic.py` + `tests/test_held_staging.py` (15
  tests; the status/CLI cases stay Python until the boundary slice).
  `cargo test` is 18 unit + 9 golden + 58 behavioral; clippy clean;
  Python suite 190. Persistent workers and the PyO3 boundary are the
  remaining slices.
- **Rust port, phase 3 slice 6 — the PyO3 boundary** (`rust/brix-py/`,
  `src/brix/engine.py`, `src/brix/lowering.py`): set `BRIX_ENGINE=rust`
  and every builder's `build()` crosses into the Rust engine — same
  user-facing API, same reporter events, same `BuildResult`. The
  boundary is one function, `brix_py.build(spec_json, config_json,
  on_event=None) -> result_json`: three JSON strings in the shapes
  pinned by `docs/boundary-schema.md`, deliberately language-neutral so
  the future JS/TS binding is the same crate with a different FFI
  prologue. The GIL is released for the whole build and re-acquired per
  event; a reporter exception doesn't tear down the build mid-flight —
  it re-raises once the build finishes. The graph→spec lowering moved
  from `rust/golden/generate.py` into `brix/lowering.py` (goldens stayed
  byte-identical); builders grew `layer_specs()` (their layer stack as
  wire configs) and `source_graph()`. Two additive wire extensions
  (user-approved): `build_begin` now carries the plan's nodes, and the
  result JSON carries the final plan (expanded/inserted nodes included)
  — the Python side synthesizes `build_end` from the returned result.
  Graphs the port can't take yet — workers, `identity=` plugins, custom
  Python cache stores — fall back silently to the Python engine;
  `BRIX_ENGINE=rust` without a built extension raises instead of
  quietly benchmarking the wrong engine. Extension discovery: installed
  `brix_py` module → `$BRIX_RUST_ENGINE_LIB` → `rust/target/{release,
  debug}` (built with `cargo build -p brix-py`, no maturin needed).
  Boundary pins in `tests/test_rust_engine.py`; the whole Python suite
  passes on **both** engines (197 with `BRIX_ENGINE=rust`, 197 without —
  unsupported graphs exercising the fallback). `cargo test` is 18 unit +
  9 golden + 58 behavioral; clippy clean across the workspace.
  Persistent workers are the remaining slice.
- **Rust port, phase 3 slice 7 — persistent workers**
  (`rust/brix-core/src/workers.rs`): the warm-compiler transport runs in
  Rust. Ported behavior-for-behavior from `workers.py`: the JSON
  line protocol over the worker's stdin/stdout, one request in flight
  per worker process, lazy start on first request, pools per spec, all
  workers terminated at build end; a worker crash or startup failure is
  an `exit_code: None` result — never cached, never a silent fallback
  to spawning. The wire `Process` shape gained its reserved `worker`
  key; worker inputs create dependency edges (a bootstrap-built worker
  jar's producer runs first) and the worker's command + inputs join the
  action fingerprint, now golden-pinned for byte-parity (fingerprints
  are cache keys shared across engines). One deliberate translation:
  pools are keyed by spec content rather than Python's object identity
  — across the JSON boundary every node carries a copy of its spec.
  Pinned by `rust/brix-core/tests/workers.rs`, the mirror of
  `tests/test_workers.py` (same cases, same names). With this, worker
  graphs cross the boundary under `BRIX_ENGINE=rust` (the Java worker
  tests included); the suite is 197 on both engines, `cargo test` 18
  unit + 9 golden + 63 behavioral, clippy clean. Content-identity
  plugins are the last fallback before the default flip.

- **Rust port, phase 3 slice 8 — built-in content identities**
  (`rust/brix-core/src/jvmabi.rs`, `tsdts.rs`, `identity.rs`): the
  `jvm-abi` and `ts-dts` plugins are implemented in the core, so every
  skin — the future JS/TS one included — gets ABI-fingerprinted Java/TS
  builds without calling back into Python per file. Byte-identical
  digests: `rust/brix-core/tests/identity.rs` (the mirror of
  `tests/test_content_identity.py` + `tests/test_ts_dts_identity.py`)
  asserts the exact digests Python pins over the same committed `.class`
  fixtures, and the fingerprint tail (`["jvm-abi@1", digest]`, or
  `["jvm-abi@1:raw", byte_hash]` when a file can't be parsed) is
  golden-pinned. Identity digests cache in `metadata.json` under their
  versioned tag, interoperably with Python-written entries. Python stays
  the reference implementation — an algorithm change bumps the version
  tag in *both* implementations (docs/boundary-schema.md). The engine
  fallback narrowed accordingly: only a custom skin-registered identity
  keeps a graph on the Python engine, and `tests/test_rust_engine.py`
  now proves a Python-engine build replays as all-fresh on the Rust
  engine through shared metadata. Suite is 199 on both engines,
  `cargo test` 21 unit + 9 golden + 81 behavioral, clippy clean. Phase 3
  is complete; next is the `BRIX_ENGINE` default flip.

- **Same-collection binding unification in dependency inference**
  (`patterns.py`/`planner.py`, mirrored in `rust/brix-core/src/patterns.rs`/
  `planner.rs`): found live on the animation project the moment the Rust
  engine tried it — `combine {video}` and `finalize {video}` both expand one
  `videos` collection, and with recursive-by-default `{video}` the planner
  read `finalize`'s `out/{video}.mpg` output as overlapping `combine`'s
  `out/{video}/{scene}.mpg` input (`out/a/b.mpg` splits as video `a/b` *or*
  video `a` + scene `b`), reported a false combine↔finalize cycle, and
  skipped both. Both engines failed identically — parity held; the
  inference was wrong. Fix: when two expansions iterate structurally-equal
  collections under the same element name, their binding holds the same
  value in any realized pair, so `overlaps` strips the matching prefix
  (identical literals + shared bindings, which occupy the same span) and
  runs the ordinary conservative check on the tails — sharing only ever
  removes provably-impossible overlaps, so a missed strip degrades to the
  old conservative edge, never to a missed dependency. Nested video
  directories (`rights/intro`) build fine under bare `{video}`
  (`test_same_collection_expansions_build_end_to_end`). Pinned by mirrored
  plan/pattern tests (same names) and new golden overlap + plan cases.

- **Rust port, phase 4 (part 1) — the Rust engine is the default**
  (`src/brix/engine.py`): `BRIX_ENGINE` unset now runs the Rust engine;
  `BRIX_ENGINE=py` forces the Python engine (kept working one release);
  an explicit `BRIX_ENGINE=rust` with no built extension still raises,
  but the *default* quietly falls back to Python so a pure-Python
  checkout keeps building. Verified on the animation project end-to-end
  (its stale venv `brix.pth` re-pointed via `pip install -e`): full
  dryRun build (106 nodes), no-op rebuild all-replayed on *both* engines
  against the same metadata, targeted `build wealth` (12-node plan),
  `clean` then full cache restore (94 hits), `status`. Two animation
  `brix_build.py` updates rode along (uncommitted there): `{video:*}` on the
  videos glob — bare `{video}` would read every scene/helper/`__init__`
  as a video, and path shape alone cannot distinguish `rights3/Fence.py`
  (helper) from a future `rights/intro.py` (nested video); when videos
  move into subdirectories, compute the list in `create_graph` (sibling-
  directory test) instead — and a nested-capable scene regex
  (`(?P<video>.+)`). Suite: 206 both engines; `cargo test` 25 unit +
  9 golden + 81 behavioral; clippy clean. Remaining for phase 4: one
  release of soak, then delete the Python engine internals.

- **Rust port, phase 4a — `status` and `clean` call the core**
  (`rust/brix-core/src/status.rs`, `brix_py.status`/`brix_py.clean`,
  `src/brix/status.py`/`cli.py` as thin clients): the freshness decision
  now exists once — `compute_status` and the CLI's `clean` cross the
  boundary like `build` does (wire shapes in `docs/boundary-schema.md`;
  rendering and exit codes stay Python). The report is the same walk the
  incremental layer would do at node-ready time: per-action
  fresh/never-built/inputs-changed/outputs-missing/failed states,
  pending deletions for vanished expansion members, staged/held atomic
  state, and outdated-propagation downstream of dirty work. Graphs with
  a custom skin-registered identity still fall back to the Python
  implementation (until the extension-trait slice). Pinned by
  `rust/brix-core/tests/status.rs` — the core cases of
  `tests/test_status.py`, same names — a cross-engine parity sweep, and
  new boundary-dispatch tests in `tests/test_rust_engine.py`.

- **Rust port, phase 4b — extension traits: custom stores and identity
  plugins cross the boundary** (`rust/brix-py/src/lib.rs`,
  `src/brix/engine.py`, `builders.py`, `lowering.py`;
  `rust/brix-core/src/identity.rs` gained the `CustomIdentities` seam
  threaded through the change detector): the last two Python-engine
  fallbacks are gone. A `CacheBuilder(store=...)` store rides the build
  as the `cache_store` trait — the Rust cache layer calls its
  `load`/`restore`/`store` back across the boundary (entries cross as
  JSON; a store exception is a miss/skipped populate, never a failed
  build), so the HTTP/two-tier cache tests now exercise the Rust engine
  end-to-end. A custom `identity.register(...)` plugin rides as the
  `content_identity` trait: the engine calls its `digest` per file (from
  worker threads), caches under the same versioned tag, honors the
  raise-means-`:raw` fallback, and an unregistered name raises before
  anything runs. `supports_rust_engine` is gone — every graph crosses.
  Wire contract notes in `docs/boundary-schema.md`. Suite: 209 both
  engines (custom-store and custom-identity crossing tests included);
  clippy clean.

- **JVM fast builds, phase 3 — cross-build worker daemon**
  (`rust/brix-workerd/` new bin crate, `rust/brix-core/src/workers.rs`,
  `content.rs` `worker_fingerprint`, boundary + CLI knobs): warm workers
  now survive *between* builds — the edit-compile loop's JVM stays hot.
  The build process exits, so persistence lives in `brix-workerd`, a
  small holder daemon per worker: it detaches, owns the worker's pipes,
  and bridges one build at a time over a Unix socket (short real path in
  the temp dir; the registry under `<metadata_dir>/workers/<fp16>/`
  holds pointer files — socket paths are SUN_LEN-limited and workspaces
  can be deep). The registry key is the worker-section fingerprint
  already in every action fingerprint, so a JDK upgrade or rebuilt
  wrapper jar just misses the registry and the orphan idles out —
  replacement with no kill logic. Sessions end with an explicit release
  line; any abrupt disconnect (engine crash, discarded-after-garbage
  worker) poisons the worker and the holder kills it — mid-request state
  is unknowable, so v1 crash semantics hold exactly. One knob:
  `worker_idle_timeout` (default 900 s; 0 = no daemon at all, v1 pipes;
  CLI `--no-daemon` / `--worker-timeout`; env
  `BRIX_WORKER_IDLE_TIMEOUT`, which `tests/conftest.py` zeroes so suites
  never leak daemons). Never fingerprint material. Rust engine only —
  the Python engine keeps v1 until 4c deletes it. Pinned by
  `rust/brix-workerd/tests/daemon.rs` (adoption by pid equality, idle
  expiry, fingerprint re-key, crash-not-reused, busy-holder
  concurrency) and `tests/test_worker_daemon.py` through the full
  boundary. Suite: 211 both engines; cargo 13 suites; clippy clean.
  Next: the Gradle acceptance benchmark, scenarios 4/5 warm.
