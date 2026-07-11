# Brix

git test

A build-system toolkit: define build graphs in ordinary Python while the
engine handles planning, dependency inference, and parallel execution.

**The long game:** prototype everything in Python first to stabilize the
functionality and the API. Then move the engine internals to Rust (PyO3), so
the same core can later back a JavaScript/TypeScript API. Python stays the
user-facing build language.

> This README is the hand-off document *and* the spec. A fresh session should
> be able to read this file plus [docs/build-layers.html](docs/build-layers.html)
> and continue the work. It is updated at every commit — especially the TODO
> list and the Completed log.

## Start here

1. This file — vision, spec, current state, and the TODO list.
2. [docs/build-layers.html](docs/build-layers.html) — the agreed design for
   the next phase (incremental / cache / atomic layers). Open in a browser;
   it has class and data-flow diagrams.
3. [docs/class-diagrams.md](docs/class-diagrams.md) — class diagrams for
   every class in `src/brix/` (48 classes, five grouped diagrams: data
   model, actions, execution plan, builders & layers, support).

## Current state

Minimal prototype (spec steps 1–10 plus dependency inference), the
build-layer seams, the full IncrementalBuilder (fingerprint skip +
deleted-source cleanup), the reporter seam, status reporting with a CLI,
AtomicBuilder, typed C++/Java actions, nested expansion, and per-action
environments are implemented and working. 103 tests pass (plus a JDK
end-to-end test that skips when no JDK is on PATH).

- Logical data expressions: `file`, `glob`, `regex`, `tool`, `value`, unions
- `Graph.add(...)` with generic `ExternalProcessAction` (`name` is optional;
  label falls back to the command line)
- Lazy command-line placeholder rendering (only placeholders referenced in
  `command_line` are rendered)
- Dependency inference from data-set intersections (conservative overlap)
- Graph-shaped `ExecutionPlan` with a parallel thread-pool scheduler
- `ExpandAll` as an unrealized plan node, expanded at runtime — including
  over collections produced by generator actions during the same build.
  Children are any `Expandable` (process action *or another ExpandAll*),
  so expansion nests: per video, per scene of that video. Regex
  collections substitute outer bindings (named group → literal) and can
  filter membership (e.g. numbered scene files only)
- `env=` on `ExternalProcessAction`: per-action environment entries
  (data expressions; fingerprinted like inputs)
- Structured `ProcessResult`s (stdout/stderr/exit/signal); failure cascades
  skip downstream nodes; cycle detection
- `CleanBuilder(graph, root).plan()` / `.build(jobs, reporter)`
- `BuildLayer` / `NodeContext` seams: per-node `around_node` middleware
  stack plus lifecycle events in the executor; scheduler-owned monotone
  `NodeStateTable` (node definitions are frozen)
- `IncrementalBuilder`: fingerprint-fresh nodes complete with their previous
  result (stdout replay included); deleted sources produce layer-proposed
  `rm` nodes through the insertion channel; failed outputs are removed
- Edge trigger modes: `on-success` (default, cascades skips) and
  `on-completion` (fires after success *or* failure)
- Reporter seam: the engine never prints; builders are silent by default
  and take a `Reporter` (`ConsoleReporter`, `ErrorsAtEndReporter`, or your
  own); `ProcessResult.source` carries provenance and replays are marked
  `(replayed)` by the console reporter; `process_started` fires only when
  a real process spawns (never for replays) so long-running builds show
  liveness (`run: render wealth/03`)
- `compute_status()` + `brix.cli.main()`: `brix.py status` explains what
  would build and why (never built / inputs changed / outputs missing /
  failed last build / outdated upstream / pending deletions) without
  building; `brix.py errors` replays recorded failure diagnostics;
  `build`, `clean`, `graph` round out the CLI; `commands=` adds project
  build-then-act subcommands (`./brix.py view wealth` builds the video,
  then hands the built paths to the project's function)
- Target selection: `build`/`status` take output paths or aliases
  (`./brix.py build wealth`); demand-driven backward selection with
  binding propagation constrains expansions (targeting one video never
  realizes the others' scenes), make's shortest-stem rule resolves
  ambiguous producers, and targeted builds share incremental records
  with full builds
- `AtomicBuilder(inner, staging_dir=...)`: outputs and deletions stage
  under `build/staging/` and commit only if the whole build succeeds; the
  `NodeContext` overlay makes every read see the workspace as it will look
  after commit; crash mid-commit is journaled (COMMIT marker) and finished
  by the next build; failed-build staging is inspectable via status, then
  discarded. **Held staging**: `build --stage` (API: `build(hold=True)`)
  builds fully but keeps outputs in staging for review — `status` shows
  them awaiting commit (freshness read through the overlay), `commit`
  applies, `discard` drops; a plain build on top composes and commits
  everything
- `CacheBuilder(inner, cache_dir=...)`: results shared across workspaces
  and clean checkouts, keyed by the same `action_fingerprint` incremental
  records; stdout/stderr replay, `source="cache"` provenance, cached-
  failure replay; the layer sits innermost so fresh nodes never touch the
  cache and restored outputs route through atomic staging; the store
  survives `brix.py clean` (animation: full clean rebuild 1m46s → 16s,
  zero processes)
- `brix.cpp` typed actions (`CompileSourceAction`, `LinkApplicationAction`)
  and `brix.java` (`JarAction` — javac + jar, optional `entry_point`) that
  lower to the generic model in `Graph.add`; raw and typed forms compose
  in one graph (`examples/cpp_to_java/`: a C++ tool generates Java sources
  that feed a jar)

## TODO

**Priority:** lock down how brix behaves (in Python) before rewriting the engine
in Rust. Every behavior settled now is rework the rewrite won't have to redo.

**Who does what.** Two AI models share the work. **Opus** is the everyday model:
it handles self-contained jobs that come with tests and don't need deep judgment.
**Fable** is the expensive, most-careful model, saved for the few pieces where a
subtle mistake would break builds in ways ordinary tests might miss — mainly the
scheduler (the part that decides what runs when) and the build server. Each item
below is tagged with the model that should do it.

### The three big tracks

1. **Make Java builds fast** — design notes in `docs/java-fast-builds.md`. Mostly
   done: brix already skips recompiling a Java library's users when only its
   internals changed (not its public interface), and it keeps compiler processes
   alive so they don't pay startup cost on every file. **The always-warm
   build server landed** (see Completed: cross-build worker daemon): warm
   compiler processes now survive *between* builds, adopted by the next
   build through a per-worker holder daemon, with a single
   `worker_idle_timeout` knob (default 15 min; 0 or `--no-daemon` turns it
   off). **This track is done, and the "faster than Gradle" claim is
   confirmed**: benchmarked against Gradle 8.14 on JUnit 5's seven platform
   modules (`examples/junit5/benchmark.py`), brix wins the edit-compile loop
   ~5–12× (no-op ~0.09s vs ~1.1s; impl-only change ~0.37s vs ~1.8s) and ties
   a full API-change cascade (~1.3s vs ~1.6s). Remaining polish, if wanted:
   class-level incremental compilation within a module is deliberately out of
   scope (see the design doc).
2. **Rewrite the engine in Rust** — design notes in `docs/rust-port.md` and
   `docs/boundary-schema.md`. The goal: one shared engine that can power *both* a
   Python and a JavaScript/TypeScript way of writing build files, so nobody has
   to learn a new language. Most of the groundwork is done and proven to match
   the Python behavior exactly (via tests that cross-check the two): the
   change-detection math, the planning, the dependency inference, the
   scheduler core (runs steps in parallel in the right order — clean builds
   work end-to-end in pure Rust), incremental rebuilds (a Rust build skips
   everything that hasn't changed, sharing the same metadata file Python
   writes), the shared result cache (a fresh checkout builds instantly from
   results any earlier build stored — Rust and Python can even share one
   cache directory), atomic builds (all-or-nothing commits through
   a staging area, including the held-for-review flow), the
   Python↔Rust boundary itself (set `BRIX_ENGINE=rust` and every
   builder's `build()` runs on the Rust engine — same graph, same
   progress events, same results), persistent workers (the
   warm-compiler transport that makes Java builds fast runs in Rust
   too, so worker-routed builds cross the boundary), and now the
   built-in content identities (`jvm-abi` and `ts-dts` produce
   byte-identical digests in Rust, so Java/TS graphs cross the
   boundary too — only a hand-registered custom identity plugin still
   falls back to Python). **The Rust engine is now the default**:
   `BRIX_ENGINE=py` forces the old engine, the animation project
   builds/replays/cache-restores/targets correctly on both engines,
   `status` and `clean` call the core (phase 4a), and custom cache
   stores + custom identity plugins cross as extension-trait callbacks
   (phase 4b) — nothing falls back to the Python engine anymore.
   **What's left — phase 4c, ON HOLD for soak (Duane's call,
   2026-07-09): delete the Python engine internals** after the
   animation project has run on the Rust default for a real work
   stretch (days, not hours). Agreed deletion boundary: delete the
   execution half (`executor.py` scheduler, `process.py`/`render.py`,
   `workers.py` transport, the incremental/cache/atomic `around_node`
   machinery, `status.py`'s fallback walk); keep the reference math
   (`patterns`/`planner`/`targets`, `incremental.py` fingerprints,
   `metadata.py`, `jvmabi`/`tsdts`/`identity`) because
   `rust/golden/generate.py` generates the byte-parity fixtures from it
   and Python stays the reference implementation. Consequences to
   accept at that point: `BRIX_ENGINE=py` becomes a clear error, and a
   checkout without the compiled extension cannot build (the animation
   project then hard-requires `cargo build -p brix-py` or a maturin
   wheel). **[Fable]** — the trickiest part of the whole project; one
   subtle ordering bug here breaks builds, so it needs the most careful
   model reviewing it line by line.
3. **A JavaScript/TypeScript way to write build files** — same features as the
   Python one, just in their language (`docs/boundary-schema.md` pins the
   mirror-API contract: same names, camelCase, async `build()`, plain-object
   reporters — and the wire shapes the binding speaks). **UNBLOCKED** as of
   the phase-4 flip: the shared Rust engine exists, is the default, and
   carries build/status/clean plus the extension traits. The binding is
   `rust/brix-py` transliterated (napi-rs or similar FFI prologue over the
   same JSON boundary), and the TS API layer is mechanical against the
   contract — Opus-sized with a Fable review of the FFI threading
   (callbacks arrive on build worker threads).

### Feature-completeness dogfooding (in progress — prove brix on real projects before starting track 3)

Cloning real projects has been the best gap-finder (each one surfaced a real
bug or missing feature). Clones live in `~/Projects/brix_test/`; our `brix.py`
artifacts live in this repo under `examples/`. **Done:** JUnit 5 (Java compile
graph; found the jvm-abi String-annotation bug; resources), fmt (C++ header-dep
tracking), jackson-core (selective test running via `java.JUnitTestAction`;
found generated-sources + shaded-dep needs), and leveldb (static-library build
of 38 TUs via `cpp.CompileSourceAction` + `cpp.ArchiveAction`, linked + run
against a smoke program — see below).

**Next, in priority order (all Opus — dogfood + helper additions, no engine
changes expected):**
1. **Annotation processors** — a small Lombok/MapStruct project; add a
   `-processorpath` option to `JarAction` and validate the processor jars are
   tracked as fingerprint inputs.
2. **Multi-module cross-suite test selection** — add `jackson-databind`
   (depends on `jackson-core`): a core impl change re-runs databind's tests
   but not annotations'.

Deferred (need features + dependency resolution — better with the real
monorepo): Spring Boot fat-jars, Kotlin modules.

### Opus queue — ready to do now

These are self-contained and come with tests. No Fable needed.

**Retire `identity=`; make comparison a `content_key` Rust trait**
(design agreed — see `docs/content-key.md`). The `identity="jvm-abi"` string
kwarg on data atoms is a comparison concern in the wrong locus. Replace it with a
polymorphic `content_key` on the data objects, implemented as a **name-dispatched
Rust trait** (byte-hash is the default impl); the built-in lenses (`jvm-abi`,
`ts-dts`) move *out* of brix-core's neutral path into a `lenses` module/crate, and
`brix.java` owns the `java.abi(...)` surface — the core stops naming Java. Python
side becomes declaration only. Crosses the Python/Rust boundary on the fingerprint
path under a **fingerprints-byte-identical** invariant (pure relocation, no
behavior change), so: build on Opus, Fable review pass. Custom pure-Python lenses
are dropped for now (the `CustomIdentities` callback is additive to re-add later —
that's why dispatch must stay a name registry, not an `enum`). Touches
`docs/boundary-schema.md`. **Do the `-`/exclude item (Fable queue) first.**

**Correctness fixes** (holes found in the "what might we be missing?" review):

- **Let you say "run B after A" without a shared file.** Today brix works out
  ordering only from files — A makes a file, B reads it. Some steps need to be
  ordered without sharing a file, like "start the service, then run tests against
  it." There's no way to express that yet.
- **Warn when two steps write the same file.** Right now brix silently lets the
  second one win, which can make a build come out differently depending on timing.
  It should catch the collision instead.
- **An optional "always re-check file contents" mode.** To go fast, brix assumes a
  file is unchanged when its size and timestamp both match. In rare cases a file's
  contents can change while those stay the same; this mode would catch it, at the
  cost of some speed.
- **Record the environment a build used.** An environment variable or machine
  setting that a tool reads, but that the build file didn't declare, can change a
  build's result without brix noticing — the classic "works on my machine, fails
  on the server." Recording what was actually used is the first step to catching
  it.

**Making it easy to start:**

- **A `brix` command, like `git`.** `brix init` sets up a new project with a
  starter build file; `brix build` runs it. Today there's no `brix` command — you
  run your build file directly with Python.
- **Draw a picture of the build.** Boxes for steps, arrows for what depends on
  what, instead of a text list.
- **Ready-made starter templates** for a Java service and a TypeScript service, so
  people begin from something that already works.

**Building trust** (also makes the Rust rewrite safer to check):

- **More tests that try to trick brix into skipping a rebuild it shouldn't** — and
  prove it doesn't.
- **Close the last corner of the delete-cleanup fix.** When you delete files, brix
  cleans up their leftover build outputs; it handles the normal case, but one rare
  situation (a build that crashed at just the wrong moment) it can't fully clean.
  Fixing it needs a small change to how brix remembers past builds — cheaper to do
  before the Rust rewrite locks things down.
- **Double-check symbolic links** — that a symlinked source file, or one whose
  target changes, is noticed.

**Extending brix:**

- **Use git to spot changes instantly.** Today brix checks files directly to see
  what changed; on a big repo, asking git "what changed since last time?" is much
  faster.
- **Write down the official ways to plug in.** There are three: where cached
  results are stored, how brix judges whether a change actually "matters," and how
  it detects file changes. Document them so other people can build on brix.
- **The "did the meaningful part change?" trick for Go and Protobuf.** brix already
  does this for Java and TypeScript (skip rebuilding users when only internals
  changed). Add it for Go and Protobuf when those languages show up in a project.
- **Support for building TypeScript/JavaScript** — a helper like the ones for C++
  and Java. Today you *can* build TS by running the compiler as a plain command,
  but there's no convenient helper. As part of this, track import dependencies:
  the TS version of the C/C++ header problem — a bundle depends on everything it
  imports, so editing a deeply-imported file should rebuild it. The machinery for
  this already exists (built for C/C++); TS just needs a different reader for its
  dependency format. Note: this is about *building* TS code — separate from the
  [Blocked] track-3 item of *writing build files* in TS. The interface-change
  detector for TS already exists.

**Bigger, but still Opus** (sketch the design first):

- **"Only build what my change touched."** Given a set of edits, work out which
  parts of a big project could be affected and build/test only those — not all 50
  services every time. The biggest time-saver for a company monorepo.

### Fable queue — for the careful model only

- **The scheduler + build layers of the Rust rewrite** (track 2), reviewed
  carefully, then switching brix to run on Rust by default. The single trickiest
  part of the project.
- **The always-warm Java build server** (track 1).

### Blocked or not yet decided

- **Fully replacing your Java/TS build tools with brix.** Blocked on two decisions
  from Duane: (a) wrap the existing builds first and replace piece by piece, or
  replace everything at once; and (b) whether brix also handles *downloading*
  third-party dependencies, or leaves that to an existing tool.
- **Two big future projects.** *Sandboxing*: run each build step so it can only
  see the files it declared — turning "you forgot to list a dependency" into an
  immediate error instead of a silent wrong build. This is the safety net that
  would automatically catch several of the correctness gaps above. *Spreading
  compilation across many machines* (remote execution). Both are large — either
  Opus as a big effort or Fable to review.
- **Pending:** Duane will hand over a real C++ project to set up a build file for.
  Header dependency tracking is already built and ready for it.

**Lower-priority ideas** (from the original spec): loading only part of a huge
project's build at a time (only needed at hundreds of services), fancier change
detection (a file-system watcher, or pulling change info from a remote server),
build inputs that come from memory or a database instead of files, and a C++
"compile-and-link in one step" helper.

## Completed

- **Glob exclusion via the `-` operator** (per `docs/content-key.md`, both
  engines): `data.glob("{name}.cc") - "*_test.cc"` (or a list). fnmatch
  right-hand side (`*` crosses `/`, `?`, `[...]`) matched against the
  workspace-relative path; `__sub__` returns an immutable copy; `find()`
  filters while `match`/`substitute`/planner overlap ignore excludes
  (conservative — an exclude only shrinks a set). Additive wire field
  `glob.exclude` (schema stays v1); joins fingerprints/identities **only when
  non-empty**, so every pre-exclude graph hashes byte-identically (no cache
  split), and editing the exclude list itself re-runs. Mirrored tests both
  engines.
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
  remaining Java gap (tests). Next gaps if wanted: annotation processors,
  and multi-module cross-suite selection (add jackson-databind).
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
  stages the exact POSIX library sources (list parsed straight from upstream
  `CMakeLists.txt`, symlinked so real edits propagate) — brix globs have no
  exclusion, so this is how you carve a library subset out of a tree that
  interleaves `*_test.cc` and a Windows port. brix then compiles all **38**
  translation units in parallel (`cpp.CompileSourceAction`), bundles them with
  `cpp.ArchiveAction` into `libleveldb.a` (`ar Dcrs`, 38 members), and links a
  smoke program (open a DB, batch-put, get, iterate) against the archive via
  `cpp.LinkApplicationAction` — run with `brix.py run smoke` (a build-then-act
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
  (`brix.py status` shows staged files/deletions and interrupted commits)
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
  and the old binary keeps working (atomic), `brix.py errors` replays the
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
  project's brix.py, rest to `cli.main(argv=...)`, flags become
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
  Verified on the animation project: `./brix.py build wealth` plans only
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
  three lines in its brix.py.
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
  second content-identity plugin, `ts-dts` — `data.file("api.d.ts",
  identity="ts-dts")` makes a `.d.ts` contribute its *declaration surface* to
  fingerprints, so a consumer recompiles on an interface change but not an
  implementation-only change or a doc/format edit. The ABI-pruning multiplier
  for the Java+TS monorepo half, and proof the content-identity seam is a real
  extension point (a second plugin, no engine change). Most of the win is
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
  `brix.py` updates rode along (uncommitted there): `{video:*}` on the
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

## Design rules

Settled — a fresh session should treat these as fixed unless Duane reopens
them:

1. **The logical graph is clean-build truth.** It never contains
   user-authored nodes for stale-output removal, failed-output deletion,
   cache lookup/restore, diagnostics replay, staging, commit, or recovery.
   Those are capabilities layered on during execution.
2. **Incremental behavior must match clean-build output.** An incremental
   build produces the same final output state a clean build would — which
   forces deleting outputs that no longer belong, deleting failed actions'
   outputs, and rebuilding downstream consistently.
3. **Layers are per-node middleware, not static plan transforms.** The plan
   grows during execution (ExpandAll), so a one-shot
   `ExecutionPlan → ExecutionPlan` rewrite misses runtime-expanded nodes.
   Instead: an `around_node` stack (innermost = process run) plus lifecycle
   events (`on_build_begin`, `on_collection_realized`, `on_build_end`).
   Stack order mirrors builder nesting: `atomic(cache(incremental(run)))`.
   Full design: `docs/build-layers.html`.
4. **Nodes are effectively immutable.** The plan is append-only (nodes and
   edges are only added, via the scheduler's single insertion channel); node
   definitions are frozen at insertion; node status is monotone
   (pending → running → done/failed/skipped). Expansion is refinement, not
   replacement; data realization is resolution (membership unknown → known),
   and the collection identity survives — it's what membership history
   attaches to across builds.
5. **Logical vs physical paths.** Declared inputs/outputs like
   `build/obj/foo.o` are logical names (used by inference, cache keys,
   membership diffs). Layers never rewrite them; `NodeContext`
   (`workspace_root`, `output_root`) maps logical → physical at render time.
6. **Cleanup is not user-authored.** Deleted-source and failed-output
   deletions are layer-proposed *real nodes*, inserted through the same
   channel as expansion. Failure-triggered cleanup uses an edge trigger
   mode: `on-success` (default) vs `on-completion`.
7. **Every shortcut is a result obtained elsewhere.** Up-to-date, cache hit,
   or process run all end in a `ProcessResult`; downstream can't tell the
   difference. Failed results are real results — cacheable, replayable, and
   they cascade normally.
8. **Cache and atomicity are independent.** Neither knows the other exists;
   atomic-outermost stack order alone routes cache-restored outputs through
   staging.
9. **One underlying model.** Typed language helpers (C++, Java, …) lower to
   the same generic action/data representation as `ExternalProcessAction`;
   raw and typed forms compose in one graph.
10. **Generated files must be representable before they exist**, and
    `ExpandAll` expands into schedulable graph nodes, not an opaque loop.
11. **The engine never prints.** Output is a client decision: the scheduler
    emits events to a `Reporter`, output styles are Reporter
    implementations, and results carry provenance (`process` / `replayed` /
    `cache`) so reporting can be honest without scheduling caring how a
    result was obtained. Silent + exit code is a first-class mode.
12. **Python owns user composition; Rust eventually owns the engine** —
    metadata, graph algorithms, scheduling, caching, fingerprints, and
    performance-critical execution live below the DSL boundary.

---

# Specification

Everything below is the reference spec (originally `freshStart.md`), updated
to match the current design.

## What Brix does

Users define build graphs in ordinary Python; the engine handles:

- content-hash-based incremental builds
- generated files
- stale output cleanup
- process result persistence
- cache lookup and restore
- stdout/stderr replay
- atomic/staged output commits
- graph visualization (future)
- status reporting

The same user graph must work unchanged under any capability combination:
clean, incremental, cached, atomic, or cached-atomic-incremental.

## Expected user file shape

A project usually has a `brix.py` with three independent responsibilities
(in any order):

1. **Capabilities** — which builder/layers are used.
2. **Dependency graph** — what data exists, what actions consume and
   produce. Clean-build truth.
3. **CLI** — parse project arguments, map aliases to targets, invoke the
   selected builder.

The target composition (builder nesting = layer stack order):

```python
def create_builder():
    change_detector = TimeAndHashChangeDetector(root=".")
    change_set = change_detector.detect_changes()

    return AtomicBuilder(
        CacheBuilder(
            IncrementalBuilder(
                change_set=change_set,
                graph_supplier=create_graph,   # callable, so the builder
            ),                                 # decides how much graph to load
            cache_dir="build/.brix-cache",
        ),
        staging_dir="build/staging",
    )
```

For a simple project that doesn't care about incremental builds:

```python
CleanBuilder(create_graph()).build()
```

Builders expose `plan() -> ExecutionPlan` and `build() -> BuildResult`.

## Logical graph model

The user declares a **logical graph** of two node families:

- **Data** — files, file collections, tools, literal values; later:
  in-memory values, database-backed sets.
- **Actions** — generic external processes, typed language actions,
  repeated-work expansion points.

A common chain is `data -> action -> data -> action -> data`, but a graph
may start with an action. The graph is not a parallel build script: it
declares what actions consume and produce, and the engine **infers**
dependency edges from data-set intersections.

## Data expressions

```python
data.file("src/main.cpp")        # one logical path (need not exist yet)
data.glob("src/{name}.cpp")      # path set with named bindings
data.regex(r"src/(?P<name>[0-9]+)\.cpp")   # for patterns glob can't express
data.tool("c++")                 # tool identity (later: path, version, …)
data.value("-O2 -std=c++20")     # literal data that affects outputs
```

- `glob` bindings are **recursive by default** — a bare `{name}` matches
  across `/` (like make's `%`), so `build/{rel}.class` matches both
  `build/App.class` and `build/com/foo/Bar.class`. Write `{name:*}` to pin a
  binding to a single path segment; `{name:**}` is the explicit recursive form.
  For anything richer than that, drop to `data.regex` and write a full regex.
- `value` exists so options/flags/env are hashable inputs without users
  creating JSON option files.
- Unions compose sets:
  `data.glob("src/{n}.cpp") + data.glob("build/generated/{n}.cpp")`.

Internally all path-like expressions lower into one interned `PathPattern`
representation (`src/brix/patterns.py`): template, binding names, compiled
matcher, literal prefix. Pattern-vs-pattern dependency inference is
conservative: provably disjoint → no edge; otherwise keep a potential edge
and resolve when a side realizes. Planned for scale: exact-path map,
prefix/segment tries, suffix buckets, matcher cache.

## Actions

### ExternalProcessAction

```python
ExternalProcessAction(
    name="compile {name}",            # optional; label falls back to command_line
    command_line="{compiler} {flags} -c {source} -o {object}",
    inputs={
        "compiler": data.tool("c++"),
        "flags": data.value("-O2"),
        "source": data.file("src/{name}.cpp"),
    },
    outputs={"object": data.file("build/obj/{name}.o")},
)
```

`command_line` is a template; placeholders refer to named inputs/outputs and
are rendered lazily (declared-but-unreferenced collections are fine). Every
run captures stdout, stderr, exit code, and termination signal as a
`ProcessResult`; results are persisted (with the incremental/cache layers)
and replayed on cache hits — cached failures replay their diagnostics and
fail the build the same way.

### Typed language actions (future)

Strongly typed, ergonomic helpers — no magic string keys:

```python
cpp.CompileSourceAction(
    source_files=data.glob("src/{name}.cpp"),
    object_files=data.glob("build/obj/{name}.o"),
    compiler=data.tool("c++"),
    compile_options=["-O2", "-std=c++20"],
)
cpp.LinkApplicationAction(
    object_files=data.glob("build/obj/{name}.o"),
    output_file=data.file("build/bin/app"),
    linker=data.tool("c++"),
)
java.JarAction(
    source_files=data.glob("src/main/java/{cls}.java"),
    output_file=data.file("build/libs/app.jar"),
    javac=data.tool("javac"), jar_tool=data.tool("jar"),
)
```

These lower to the same generic model, so raw and typed forms compose — the
proving example: a C++ tool is compiled and linked, runs to generate Java
sources, and a `JarAction` consumes
`data.glob("src/main/java/{cls}.java") + data.glob("build/generated/java/{cls}.java")`.
One graph, three kinds of action, no separate build systems.

### ExpandAll

```python
ExpandAll(
    element_name="name",
    collection=data.glob("src/{name}.cpp"),
    child=ExternalProcessAction(...),      # templated over {name}
)
```

`ExpandAll` is **not an opaque loop**. It sits in the plan as a single
unrealized node until its collection membership is known, then expands into
concrete sibling action nodes (`compile foo`, `compile bar`, …) that
schedule and run in parallel like any other node. The `ExpandAll` node
itself stays in the plan, marked done (see design rule 4).

## Generated files

The user writes a clean logical graph — a generator action whose output is
a glob, and an `ExpandAll` over that glob:

```python
graph.add(
    ExternalProcessAction(
        name="generate sources",
        command_line="{python} {generator} {csv} {out_dir}",
        inputs={...},
        outputs={"generated": data.glob("build/generated/{name}.cpp")},
    ),
    ExpandAll(
        element_name="name",
        collection=data.glob("build/generated/{name}.cpp"),
        child=ExternalProcessAction(...),
    ),
)
```

At plan time the collection and the `ExpandAll` are unrealized and the
downstream link is blocked. After `generate sources` runs, the collection
realizes, the `ExpandAll` expands, and downstream actions wait for the
concrete members. No user-declared "graph-expanding node" is needed. This
works today (see `tests/test_execution.py`).

## ExecutionPlan

The execution graph for one build invocation; may be partially realized.

Current implementation (`src/brix/plan.py`): nodes are **actions** —
`PROCESS` (concrete process) or `EXPAND` (unrealized expansion point) —
plus edges with a reason. Data expressions live on actions as declared
inputs/outputs; collections realize implicitly at expansion/render time.
(The original spec also listed realized/unrealized *data* as plan node
categories; whether data become first-class plan nodes is an open modeling
question — the ground rules in design rule 4 cover them either way.)

Current shape (per `docs/build-layers.html`):

```text
ExecutionPlan            # append-only
  nodes: [ExecutionNode] # definitions frozen at insertion
  edges: from -> to, reason, trigger: on-success | on-completion

NodeStateTable           # scheduler-owned
  node id -> status, result        # pending -> running -> done|failed|skipped
```

The scheduler owns readiness, failure cascade, and the single insertion
channel (used by both expansion and layer-proposed cleanup nodes). Nodes
execute and report results; they never mutate graph structure.

## Builders and capability layers

Root builders (`CleanBuilder`, `IncrementalBuilder`) lower the logical graph
into an `ExecutionPlan`. Capability layers (incremental, cache, atomic) are
**per-node middleware plus lifecycle events** — *not* plan-to-plan
transforms (design rule 3; full rationale and diagrams in
`docs/build-layers.html`):

```python
class BuildLayer:
    def on_build_begin(self, plan, ctx): ...          # atomic: staging/recovery
    def around_node(self, node, ctx, inner): ...      # skip / restore / redirect / run
    def on_collection_realized(self, node, members, ctx): ...  # membership diff
    def on_build_end(self, result, ctx): ...          # atomic: commit
```

- **CleanBuilder** — no change detection; may rebuild everything. Can still
  be wrapped by cache or atomic.
- **IncrementalBuilder** — takes a `ChangeSet` and a `graph_supplier`
  callable (so large projects can load only affected build modules).
  Degrades safely to clean behavior if metadata is missing; if metadata is
  missing but outputs exist, error and require explicit clean/adopt.
- **CacheBuilder** — transparent except for speed; cache key ≡ incremental
  fingerprint (one shared function over command template, tool identity,
  input hashes). Restores results and outputs, replays stdout/stderr,
  replays failures as failures.
- **AtomicBuilder** — outermost, so process outputs *and* cache-restored
  outputs go through staging. Conceptual staging:
  `build/staging/add/…` (files to add/replace) and `build/staging/del/…`
  (deletion markers). All work succeeded → commit; otherwise real outputs
  stay untouched and staged state remains inspectable for status/recovery.

## Incremental build semantics

Persistent metadata must include: file content hashes, cheap scan data
(mtime/size), action fingerprints (command/tool/options/env), discovered
dependencies, declared and extra owned outputs, generated-collection and
expansion membership, and previous process results.

The three defining scenarios:

- **Changed file** — `src/foo.cpp` changed → recompile `foo.o`, relink
  downstream.
- **Failed compile** — remove `build/obj/foo.o`, mark downstream invalid,
  remove/fail downstream outputs exactly as a clean build would have. Never
  keep a stale "last good" output the clean build wouldn't have produced.
- **Deleted source** — previous membership `{foo, bar, baz}`, current
  `{foo, bar}` → delete `build/obj/baz.o` and rebuild collection consumers.
  Requires persisted membership history; file hashes alone can never detect
  this. The deletions enter the plan as layer-proposed nodes (design
  rule 6).

## Change detection

Default: `TimeAndHashChangeDetector(root=".")` — workspace-relative paths,
timestamp/size as cheap scan, content hash when needed, ignores build
metadata directories, follows file symlinks as ordinary files, rejects
case-only path collisions. Future detectors: filesystem watcher, FUSE-like
live tracker, source-control based, remote metadata service.

## Modular build files

A graph-construction feature, orthogonal to incremental builds. Simple
projects: one `brix.py`, ordinary Python imports. Large projects (later):
`ChangeSet → DistributedGraphLoader → affected logical graph →
IncrementalBuilder`.

## CLI direction

Brix is primarily a library; projects may expose `brix.py`. CLI parsing
(`build`, `status`, `clean`, `graph`, alias→target mapping, project
arguments) stays out of `Graph` and builders.

## Class inventory

Superseded by [docs/class-diagrams.md](docs/class-diagrams.md), which
diagrams the engine's classes in `src/brix/` (grouped: data model,
actions, execution plan, builders & layers, support). Not yet in a
diagram: the JVM phase 1–2 additions (`identity.ContentIdentity` /
`JvmAbiIdentity`, `jvmabi`, `workers.WorkerSpec` / `Worker` / `WorkerPool`
/ `WorkerRegistry`), plus still-planned `ChangeSet` (a pluggable
change-detector abstraction — see TODO) and
`brix.cpp.CompileApplicationAction`. A `content identity` + `workers`
diagram group is a good small follow-up.

---

## Layout

The DSL layer (`src/brix/data.py`, `actions.py`, `graph.py`) is the
user-facing declaration API and stays in each host language. The engine
layer (`planner.py`, `plan.py`, `render.py`, `executor.py`, `process.py`)
consumes the logical graph and is the part scheduled to move to Rust; keep
the boundary between the two clean.

## Process notes

- Design changes: short back-and-forth discussion first; only edit docs or
  code after agreement.
- Duane is a visual guy — prefer diagrams; `docs/build-layers.html` is
  viewed directly in a browser.
- **Update this README at every commit** — move TODO items to Completed,
  reorder priorities, and record newly settled decisions.

## Quickstart

```sh
python3 -m pytest                        # run the test suite
python3 examples/hello_text/brix.py      # portable text-processing demo
python3 examples/raw_cpp/brix.py         # compile+link demo (needs c++)
python3 examples/layers_demo/brix.py     # watch the BuildLayer stack work
python3 examples/incremental_demo/brix.py  # build/rebuild/edit/delete-a-source
python3 examples/cli_demo/brix.py status   # project CLI: status/errors/build/clean
python3 examples/cpp_app/brix.py           # real C++, typed actions, all layers
python3 examples/cache_store_demo/brix.py  # pluggable/two-tier team cache across machines
```

A build file looks like:

```python
from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph

graph = Graph()
data = graph.data

graph.add(
    ExpandAll(
        element_name="name",
        collection=data.glob("src/{name}.cpp"),
        child=ExternalProcessAction(
            name="compile {name}",
            command_line="{compiler} {flags} -c {source} -o {object}",
            inputs={
                "compiler": data.tool("c++"),
                "flags": data.value("-O2 -std=c++20"),
                "source": data.file("src/{name}.cpp"),
            },
            outputs={"object": data.file("build/obj/{name}.o")},
        ),
    ),
    ExternalProcessAction(
        name="link app",
        command_line="{compiler} {objects} -o {app}",
        inputs={
            "compiler": data.tool("c++"),
            "objects": data.glob("build/obj/{name}.o"),
        },
        outputs={"app": data.file("build/bin/app")},
    ),
)

CleanBuilder(graph).build()
```
