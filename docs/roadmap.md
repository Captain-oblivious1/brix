# Roadmap

**Priority:** the Rust engine is written and is the default, so the original
"lock down behavior in Python before the rewrite" phase is essentially over.
Remaining work is: finish the Rust track (phase 4c — delete the redundant Python
engine internals, on hold for soak), keep proving brix on real projects
(dogfooding), and the self-contained feature/correctness items below. New
behavior still lands in Python first and is mirrored into the Rust core under the
byte-identical-fingerprint rule.

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
   build server landed** (see the [development log](development-log.md),
   cross-build worker daemon): warm
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
   checkout without the compiled extension cannot build (development checkouts
   use an editable Maturin install; releases now ship the extension and worker
   launcher in a self-contained wheel). **[Fable]** — the trickiest part of the whole project; one
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
found generated-sources + shaded-dep needs), leveldb (static-library build
of 38 TUs via `cpp.CompileSourceAction` + `cpp.ArchiveAction`, linked + run
against a smoke program — see below), and **spring-petclinic** (a real Spring
Boot app: 30 sources compiled against a ~90-jar Maven-resolved classpath through
the warm worker, packaged, and — via the new `brix.springboot.SpringBootJar` —
repackaged into an executable fat jar that boots with bare `java -jar`; see
`examples/spring-petclinic/`). petclinic confirmed the design holds end to end
with **no engine changes**: brix consumes Maven's dependency resolution and owns
compile + package + incremental rebuild (a comment-only edit recompiles but does
*not* re-jar, since the class bytes are unchanged). Newest: **the_silver_searcher**
(`ag`) — a real C tool whose PCRE/zlib/lzma system-library deps resolve via
`cpp.pkg_config`; brix compiles its 12 TUs (win32 one dropped with glob `-`) and
links a working `ag 2.2.0` (`examples/the_silver_searcher/`). Newest
(2026-07-18): **polyglot Java+TS monorepo** (`examples/polyglot_monorepo/`) — a
synthetic stand-in for the access-gated air-gapped target, and the first dogfood
of a *mixed-language* workspace. A Java backend (`libs/model`, `services/orders`)
and a TypeScript frontend (`shared`, `app`) load into one `load_workspace`;
`verify_polyglot.py` proves lazy loading partitions across languages (a backend
edit loads no frontend package and vice versa; the ts-dts `shared → app` edge
still pulls in the dependent), building the Java slice for real. Guarded by
`tests/test_polyglot_monorepo.py` on both engines.

Completed: **multi-module cross-suite test selection** — the Jackson dogfood
now builds real 2.19.4 annotations, core, and databind jars plus one smoke suite
per module. Its reversible verifier proves that a core implementation change
reruns core and databind tests without recompiling databind or touching the
annotations suite. No engine changes were needed.

Also completed: **annotation processors** — `JarAction(processorpath=...)` passes a
separate processor path through the warm javac worker and fingerprints processor
jars by their complete bytes, rather than their ABI. The self-contained
`examples/annotation_processing/` dogfood and a JDK integration test prove that
an implementation-only processor change recompiles and regenerates the app.

**Kotlin modules — single-module: done.** `brix.kotlin.JarAction` compiles a
whole Kotlin module with kotlinc, adopts a Gradle KTS build's classpath through
`java.gradle(...)`, and reproduces Gradle's `kotlin("plugin.spring")` via
`spring_allopen=` — a correctness feature, since Kotlin classes are `final` by
default and Spring cannot proxy them otherwise. Proven on the
`spring-petclinic-kotlin` dogfood: it compiles the real module (non-`final`
beans confirmed in the bytecode), rebuilds incrementally with content-aware jar
skipping, and boots a full Spring context against the runtime classpath.

**Multi-module Kotlin composition — done, and it caught a live bug.** `app`
depends on `core`; brix infers the order from the shared jar and builds both.
The `examples/kotlin_multimodule/` dogfood surfaced that `kotlin.JarAction` was
ABI-lensing its classpath (inherited from the `java.JarAction` code it mirrors),
which is **unsound for Kotlin**: an `inline fun`'s body is copied into consumers'
bytecode, so an ABI-lensed dependency let brix *skip recompiling a dependent
after an inline-body change that actually altered its output* — a stale-result
bug, proven end-to-end. Fixed by **byte-tracking** the Kotlin classpath (sound;
recompiles on any dependency change). `tests/test_kotlin_multimodule.py`
(kotlinc-gated) pins the property; the example uses the plain incremental stack
because kotlinc self-resolves `-classpath` (the parked staged-overlay limitation).

**Multi-module Kotlin ABI skipping — done (2026-07-16, Fable): the
`kotlin-abi` lens.** Recovers the cross-module skip byte-tracking gave up.
Ground truth first: an inline-body edit changes consumers' bytecode, and so
does a *line shift alone* (three comment lines above an `inline fun` changed
the consumer — inlined copies carry their line-number tables for SMAP), while
the byte changes stay confined to the inline-carrying class. The lens follows
exactly: per class, jvm-abi counting (which already covers `const val` via
`ConstantValue` and hashes `@Metadata`, so declaration changes are seen)
unless the class's Kotlin metadata declares an inline function or inline
property accessor — then whole-class bytes; classes nested under a flagged
name (`$` prefix — lambdas/objects that inlining regenerates into consumers)
and anything the parser will not vouch for (unknown metadata kind/version,
legacy encodings, pre-1.0 fields) also count by bytes. **Fail closed: wrong
in the slow direction, never stale.** Metadata facts pinned from the Kotlin
compiler's own sources (BitEncoding UTF-8 mode; `Function.flags=9`/IS_INLINE
bit 10; `Property.getter_flags=7`/`setter_flags=8`/IS_INLINE_ACCESSOR bit 8).
Implemented in both engines (`src/brix/kotlinabi.py` reference,
`rust/brix-core/src/lenses/kotlinabi.rs`), golden-pinned on kotlinc-compiled
fixtures (`tests/fixtures/kotlinabi/`, 14 digests identical across engines);
`kotlin.JarAction`'s classpath flipped from byte union to the lens (zero
user-API change). The equivalence oracle (`tests/test_kotlin_multimodule.py`,
both engines): normal-body edit → dependent **replays** and equals a clean
rebuild byte-for-byte; inline edit and line-shift → dependent recompiles and
equals clean. Residual assumption for the air-gapped trial: inline is
kotlinc's only body-leak channel (reified requires inline; `const` is
ConstantValue) — a surprise there points here. Warm-worker reuse for kotlinc
is **done** (2026-07-17, Opus; see the Opus-queue entry below). (Spring Boot
fat-jars: **done** — see spring-petclinic.)

### Opus queue — ready to do now

These are self-contained and come with tests. No Fable needed.

**Correctness fixes** (holes found in the "what might we be missing?" review):

- **An optional "always re-check file contents" mode — done (2026-07-18, Opus).**
  To go fast, the detector assumes a file is unchanged when its size and mtime
  both match; a content change that leaves both unchanged (a same-size in-place
  edit that restores the mtime, or a tool that rewrites preserving timestamps) is
  missed. `verify_contents=True` (on `IncrementalBuilder` and `build_stack`)
  makes the default detector re-read and re-hash every file, catching it, at the
  cost of speed. Opt-in; it never changes the *returned* digest for an actually-
  unchanged file, so it stays fingerprint-compatible with the fast path (a
  no-op still replays). It configures the default detector and rides the boundary
  as a `verify_contents` wire flag, so the Rust engine's native detector honors
  it too (both hash paths skip the stat cache — and the git vouch — when set).
  `tests/test_verify_contents.py` pins that the fast path *misses* the collision
  and the mode *catches* it, on both engines. **Follow-up:** a `build
  --verify-contents` CLI flag for a one-off paranoid run (today it is a
  build-file/`build_stack` argument).
- **Record the environment a build used.** An environment variable or machine
  setting that a tool reads, but that the build file didn't declare, can change a
  build's result without brix noticing — the classic "works on my machine, fails
  on the server." **Phase 1 done (2026-07-17, Opus):** a build now captures its
  filtered environment to `build/.brix/environment.json`, and `brix.py env`
  diffs the current environment against the last recorded build (exit 1 on a
  difference), with a one-line note folded into `status`. A *detective, not a
  guard* — it records and shows, never rebuilds or fingerprints, so it can't
  bust a cache or slow a build; being Python-owned diagnostic state it needs no
  Rust/graph/wire change. A conservative ignore-list drops session noise
  (`PWD`, `SHLVL`, `SSH_*`, …) and keeps the signal (`PATH`, `JAVA_HOME`,
  `*FLAGS*`, …); `cli.main(..., env_ignore=[...])` extends it. **Phase 2 also
  done (2026-07-17, Opus):** `env_used=[...]` on an `ExternalProcessAction`
  promotes named variables to fingerprint material — the *guard* — so changing
  one re-stales the action (proven end-to-end on both engines). It reuses the
  `classpath_track` pattern: the named variables are snapshotted on the Python
  side into one fingerprint-only `ValueData` input, so only the resolved value
  crosses the boundary and **Rust needs no change**; a golden fingerprint case
  pins the cross-engine parity. It never *sets* the variable (unlike `env=`) or
  reaches the command; unset is distinct from empty. **Phase 2 crosses the
  fingerprint path, so it is queued for a Fable review** (reserved role, as with
  `content_key`). **Phase 3 struck as unsound (2026-07-18):** the old plan was to
  auto-*detect* which variables a tool read (`getenv` tracing, folded in like
  depfile headers), but env discovery is path-dependent — a variable read on one
  input isn't read on another, so a discovered set is only a lower bound and a
  changed-but-unread-this-time variable slips through as a stale cache hit. The
  depfile analogy fails because file discovery is self-healing (the source edit
  that adds an `#include` is itself a tracked change that forces re-discovery)
  while env has no such trigger (flipping a variable changes behavior with no
  tracked-file change). The sound replacement is **isolation, not detection**:
  scrub each action's environment to an explicit **allowlist**, so undeclared
  variables are simply absent and the failure mode flips from silent-wrong to
  loud-and-fixable. **Settled API shape (2026-07-18):** collapse `env=` (set) and
  `env_used=` (track) into a single `env` dict whose value carries the intent via
  sentinels — a literal *sets*, `SYSTEM` *takes it from the ambient environment*
  and fingerprints it, `UNSET` *withholds* it from that action — which deletes
  the `env_used` concept (and its awkward name) rather than renaming it. Portable
  (pure env manipulation, no `ptrace`), opt-in, post-trial. The implementation
  crosses the fingerprint path, so it is **reserved for a Fable review when
  built** (same gate phase 2 passed). See `docs/environment-recording.md`.

**Making Kotlin builds fast:**

- **Warm kotlinc worker.** **Done (2026-07-17, Opus).** A cold `kotlinc`
  reloads and re-JITs the entire Kotlin compiler on every call — a far bigger
  tax than `javac` — so `kotlin.kotlinc_worker(graph, kotlin_compiler=...)`
  keeps one alive and compiles in-process, the Kotlin analogue of
  `java.javac_worker`. The worker (`src/brix/_java/KotlincWorker.java`) is
  plain Java that reflects into `org.jetbrains.kotlin.cli.jvm.K2JVMCompiler`
  (so it bootstrap-compiles with plain `javac`, no Kotlin on the compile
  classpath; the compiler arrives on the launch classpath as
  `lib/kotlin-compiler.jar`), and its `exec(PrintStream, String…)` returns an
  `ExitCode` without `System.exit`, so it is safe to call in a loop.
  `JarAction(kotlinc_worker=…)` routes the compile through it; the classpath
  stays kotlin-abi-lensed, identical to the spawn path, because the worker is
  pure transport. It rides the same cross-build daemon as `javac` (crosses the
  Python↔Rust boundary; measured warm across builds), and the all-open
  plugin loads in-process. Note the compile-vs-launch JDK split: the worker is
  built with `--release 17` so it runs on whatever `java` launches it, which
  must be a JDK the Kotlin compiler supports. `tests/test_kotlin_worker.py`
  (unit + kotlinc-gated e2e, both engines); demonstrated in
  `examples/kotlin_multimodule/brix.py`.

**Making it easy to start:**

- **`brix.py help` — show the *live* configuration, not just the command list.**
  **Done (2026-07-17, Opus).** Argparse's `-h` lists the built-in subcommands,
  but everything project-specific lived in the `cli.main(...)` call and was
  invisible without reading the file. `help` now introspects the builder chain
  (walks each builder's `.inner`, reading wrapper attributes only — it never
  instantiates the layers or touches disk) and prints the active stack
  outermost-first with each layer's path (Atomic → Cache → Incremental), the
  target aliases, and the project's `commands=`. Whether staging is wired is
  reported both as a summary line and per-command (`commit`/`discard` are
  individually flagged when no `AtomicBuilder` is present). Both enablers
  landed: an optional description slot for aliases via `cli.Alias(target,
  description)` — a drop-in for a bare alias value, so existing
  `str`/`list[str]` mappings are untouched — and each `commands=` callable's
  first docstring line as its summary. Engine-agnostic (a pure client
  printout; identical under `BRIX_ENGINE=rust`). `tests/test_cli_help.py`;
  demonstrated in `examples/leveldb/brix.py`.
- **Draw a picture of the build.** Boxes for steps, arrows for what depends on
  what, instead of a text list.
- **Ready-made starter templates — done (2026-07-18, Opus).**
  `examples/templates/java-service/` (→ runnable jar via `java.JarAction`) and
  `examples/templates/typescript-service/` (→ `dist/*.js` via `ts.CompileAction`):
  minimal, generic, working projects meant to be *copied out*, each a `brix.py`
  on the recommended `build_stack` with a `build`/`run`/`status` CLI and one
  placeholder source. Each opens with a marked "delete in your copy" block (it
  only lets the template run in-repo) plus the always-needed `sys.path` fix a
  file named `brix.py` requires (the script's own dir would otherwise shadow the
  installed `brix` package). `tests/test_templates.py` runs a tool-free
  parse-and-plan check on both and a gated end-to-end build+run (Java verified
  here; TS gated on `tsc`). Listed in `examples/README.md`. **Follow-up:** a C++
  and a Kotlin template could join them.

**Building trust** (also hardens the parity between the two engines):

- **More tests that try to trick brix into skipping a rebuild it shouldn't** — and
  prove it doesn't. (Largely done — the `test_incremental_equivalence*.py` oracles
  now cover byte-lens edits bounded-exhaustively plus ABI-lens skips and discovered
  C headers, each checked against a clean rebuild. Remaining alphabet to add over
  time: generator-produced *membership* changes, and multi-hop ABI chains.)
- **Close the last corner of the delete-cleanup fix.** **Done (2026-07-17,
  Opus).** When you delete files, brix cleans up their leftover build outputs;
  it handled the normal case but leaked in one: a collection member *added by a
  build that then failed* (its output already on disk) was forgotten, because
  membership was committed only on success — so if that member's source later
  disappeared, its output was never cleaned incrementally (only a full `clean`
  caught it). Real only for unstaged builds; atomic mode wipes a failed build's
  staging before it reaches the workspace. The fix turned out smaller than a new
  metadata slot: on a failed build the membership record becomes the *union* of
  the last-known and this-build's realized members (never a replace), so an
  added-then-deleted member is still cleaned while a member pending deletion is
  retained until a successful build settles it (the retry invariant). One
  write-policy change in each engine's incremental `on_build_end`; no schema
  change. `tests/test_delete_cleanup.py` (both engines, unstaged fix + retry +
  atomic regression + the nested case the code's own docstring had called
  "unrecoverable").
- **Double-check symbolic links — done (2026-07-18, Opus).** A symlinked source
  was already stat'd *through* to its target, so a target **content** change
  (mtime/size move) was caught. The hole was a symlink **repointed** to a
  different file sharing the old target's `(mtime, size)` — a release-style
  `current -> vN` swap with mtimes preserved by `tar`/`rsync -a` — which collided
  and served the old target's cached hash (reproduced). Fix: fold the link target
  into the stat signature (`_symlink_target_sig`: first 8 bytes of sha256 of the
  readlink target, appended only when `is_symlink()`), so a repoint busts the
  cache. **Symlink-only**, so no regular-file signature or existing hash cache
  changes; **pure performance layer** (validates the stat cache, never a
  fingerprint, like the git detector), so no Fable gate. Mirrored in the Rust
  detector (`rust/brix-core/src/detector.rs`) under the byte-identical rule;
  `tests/test_symlink_detection.py` (repoint-with-colliding-stat rebuild,
  target-content-change rebuild, stable-target replay) runs on both engines.

**Extending brix:**

- **Use git to spot changes instantly.** **Reference done (2026-07-17, Opus);
  opt-in, Python-engine only for now.** The change detector is an injected seam:
  `IncrementalBuilder(..., detector=GitChangeDetector)` (or the same through
  `build_stack(detector=...)`) swaps the default stat detector for
  `brix.gitdetect.GitChangeDetector`, which wraps it — for a
  *tracked* file git reports unmodified since the last build, it trusts the
  stored hash without stat-ing it; untracked, ignored (the build's own
  intermediates), and out-of-repo inputs fall through to the stat/hash path. It
  is a pure performance layer — never changes a fingerprint — proven by an
  equivalence oracle (`tests/test_git_detect.py`) that replays an edit sequence
  under both detectors and asserts byte-identical rebuild decisions. Key design
  choices, all settled in discussion: git is the authority (tracked set via
  `git ls-files`, never a reimplemented `.gitignore`); "since last build" unions
  the working-tree diff with `git diff <baseline>..HEAD` (baseline = the
  recorded last-build HEAD), so commits between builds are caught and the user
  never has to commit; degrades to the stat scan when not a git repo / no
  baseline / any git call fails. Inherits git's fsmonitor speedup for free when
  the user has it enabled (no sudo, no Watchman dependency). **Hardened
  (2026-07-18, Fable):** the vouch had a revert-staleness hole — a file dirty
  at build N is hashed with content git never saw, so reverting it to HEAD
  before build N+1 made git call it clean while the cached hash was stale. The
  baseline is now persisted *paired with that build's working-tree dirty set*;
  a previously-dirty file is never vouched. Same commit added
  `changed_paths()`, the graph-independent "what changed since the last build"
  enumeration the lazy-loading tier consumes (None whenever git can't answer —
  the caller loads everything). **Rust mirror done (2026-07-18, Opus):**
  `rust/brix-core/src/gitdetect.rs` ports the vouch (same baseline+dirty
  metadata keys, same degrade-to-stat rules), folded into
  `TimeAndHashChangeDetector` as an optional mode rather than a trait refactor.
  Configuring `detector=GitChangeDetector` now accelerates the **default (Rust)
  engine** too: `IncrementalBuilder.layer_specs` emits a `git_detection` wire
  flag (the detector object can't cross the boundary; the intent does). Proven
  by the equivalence oracle run on *both* engines plus a guard test that the
  native path is actually engaged (only it writes the baseline). **Remaining
  follow-up:** once soak-proven against real repos, consider defaulting it on
  for git workspaces — but *not before the air-gapped trial* (the boring stat
  detector is the safe floor there).
- **Write down the official ways to plug in.** There are three: where cached
  results are stored, how brix judges whether a change actually "matters," and how
  it detects file changes. Document them so other people can build on brix.
- **Generate HTML documentation — first version done (2026-07-18, Opus).**
  A MkDocs + Material + **mkdocstrings** site (`mkdocs.yml`, `docs/site/`): a
  hand-written landing page (purpose + tenets, distilled from the spec's design
  rules) plus an **API reference generated from the source docstrings**, so it
  can't drift from the code. Scoped to the **public authoring API only** — the
  curated `__all__` on each public module is the boundary, so the generator
  renders exactly the user-facing surface and none of the engine internals or
  private helpers. Builds fully offline (`docs/requirements.txt`; no CDN), so it
  works on the air-gapped machine. A prebuilt copy is committed under `site/`
  for review (open `site/index.html`); can revert to build-on-demand once the
  shape is settled. **Follow-ups / advice-and-consent:** (a) *done* — the three
  RST role refs (`:class:`/`:meth:`) that rendered literally are normalized to
  Markdown; the remaining ``double-backtick`` markup renders correctly as code
  spans, so no further pass is needed unless we want richer cross-references;
  (b) the conceptual layer is deliberately one tenets page for now — it could
  grow guides (the graph/action model, incrementality, lenses, hierarchical/lazy
  loading) if wanted; (c) the same **TypeDoc** treatment for the TS authoring
  API once that API exists.
- **The "did the meaningful part change?" trick for Go and Protobuf.** brix already
  does this for Java and TypeScript (skip rebuilding users when only internals
  changed). Add it for Go and Protobuf when those languages show up in a project.
- **Generate API docs for the *user's* project as a build step — done
  (2026-07-18, Opus).** brix now ships typed actions that run the ecosystem's
  doc generator over a project's own sources as ordinary, tracked, incremental
  steps: `java.JavadocAction` (`javadoc`), `ts.TypedocAction` (`typedoc`), and
  `cpp.DoxygenAction` (`doxygen`). Each declares the sources as inputs (so the
  doc set rebuilds only when a source changes and replays otherwise) and its
  whole output tree as one `FileData` dir + recursive `{name}` glob (so it
  stages atomically and caches like any other output). `JavadocAction` also
  takes a `classpath` (abi-lensed, so an impl-only dependency change replays);
  `DoxygenAction` is config-driven (`doxygen <Doxyfile>`), declaring the
  `Doxyfile` and `output_dir` explicitly since brix can't read them out of the
  config. Distinct from brix's *own* documentation (the "generate HTML
  documentation" item below); this is doc generation offered *to* the projects
  brix builds. All three are curated into the modules' `__all__`, so they appear
  in the public API site automatically. Lowering-shape tests plus a real-`javadoc`
  end-to-end incremental test in `tests/test_docgen.py`. **Follow-up:** a
  `pydoc`/`pdoc` action for Python and a warm-daemon path are still open, and no
  classpath/tool-path resolution is threaded in v1 (users pass tool flags via
  `options=`).
- **TypeScript: automatic import-dependency tracking — done (2026-07-18,
  Opus).** A `ts.CompileAction` already tracked *declared* cross-module deps
  (another module's built `.d.ts`, ts-dts-lensed); it now also discovers the
  *undeclared* import graph, closing the stale-build hole where a module imports
  a shared `.ts` outside its `sources` glob (the TS version of the C/C++ header
  problem). Default-on (`track_imports=True`, like C++'s always-on `-MMD`): brix
  wraps `tsc` in a `--listFiles` recorder (`src/brix/_ts/tsc_listfiles.py`) that
  writes a **Makefile depfile** of the workspace `.ts`/`.d.ts` the compile read,
  so it flows through the *existing* depfile channel — **no engine change**, and
  proven identical on both engines. The recorder excludes this module's own
  `out_dir` and each declared dependency's directory, so declared `.d.ts` stay
  governed by the ts-dts lens (byte-tracking them in the depfile would defeat
  the impl-only-change replay). Gated real-tsc test plus a self-contained
  fake-tsc integration test (`tests/test_ts_import_tracking.py`) that runs on
  both engines. Separate from the [Blocked] track-3 item of *writing build
  files* in TS. **Follow-up:** an `ts.npm`/`node_modules` resolver is still
  planned (docs/dependencies.md).

**Bigger, but still Opus** (sketch the design first):

- **"Only build what my change touched."** Given a set of edits, work out which
  parts of a big project could be affected and build/test only those — not all 50
  services every time. The biggest time-saver for a company monorepo. **All
  three tiers implemented** (tiers 1–2 Opus 2026-07-17; tier 3 — the
  lazy-loading completeness core — Fable 2026-07-18: `brix.affected` with
  `select_affected` + `load_affected`, index v2 persisting per-package
  patterns/footprints, gated by `tests/test_affected.py`'s full edit alphabet
  plus a negative control). **Opt-in**: `load_workspace` full load stays the
  default; relying on lazy loading is post-trial. **Design note:
  `docs/monorepo-scaling.md`** — hierarchical `brix.py` files (a Bazel-style
  package model, monolith as the degenerate case), a *derived* reverse-dependency
  index + loading-footprint tracking (no redundant declarations), and lazy
  loading gated by an equivalence oracle. Key findings: the value is lopsided —
  lazy *loading* (skipping graph construction) is the real win; and selection
  must be pattern-based, not ownership-based (shared out-of-subtree inputs make
  "nearest owner" under-select — see the design note's correction).

### Fable queue — for the careful model only

The two original Fable items — the Rust scheduler/build layers (track 2) and the
always-warm Java build server (track 1) — have **both landed** (Rust is the
default engine; the worker daemon is done and benchmarked). What remains for the
careful model:

- **Phase 4c — delete the Python engine internals** (track 2 tail), on hold for
  soak. A line-by-line review that the Rust engine fully covers the execution
  half before the Python fallback is removed; one subtle ordering gap here breaks
  builds. Deletion boundary spelled out in track 2 above.
- **Review pass on `env_used=` (environment-recording phase 2)** — **done
  (2026-07-18, Fable), verdict positive.** All four scoped checks hold: (a)
  unset-vs-empty is deterministic and engine-identical (canonical sorted JSON,
  `null` ≠ `""`); (b) the snapshot never reaches the process environment or
  the command (probe test proves tracking ≠ injection); (c) the
  verbatim-through-`substitute` copy holds under nested expansion; (d) the
  golden genuinely exercises the path (stored spec carries `_brix_env_used`,
  Rust re-derives the pinned `58ec081e…` fingerprint). One hole found and
  closed: idempotence keyed on the reserved input key's *presence*, so an
  author-supplied `_brix_env_used` silently replaced the live snapshot with a
  constant — a guard bypass in the stale direction. Now: supplying the
  reserved key raises, and `substitute` threads the frozen snapshot through a
  private parameter. Fingerprints byte-identical (golden regen: zero diff).
  Miss-only note: a non-UTF-8 env value (surrogate-escaped) fails loudly at
  the boundary rather than fingerprinting silently.
- **Review pass on the `content_key` relocation** (commit `634c115`) — **done
  (2026-07-17, Fable), verdict positive.** The byte-identity claim holds: the
  golden diff proves digests unchanged across the `identity`→`content_key`
  rename (the wire field name is not fingerprint material — only the versioned
  tag is), the 3-element byte-hash vs 4-element lens material shapes cannot
  collide, digest caching keys on the versioned tag plus the shared stat
  signature, and substitution preserves the lens in both engines. One gap
  found and closed (commit `b58dd6a`): no fingerprint golden pinned the
  `kotlin-abi@1` tag across engines — added, plus a refresh of cpp golden
  specs that had gone stale after the `deps=` feature (parity never broke;
  golden.rs replays stored specs). Miss-only, non-blocking notes: failing
  lens parses are not cached (perf only); Python `identity.register()` can
  silently replace a builtin; a one-sided lens version bump diverges the
  engines toward rebuilds, never staleness.

### Blocked or not yet decided

- **Fully replacing your Java/TS build tools with brix.** Blocked on two decisions
  from Duane: (a) wrap the existing builds first and replace piece by piece, or
  replace everything at once; and (b) whether brix also handles *downloading*
  third-party dependencies, or leaves that to an existing tool.
- **Two big future projects.** *Sandboxing*: run each build step so it can only
  see the files it declared — turning "you forgot to list a dependency" into an
  immediate error instead of a silent wrong build. This is the safety net that
  would automatically catch several of the correctness gaps above. **Unify with
  the staged-overlay design note** (Performance next steps → "Parked design:
  staged-overlay visibility for foreign resolvers"): imposing a per-step
  filesystem view is the *same primitive* as exposing staged outputs to a foreign
  resolver — build them as one mechanism. *Spreading compilation across many
  machines* (remote execution). Both are large — either Opus as a big effort or
  Fable to review.
- **Pending:** Duane will hand over a real C++ project to set up a build file for.
  Header dependency tracking is already built and ready for it.
- **Declare v1 of the Python API (discussion — decide, may do nothing yet).**
  Duane wants to eventually stamp the authoring API as stable. Open question was
  whether to version the *import path* (`brix.cpp.v1`, later `brix.cpp.v2`).
  Recommendation on the table (not yet decided): **don't** put the version in the
  path. Two versions of the authoring API can't meaningfully coexist in one
  process anyway (one process links one core + one Rust engine), so an in-path
  version buys no real parallelism while forcing `.v1` into every build file's
  imports — the exact churn a migration should avoid. Per-language versioning is
  worst (all helpers share one core, so a pinned `cpp.v1` on a v2 core is
  incoherent). Instead: cut `1.0.0` (semver on the distribution), freeze the
  top-level contract (already pinned by `tests/test_public_api.py`), document a
  stability policy in `docs/python-api.md`, and handle breaks with a
  `DeprecationWarning` cycle → major bump. Keep explicit versioning where it
  belongs: the Python↔Rust wire spec (`SPEC_VERSION`, already there). The 50-service
  incremental migration is served by deprecation shims + per-env version pinning,
  not parallel import trees. Decision is Duane's; capture-only for now.

**Lower-priority ideas** (from the original spec): loading only part of a huge
project's build at a time (only needed at hundreds of services), fancier change
detection (a file-system watcher, or pulling change info from a remote server),
build inputs that come from memory or a database instead of files, and a C++
"compile-and-link in one step" helper.

### Performance next steps (from the scaling benchmark)

Client build runtime is the metric that matters (brix's own build/test time is
irrelevant by comparison). `examples/scaling_benchmark.py` gave the first
numbers (release engine). One lever falls out, and it is milder than first feared:

- **A resident daemon / warm graph**, eventually, to collapse the cold no-op
  floor. At ~50-service scale (~10–15k nodes) a cold no-op extrapolates to only
  **~3–4s** (linear at ~265 µs/node on the release Rust engine), plus a ~0.26s
  cold-CLI tax. That is a mild annoyance, not a painful one — a **nice-to-have**,
  not the big scaling win it first looked like. Every `brix build` today
  re-realizes globs, re-expands, and re-checks freshness cold with no filesystem
  watch; a daemon would erase that, but it is not urgent.

- **Filesystem-watcher change detection (parked — design captured below).** The
  concrete form of "collapse the no-op floor": a pluggable `ChangeSource` that,
  given a build-owned token, reports only the paths changed since the last build,
  so a no-op becomes **O(changed)** instead of O(all files) — git's `git status`
  speed, by the same technique git uses.
  - *Mechanism, not git.* The watching primitive is the OS (inotify /
    FSEvents / `ReadDirectoryChangesW`); backends are a native watcher (Rust
    `notify` crate), Watchman, or git's fsmonitor daemon. **No git repo, commit,
    or staging required** — the token is anchored to the last *build*, not to any
    VCS state (using `git status`/index-diff would be the wrong tool: it anchors
    to commits and would report the same dirty set every build). Works on Windows
    (git's built-in fsmonitor shipped Windows-first; and it matters *more* there,
    since Windows FS scanning is slow — the exact problem Microsoft's Scalar /
    VFS-for-Git solve). Rust `#[cfg]` handles the few platform-specific leaf
    functions (stat-identity fields); `std` + the `notify` crate cover the rest.
  - *Why parked.* The payoff (~3–4s no-op → instant at 50-service scale) is a
    real-monorepo-onboarding win, not a today-problem. It is the **correctness-
    critical invalidation path** — a missed event silently ships a stale build,
    the exact failure the incremental-≡-clean oracles guard — so the *skipping*
    logic is Fable-territory. And it only pays off with a **resident** watcher
    (a persistent daemon holding the watch + token), so it is coupled to the
    daemon lever above.
  - *How to build it (when it bites).* Introduce the `ChangeSource` seam **and the
    first real watcher backend together, in one increment** — not seam-first (the
    interface must be shaped by a real consumer: tokens, watch descriptors,
    overflow/resync semantics). Route the equivalence oracles *through* the change
    source so a dropped event fails a test, and keep a mandatory **full-scan
    fallback** for the cold start, a watcher crash, or an event-queue overflow.
    A seam with only the full-scan implementation is speculative abstraction with
    no payoff — don't land one in isolation.

(An earlier "Rust executor is ~5× slower on clean builds" note was withdrawn — it
was a *debug*-build artifact; on the release engine Rust matches or beats the
Python engine on clean builds and is ~10× faster on no-ops. See the development
entry. Lesson baked into the benchmark: measure the release `.so`.)

### Parked design: staged-overlay visibility for foreign resolvers

**The gap.** The `AtomicLayer` writes a build's outputs to a staging tree
(`build/staging/add/<logical path>`, deletions under `del/`) and resolves brix's
*own* data atoms through an overlay that reads "the workspace as it will look
after commit." A step that reads an input brix handed it (a `FileData`, a
`{classpath}` substitution) therefore sees the staged version. But a subprocess
that does its **own** filesystem path resolution does not: `tsc` walking
`import "../../x/dist/y"`, a C compiler resolving `#include` of a header another
step generated, Go resolving an import, protobuf resolving an include — all call
`open()`/`stat()` on the *real* workspace, where this build's staged outputs are
not. So cross-module builds through a foreign resolver only work on the **plain
incremental stack** (outputs written in place), not under atomic staging. Java is
immune only because brix substitutes the overlay-resolved classpath into
`{classpath}` explicitly; a tool brix can't feed the path to hits the wall.
"Exposing the overlay to subprocesses" = making the child's own reads see the
merged (workspace + staging) view, so `stat("x/dist/y.d.ts")` transparently
returns the staged file.

**The hazard it closes (why this is correctness, not polish).** A *first* atomic
build of cross-module TS fails **loud** ("cannot find module") — safe. The
dangerous case is an *incremental rebuild* where a prior committed `y.d.ts`
already sits at the real path: the foreign resolver reads the **stale committed**
declaration (staging invisible to it), type-checks against a stale API, can
**succeed**, and the build commits a silently stale output — the exact
missed-edge failure the incremental-≡-clean oracles exist to catch. So the
atomic + cross-module + foreign-resolver combination must be either *fixed* or
*loudly refused*; it must never be silently allowed.

**Mechanism options (cheapest → airtight).**
- *Symlink farm / materialized merged root.* Before a step, build a directory
  mirroring the post-commit workspace (symlink real files, symlink or copy staged
  ones) and run the subprocess rooted there. Portable, no privileges. Weaknesses:
  a tool that calls `realpath` sees through the symlink and escapes the illusion
  to the un-staged path; per-step construction cost.
- *OS union filesystem.* Linux `overlayfs` (`lowerdir`=workspace, `upperdir`=
  staging/add, whiteouts for staging/del) mounted inside a per-process
  mount+user namespace (no root). Airtight, transparent even to `realpath`.
  Linux-only — macOS (no overlayfs; FUSE/bindfs or copy) and Windows (Projected
  FS, or a different scheme) need separate backends, a `#[cfg]` multi-backend
  effort like the filesystem-watcher.
- *Write-in-place + rollback journal.* Abandon copy-then-commit: write real paths,
  journal enough to undo on failure. Nothing to expose. **Rejected** — it weakens
  the atomicity guarantee (a crash mid-build leaves partial real outputs until
  recovery), which is the entire reason the atomic layer exists.

**Semantics the merged view must nail (the contract, before any code).**
precedence (staging/add wins over workspace); deletions (staging/del must *hide*
a workspace file — whiteout, not just "absent from add"); `realpath`/normalization
(a tool must not be able to resolve back to the un-staged path); symlinked
workspace packages (pnpm/yarn/npm workspaces symlink `node_modules/@scope/pkg` →
the real package dir — the view must compose with those); case-sensitivity
(macOS/Windows); and read-only enforcement of inputs. Route the
incremental-≡-clean oracles **through** the merged view so a precedence or
whiteout bug fails a test.

**Converges with Sandboxing — build them once.** The parked *Sandboxing* project
(run each step so it can see only the files it declared) is the **same primitive
from the other side**: both are "construct and impose a filesystem view on a
subprocess." A view containing exactly {this step's declared inputs, staged
outputs included} *simultaneously* enforces declared-dependency hygiene (the
sandbox) and exposes staged outputs (the overlay). Do not ship two mechanisms —
the overlay-visibility fix and sandboxing are one piece of infrastructure, and
that is the strongest reason to do it once, foundationally, rather than bolt a
TS-only workaround onto the atomic layer.

**Why parked / when it bites.** Correctness-critical foundation under *every*
atomic build (Fable-territory: a wrong merged view is a silent wrong build);
platform-specific plumbing for the airtight version; and no real driver yet to
pin the semantics — real TS services, not the `examples/typescript` toy, reveal
the `node_modules`/symlink/`realpath` requirements the view must satisfy. The
plain incremental stack builds cross-module TS *correctly* today; only atomic
crash-safety is lost, so the practical cost of waiting is low.

**Cheap interim guard (optional, do-now).** Independent of the full fix: a
targeted loud refusal/warning when a foreign-resolver action with cross-module
`dependencies=` (today `ts.CompileAction`) runs *under a staging layer*, turning
the silent-stale-on-rebuild path into an explicit "use the plain stack here"
error. Small, local, no merged-view machinery — closes the hazard while the
overlay stays parked.

**How to build it (when it bites).** Design note first (this). Then the
merged-view **semantics contract above + one backend together** (start with the
symlink farm for portability, or commit to overlayfs if Linux-only is acceptable
near-term), with the **sandbox as co-consumer shaping the interface** so the two
land as one mechanism — not a seam plus a TS-only path. Route the equivalence
oracles through the view; keep the plain stack as the always-available fallback.
