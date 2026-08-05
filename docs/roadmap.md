# Roadmap

The one place for what's left and what's done. **Remaining work is at the top in
priority order; completed work is a flat list at the bottom.** Each line is one
plain-English item; where a thing has a design contract, the doc is linked — the
detail lives there and in the [development log](development-log.md), not here.

---

## Remaining — highest priority first

Two governing rules, in tension, and the order below resolves them:
- **Finish a capability before starting a new one** (we may lose the capable
  model at any time; brix must always be shippable).
- **But don't "finish" something a lower item will force you to undo or rewrite.**
  In particular: the authoring API is matured **by real use, not by decree** — a
  build file that needs manual workarounds is an API-gap ticket, not a reason to
  freeze; the formal v1 label is a near-the-end non-event. And the TS authoring
  API (track 3) *mirrors* the Python one, so it must come *after* that surface has
  stopped churning, or it re-mirrors every change.

### Tier A — close out the in-flight capability (test-impact) — **DONE**

Java, C++, and TS all select tests in real builds by **runtime discovery** (record
what each test actually used, re-run when that changes). Java was rebuilt from
static prediction to discovery, retiring the whole `plan_suite` subsystem and
making the coupled-monolith case selectable: on jackson-databind, where static
class-level selection skipped ~0%, discovery selects ~1.5% (edit one class → ~11
of ~700 tests). The C++ Windows gap (#43) is closed by construction:
`cpp.RunTestAction` execs the binary platform-correctly (no Unix-only `./`) and
`references_dl` detects `LoadLibrary`/`GetProcAddress` alongside `dlopen`
(unverified on Windows — no CI there). Details in
[test-impact-selection.md](test-impact-selection.md). The one **out-of-scope**
Windows item is compile-side: MSVC `cl.exe` header tracking (`/showIncludes` vs
`-MMD`), needed only for an MSVC (not GCC/Clang/MinGW) toolchain — not a
test-impact task.

### Tier B — mature the authoring API (by use, before track 3) — **DONE**

The authoring API matures by exhaustion of real gaps, not by decree. The one
named gap that drove this tier — animation's build fell back to a coarse
whole-engine dependency because there was no channel for *discovered* per-item
deps — is fixed and generalized: the JSON dependency manifest + `depfile=` now
gives per-item discovered deps, the mechanism that became brix's native manifest
format and `python.PytestAction`, proven across real builds (animation renders,
logicEngine's pytest suite, the TS wiring). A closing audit of the animation and
logicEngine build files found only cosmetic/ergonomic residue — no forced
workarounds; the two ergonomic candidates it surfaced are parked in Tier E. What
remains unbuilt in animation (test target, `.mp4` conversion) is deferred
migration *completion*, not an API gap. Env isolation phase 3 (the four-way `env`
vocabulary + scrub-to-allowlist) is also done; see Completed. This unblocks Tier
C, which mirrors this now-settled surface into TS. Guardrails stand for any future
change: **every API change proposed to Duane first**, and **keep generic classes
generic** (never leak a layer-specific or engine-internal detail onto a generic
`Action`/`Graph`/`Data`).

### Tier C — a JavaScript/TypeScript way to write build files (big track 3)

**Stage 1 — the generic core — DONE.** TS-brix can author and run any *skinless*
build (spell the commands out) with the full engine behind it. `rust/brix-node`
is the napi twin of `rust/brix-py` (async `build` returning a Promise, live
events via a threadsafe function — the FFI callback threading has been
**reviewed** for event completeness/ordering/lifecycle; custom
cache-store/identity callbacks are deferred, the env
sentinels are now implemented in the TS core, see Stage 2). The `ts/` package
mirrors the Python core (data, actions, graph,
lowering, builders, engine, reporters, cli) — three sanctioned deviations only
(camelCase, async `build`, plain-object reporters), plus method forms for the
operators TS can't overload (`.plus`, `.excluding`). Proven by a lowering oracle
(TS spec deep-equals Python's) and a real dogfood: `animation/brix_build.ts`
deep-equals `brix_build.py` and runs `graph`/`status`/`help` through the Rust
engine — even reading metadata a Python build wrote (cross-frontend, one wire,
one engine).

1. **Stage 2 — the language skins.** Mirror the per-language sugar in TS, each
   lowering to the same core actions Stage 1 proved, with a deep-equal skin
   oracle (author the same usage in Python + TS, lower, compare). Same guardrails
   as Tier B: **every API change proposed first**, generic classes kept generic.
   - **Done, oracle-proven:** `python` (PytestAction); `ts` (Compile/RunTest/
     Typedoc + the `ts-dts` content lens); `java` **core** (JarAction, Javadoc,
     the `jvm-abi` lens); `cpp` (Compile/Link/Archive/RunTest/Doxygen); `rust`
     (CargoBuildAction); `kotlin` **core** (JarAction spawn path + the
     `kotlin-abi` lens). Env sentinels (`SYSTEM`/`UNSET`/`PASS` + the
     `_brix_env_used` snapshot) were added to the TS core to support them. Each
     skin's oracle locks **every** branch (full-options *and* bare-defaults)
     byte-equal to the Python skin; `rust`/`kotlin` were built from a discrete
     confined packet by a cheaper model (ChatGPT) and reviewed. The shared
     **`DepSet`** (`.plus`/`.excluding` method forms, first-wins dedup, fnmatch
     exclude, the compile/link flag split) is done and threaded through its two
     non-gated consumers — `cpp` `deps=` and `kotlin` `classpath=` (spawn path) —
     oracle-proven byte-equal. (TS `java` `classpath=` rides the javac worker, so
     it lands with the deferred TS `java`-skin mirror — the asset atom it needed is
     now built; see below.) The **`ts.npm`** resolver (reads
     `node_modules`, filesystem-only) is done and threaded through
     `ts.CompileAction({ deps })` — proven against a fixture tree (own types,
     nested-`node_modules` skip, `@types` fallback, scoped-name mangling, dedup),
     which also closed the previously-untested `dependencies`→`types` path.
   - **The `asset` atom — BUILT (2026-07-31).** brix-shipped helpers
     (`JavacWorker.java`/`KotlincWorker.java`/`BootRepackager.java`, the `.py`
     recorders/wrappers) were referenced by absolute path — machine-specific, in
     the fingerprint, no cross-frontend form. Fixed with the `asset` atom
     (`{kind:"asset", name}`): **the engine owns the bytes**, fingerprinted by
     name + content, never the resolved path. Both engines route it (Rust
     `DataAtom::Asset` + fingerprint/render; Python mirror), proven by a
     differential golden oracle (byte-identical across engines) + incremental==clean
     (edit helper → re-stale) + a three-way digest match (sha256sum/Rust/Python).
     **All four Python skins migrated** off machine paths (java worker + JUnit
     recorder, kotlin/springboot/ts). Per-class Java test-impact now carries a
     machine-portable fingerprint from the Python frontend. Full design + status:
     [asset-engine.md](asset-engine.md). Dead duplicate helpers retired; installed
     wheels resolve helpers from the copy embedded in the `brix_py` extension
     (Option A — the engine owns the bytes), proven by `wheel_smoke.py`.
     **Remaining:** the TS-frontend skin mirrors — *deferred* (frontend parity; the
     real driver uses Python build files). A future TS `JUnitTestAction` port is
     unblocked: its `ToolData(sys.executable)` is *not* a wart (consistent with all
     tool fingerprinting); the fix is the established `python=`-param pattern already
     shipped in `python.PytestAction`/`ts.CompileAction` (add the same optional param
     to `java.JUnitTestAction`, the only one missing it; TS mirror passes it
     explicitly like PytestAction).
   - **Deferred (mechanical):** the *shell-out* resolver adapters —
     `java.pom`/`maven`/`gradle` (`mvn`/`gradle`) and `cpp.pkg_config`
     (`pkg-config`) — frontend-only path production, deep-equal-oracle-testable
     only where the external tool is present, so left until a project needs one;
     the atomic staging-hazard guard. (`springboot` and the java/kotlin *worker*
     paths are asset-gated, above.)

### Tier D — gated, deliberately not "do now"

2. **Delete the old Python engine internals (track 4c).** Finishes the Rust port,
   but **gated on real-world soak** (Duane's call) — the Rust default must run on a
   real project for a work stretch (days) first. Deletes the execution half, keeps
   the reference math (Python stays the golden-fixture oracle). One subtle ordering
   bug breaks builds → verification-heavy, line by line (differential oracle vs the
   Python reference + `ultrareview` audit). [rust-port.md](rust-port.md).
3. **Per-method test selection (test-impact increment 2) — largely obviated.**
   The motivation was that class-level selection was useless on coupled monoliths;
   runtime discovery *dissolved* that (it selects finely on databind — see Tier A),
   so going sub-class is now low-value. Keep parked unless a real suite shows
   class-level discovery is still too coarse; only then revisit (and the soundness
   kernel — coverage is a past-run record — is correctness-critical:
   verification-heavy, differential oracle + `ultrareview` audit).
4. **`brix.go` and Protobuf "did the meaningful part change?" skip.** Build Go by
   wrapping the `go` tool; add the interface-change skip — when a project using
   them appears.

### Tier E — small, safe, opportunistic

5. **Draw the build graph + more machine-readable output.** A visual graph
   (boxes/arrows) consuming the `status --json` document; plus `errors --json`.
6. **Document the three official extension points** (cache store, `content_key`,
    change detection) so others can build on brix.
7. **Smaller follow-ups.** A `build --verify-contents` CLI flag; C++ and Kotlin
    starter templates; a `pydoc`/`pdoc` doc-gen action + warm-daemon doc path; more
    "trick brix into a bad skip" oracles (generator-driven membership changes,
    multi-hop ABI chains); default git-detection on for git workspaces (only after
    the air-gapped trial). Plus two **parked authoring-API ergonomics** from the
    Tier B close (both real but optional — neither forces a workaround today, and
    adding surface right before Tier C mirrors it isn't worth the churn; build
    either only if a real project trips over it): (a) a `cli.main` **project-flags
    hook** so a build file declares its own flags (animation's `--lowRes` etc.)
    instead of hand-splitting argv before `cli.main`; (b) **derive CLI aliases from
    a graph glob binding** so alias names come from the build's own scan, not a
    second filesystem glob that can drift (animation's per-video aliases).
8. **Formal Python API v1 label (near the very end).** Once the surface is mature
    by use: cut `1.0.0` (semver on the distribution), lean on the
    `tests/test_public_api.py` contract, a `DeprecationWarning` cycle for breaks —
    and *don't* put the version in the import path. A labeling non-event, never a
    reason to work around a gap.

### Tier F — the onboarder (migrate an existing build onto brix)

The concrete migration path, and the highest-leverage remaining capability: the
real monorepo has **no AI on its network**, so the mechanical substitute for
AI-authoring build files is a tool that reads an existing build's *already-resolved*
model and emits a starting `brix_build.py`. Structured as a compiler-style split so
one backend serves every source tool: **dumpers** (frontends, one per build system)
→ a tool-neutral **IR** (brix's own graph shape + a per-node `fidelity` flag + a
tool-neutral `bridge` descriptor) → one **emitter** (backend). Because the IR is
shaped by brix's needs, not any one tool's, the expensive emitter is written once
and amortized across Gradle/Maven/CMake/Make.

- **Gradle dumper — DONE.** An injected init script (`brixDumpModel`, run at
  execution phase so classpath resolution is legal) transcribes Gradle's resolved
  project/task model to the IR. No build-file parsing — the multi-level indirection
  (subprojects, `buildSrc` conventions, variants) has already collapsed. Proven on
  spring-petclinic-kotlin (single-module, full classpaths) and junit5 (25 modules,
  2271 nodes, 109 cross-module edges); bespoke code-generation tasks are captured as
  bridge nodes carrying their declared I/O (`recognized:false`), with un-declared-output
  ones flagged `fidelity:verify`. `src/brix/onboard/gradle/` (`python -m brix.onboard.gradle`).
- **Emitter increment 1 (native JVM spine) — DONE.** Per module with a compile task,
  a native `java`/`kotlin` `JarAction` with classpaths **delegated to `java.gradle()`**
  (the sanctioned migration path — declarations stay in the build files). The
  generated petclinic build is structurally identical to the hand-authored
  `examples/spring-petclinic-kotlin/` bar exactly the two facts the IR cannot carry
  (`entry_point`, `spring_allopen`), which are emitted as explicit `# TODO`s + notes —
  the onboarder-surfaces-gaps loop (it already forced one dumper fix: Kotlin source
  dirs). `src/brix/onboard/emit.py` (`python -m brix.onboard.emit`).
- **Emitter increment 2 (bridge emission) — DONE.** Non-spine build-product tasks
  (custom generators, jib, javadoc, pom/metadata generation) are emitted as cached
  `ExternalProcessAction` bridges that re-invoke the source tool for exactly that
  target, keyed by coarse per-module inputs (source/resource dirs, never the build
  dir → no output→input cycle) with outputs captured file-or-directory (the
  JavaCompile `FileData(dir)` + recursive-glob idiom). Over-approximate inputs are
  safe (re-invoke more, never serve stale). Environment/IDE tasks (`Wrapper`,
  `UpdateDaemonJvm`, Eclipse/IDEA generation) are blocklisted, never run as build
  steps. Proven on petclinic (5 bridges) and live junit5 (its `generate*` tasks
  bridged).
- **Live-run proof (single-module) — DONE.** The full chain runs end-to-end on
  `spring-petclinic-kotlin`: dump → emit → (human fills only the two flagged TODOs,
  `entry_point` + `spring_allopen`) → `brix build` compiles 24 Kotlin sources into an
  executable-manifest jar (resources bundled), warm rebuild replays from cache, and
  the jar **boots the real Spring Boot app** (`Started PetClinicApplicationKt`, Tomcat
  + JPA + Spring Data up) — booting at all confirms `spring_allopen` took effect. This
  surfaced and fixed a toolchain gap: **`java.gradle()` now takes `java_installations=`**
  (forwarded as Gradle's own `-Porg.gradle.java.installations.paths`), so a build whose
  required JDK the machine can't auto-detect still resolves; the dumper already had the
  same knob. The emitter wires it from `$BRIX_JAVA_INSTALLATIONS` and bakes the portable
  toolchain *version* (captured into the IR) into a TODO; the JDK *path* stays an env
  var. Verified the `-P` is accepted by junit5's strict `build-parameters` plugin.
- **Cross-module wiring — DONE.** A dependent module now compiles against brix's own
  build of its siblings: for each `project_dep` brix also builds, the emitter adds that
  sibling's `build/brix/<slug>.jar` to the compile classpath (ABI-lensed by the skin —
  jvm-abi for Java, kotlin-abi for Kotlin) and subtracts Gradle's stale copy of the
  sibling's build output. Self-deps (Gradle can report a module as its own project-dep)
  and duplicates are dropped. A cross-module build switches off the atomic overlay
  (plain in-place `IncrementalBuilder`), since javac/kotlinc resolve a sibling jar via
  `-classpath` from the real filesystem and staging would hide it (the parked
  foreign-resolver limitation). Proven on a synthetic `core`←`app` build: brix builds
  `core`, compiles `app` against brix's `core.jar`, and the ABI edge is correct both
  ways (comment-only edit to `core` → `app` replays; a new public method → `app`
  recompiles). The proof also surfaced and fixed a dumper bug — it recorded
  Gradle-declared source/resource dirs that don't exist on disk (jarring a missing
  `src/main/resources` failed); the dumper now records only existing input dirs.
- **Spring Boot fat jar — DONE.** When a module runs the Boot plugin (a `bootJar`
  task), the emitter repackages its thin jar into an executable one via
  `springboot.SpringBootJar`, so `java -jar` alone boots it. loader-tools is pinned to
  the Spring Boot version read straight off the classpath (`spring-boot-<v>.jar`); the
  runtime classpath comes from Gradle; the `@SpringBootApplication` main class is the
  one human-supplied constant (env-overridable). Proven live on petclinic: a 76 MB fat
  jar (142 nested libs) boots with **no** `-classpath` (`Started PetClinicApplicationKt`).
9. **Onboarder — remaining increments.** Close the remaining **semantic gaps** (main
   class, compiler plugins) with detection or a prompt; the **generator→compile edge** —
   feed a bridged generator's output dir into the consuming module's sources/classpath
   (a generator reading a *sibling* module is still not caught); **coalesce concurrent
   bridge invocations** (many parallel `gradlew` calls contend on the daemon — batch or
   serialize); a **one-shot dump+emit** CLI; and, when a project needs it, **more
   frontends** (Maven/CMake/Make) emitting the same IR. A design doc is still to be
   written.

---

## Needs a decision from you

- **Replacing your Java/TS build tools with brix:** (a) wrap the existing builds
  and replace piece by piece, or replace all at once; and (b) does brix also
  *download* third-party dependencies, or leave that to an existing tool.
  *Current lean (discussed, not yet confirmed):* (a) **wrap-and-replace** — the
  onboarder + bridge let you migrate module-by-module with the incumbent build as a
  live differential oracle; (b) **delegate** — the emitter resolves classpaths via
  `java.gradle()` rather than brix downloading deps. Tier F is built around these;
  confirm or redirect.

---

## Nice-to-have / parked (no driver yet)

- **Resident daemon / warm graph.** Collapse the cold no-op floor (~3–4s at
  ~50-service scale) to instant by keeping the realized graph + watch resident.
  A mild annoyance, not urgent.
- **Filesystem-watcher change detection.** A pluggable `ChangeSource` so a no-op
  is O(changed) not O(all files). Correctness-critical invalidation path
  (verification-heavy: differential oracle + `ultrareview` audit); only pays off
  with the resident daemon; build the seam and
  the first real backend together, never seam-first. Design in the appendix.
- **Sandboxing + staged-overlay visibility (one mechanism).** Give each step a
  filesystem view of exactly its declared inputs (+ staged outputs): simultaneously
  enforces dependency hygiene (sandbox) and lets foreign resolvers — `tsc`, C
  `#include`, Go, protobuf — see staged outputs under atomic builds. Today those
  only work correctly on the plain incremental stack. Correctness foundation,
  platform-specific plumbing; design in the appendix (with a cheap interim "loudly
  refuse the unsafe combo" guard).
- **Remote execution.** Spread compilation across many machines. Large.
- **Lower-priority spec ideas.** Loading only part of a huge build at a time (only
  at hundreds of services), build inputs from memory/a database instead of files,
  a C++ compile-and-link-in-one-step helper.
- **Pending input (not a task):** you'll hand over a real C++ project to set up;
  header-dependency tracking is already built and ready for it.

---

## Completed

**Fast Java builds (track 1 — done, and beats Gradle).** ABI-aware skip (don't
recompile a library's users when only its internals changed) + warm compiler
processes that survive *between* builds via a per-worker holder daemon
(`worker_idle_timeout`). Benchmarked ~5–12× faster than Gradle 8.14 on JUnit 5's
edit-compile loop.

**Rust engine is the default (track 2 — done bar the 4c cleanup above).**
Change-detection math, planning, dependency inference, parallel scheduler,
incremental rebuilds, shared result cache, atomic (all-or-nothing) builds, the
Python↔Rust boundary, persistent workers, and the built-in `jvm-abi`/`ts-dts`
content identities all run in Rust and are cross-checked byte-for-byte against the
Python reference. `status`/`clean` and custom cache-store / identity plugins cross
as extension traits — nothing falls back to Python.

**Test-impact selection — wired for Java, C++, and TS in real builds, all by
runtime discovery.** Each test is its own action that records what it actually
used and re-runs only when that changes — C++ per binary (`-MMD` + link graph), TS
per file (import closure), Java per class (`-Xlog:class+load` → depfile of loaded
classes). Each proven against a real runner (gtest/vitest/JUnit) with a
teeth-checked equivalence oracle under an absolute "selection == full-run verdict"
guarantee. **Java was rebuilt from static prediction to discovery**, retiring the
`plan_suite`/provider subsystem: on jackson-databind — where static class-level
selection skipped ~0% (all paths run through a reflective `ObjectMapper`) —
discovery selects ~1.5% (edit one class → ~11 of ~700 tests, ~7 s; cold ~58 s,
24-wide parallel). Reflection and `@MethodSource` are caught for free (a loaded
class is a loaded class). `JvmFactory` (pre-warm pool) was evaluated and dropped —
the parallel scheduler already makes it fast enough and single-use JVMs get no JIT
reuse. See [test-impact-selection.md](test-impact-selection.md).

**Authoring API matured by dogfooding (Tier B — done).** Matured by exhaustion of
real gaps, not by decree. The driving gap — a build with no channel for
*discovered* per-item dependencies falls back to a coarse whole-engine dependency
(animation's original build) — is closed and generalized into brix's native JSON
dependency manifest + `depfile=`, then `python.PytestAction`, proven across real
builds. A closing audit of the animation and logicEngine build files found only
cosmetic/ergonomic residue, no forced workarounds; the two ergonomic candidates it
surfaced are parked (Tier E follow-ups). Guardrails stand for future change: every
API change proposed first, generic classes kept generic.

**Lazy monorepo loading ("only build what my change touched").** `brix.affected`
with `select_affected`/`load_affected`, index v2 (persisted per-package
patterns/footprints), gated by a full-edit-alphabet oracle. Opt-in; full load
stays the default. Design: [monorepo-scaling.md](monorepo-scaling.md).

**Bounded caches (both content-keyed layers).** The artifact cache and the
metadata ledger no longer grow without bound — **off by default** (a cache never
discards unasked), **safe by construction** (every entry is reproducible, so
eviction only ever forces a recompute, never a wrong result — the one-directional
soundness rule). *Artifact cache:* `CacheBuilder(max_bytes=, max_age=)` — TTL
then size cap, LRU by *use* (a hit refreshes the entry's mtime, coarsened; both
engines touch on restore, pruning is engine-agnostic directory maintenance).
*Metadata ledger:* `IncrementalBuilder(keep_builds=N)` — post-build compaction
stamps every key the current graph realizes (expanding `ExpandAll` through the
recorded memberships) with a build epoch in a `_seen` side map, then evicts
records unseen for `keep_builds` builds together with their discovered deps, plus
dead file-hash entries; no on-disk-format change and no Rust core/boundary change
(the extra keys ride the shared JSON's semantic round-trip). Proven on real
jackson-databind: the ledger halved (62→32 MB, 728 pre-migration orphans evicted)
with a subsequent cached rebuild running zero actions (nothing current evicted).

**Shared / remote cache (Nexus, Artifactory, S3, …).** `HttpCacheStore` — a
content-addressed `GET`/`PUT` backend (stdlib-only, pluggable auth hook), keyed by
the same machine-portable fingerprint, so one machine's compile is everyone's
cache hit. Production wiring is `TwoTierCacheStore(local front, remote back)`:
local hits skip the network; a remote hit is promoted into the local front; a down
remote degrades to a miss, never a failed build. Works identically through the
default Rust engine (the store crosses as the `cache_store` extension trait) and
the Python engine. Proven cross-machine at scale — a clean workspace with an empty
front reconstructs a many-target build entirely from the remote with zero
recomputation. Guide: [remote-cache.md](remote-cache.md). Remote *retention* is the
server's own cleanup policy; the local front is bounded by the eviction above.

**Languages & ecosystems.** Kotlin (single + multi-module, all-open plugin, warm
kotlinc worker); Spring Boot executable fat jars (`brix.springboot`); Rust via
cargo wrap (`brix.rust`, including brix self-hosting); annotation processing.
Dependency resolvers `java.pom`/`java.maven`, `cpp.pkg_config`, `ts.npm` over a
shared `DepSet`. Content lenses `jvm-abi`, `kotlin-abi`, `ts-dts`; `content_key`
refactor + glob `-` exclusion.

**Correctness hardening.** Git-based change detection on both engines;
`verify_contents` re-hash mode; symlink-repoint detection; the delete-cleanup
corner case; environment recording phases 1–3 — `env`/`env_used`, and the
scrub-to-allowlist isolation (`SYSTEM`/`PASS`/`UNSET` sentinels, per-language
default env maps, `scrub=True` on both engines), all in
[environment-recording.md](environment-recording.md).

**Usability & docs.** `status --json` (stable schema); `help` showing live config;
Java/TS starter templates; an offline MkDocs API site; user-project doc-gen actions
(`javadoc`/`typedoc`/`doxygen`); automatic TS import tracking.

**Real-project dogfoods** (each found a real bug or gap): JUnit 5, fmt, jackson
(core/annotations/databind), leveldb, spring-petclinic, spring-petclinic-kotlin,
the_silver_searcher, the polyglot Java+TS monorepo, and the end-to-end C monorepo.

---

## Appendix: parked design detail

Kept here (not deleted) because these two designs live nowhere else. They're below
the fold on purpose — you never have to read them to see the priorities above.

### Filesystem-watcher change detection

The concrete form of "collapse the no-op floor": a pluggable `ChangeSource` that,
given a build-owned token, reports only the paths changed since the last build, so
a no-op becomes **O(changed)** instead of O(all files) — git's `git status` speed,
by the same technique git uses.

- *Mechanism, not git.* The watching primitive is the OS (inotify / FSEvents /
  `ReadDirectoryChangesW`); backends are a native watcher (Rust `notify` crate),
  Watchman, or git's fsmonitor daemon. **No git repo, commit, or staging required**
  — the token is anchored to the last *build*, not to any VCS state (using
  `git status`/index-diff would be the wrong tool: it anchors to commits and would
  report the same dirty set every build). Works on Windows (git's built-in
  fsmonitor shipped Windows-first; and it matters *more* there, since Windows FS
  scanning is slow — the exact problem Microsoft's Scalar / VFS-for-Git solve).
  Rust `#[cfg]` handles the few platform-specific leaf functions (stat-identity
  fields); `std` + the `notify` crate cover the rest.
- *Why parked.* The payoff (~3–4s no-op → instant at 50-service scale) is a
  real-monorepo-onboarding win, not a today-problem. It is the **correctness-
  critical invalidation path** — a missed event silently ships a stale build, the
  exact failure the incremental-≡-clean oracles guard — so the *skipping* logic is
  correctness-critical (verification-heavy: differential oracle + `ultrareview`
  audit). And it only pays off with a **resident** watcher (a persistent
  daemon holding the watch + token), so it is coupled to the daemon lever.
- *How to build it (when it bites).* Introduce the `ChangeSource` seam **and the
  first real watcher backend together, in one increment** — not seam-first (the
  interface must be shaped by a real consumer: tokens, watch descriptors,
  overflow/resync semantics). Route the equivalence oracles *through* the change
  source so a dropped event fails a test, and keep a mandatory **full-scan
  fallback** for the cold start, a watcher crash, or an event-queue overflow. A
  seam with only the full-scan implementation is speculative abstraction with no
  payoff — don't land one in isolation.

### Staged-overlay visibility for foreign resolvers

**The gap.** The `AtomicLayer` writes a build's outputs to a staging tree
(`build/staging/add/<logical path>`, deletions under `del/`) and resolves brix's
*own* data atoms through an overlay that reads "the workspace as it will look after
commit." A step that reads an input brix handed it (a `FileData`, a `{classpath}`
substitution) therefore sees the staged version. But a subprocess that does its
**own** filesystem path resolution does not: `tsc` walking `import "../../x/dist/y"`,
a C compiler resolving `#include` of a header another step generated, Go resolving
an import, protobuf resolving an include — all call `open()`/`stat()` on the *real*
workspace, where this build's staged outputs are not. So cross-module builds through
a foreign resolver only work on the **plain incremental stack** (outputs written in
place), not under atomic staging. Java is immune only because brix substitutes the
overlay-resolved classpath into `{classpath}` explicitly; a tool brix can't feed the
path to hits the wall.

**The hazard it closes (why this is correctness, not polish).** A *first* atomic
build of cross-module TS fails **loud** ("cannot find module") — safe. The dangerous
case is an *incremental rebuild* where a prior committed `y.d.ts` already sits at the
real path: the foreign resolver reads the **stale committed** declaration (staging
invisible to it), type-checks against a stale API, can **succeed**, and the build
commits a silently stale output — the exact missed-edge failure the incremental-≡-clean
oracles exist to catch. So the atomic + cross-module + foreign-resolver combination
must be either *fixed* or *loudly refused*; it must never be silently allowed.

**Mechanism options (cheapest → airtight).**
- *Symlink farm / materialized merged root.* Before a step, build a directory
  mirroring the post-commit workspace (symlink real files, symlink or copy staged
  ones) and run the subprocess rooted there. Portable, no privileges. Weaknesses: a
  tool that calls `realpath` sees through the symlink and escapes the illusion to
  the un-staged path; per-step construction cost.
- *OS union filesystem.* Linux `overlayfs` (`lowerdir`=workspace, `upperdir`=
  staging/add, whiteouts for staging/del) mounted inside a per-process mount+user
  namespace (no root). Airtight, transparent even to `realpath`. Linux-only — macOS
  (no overlayfs; FUSE/bindfs or copy) and Windows (Projected FS, or a different
  scheme) need separate backends, a `#[cfg]` multi-backend effort like the
  filesystem-watcher.
- *Write-in-place + rollback journal.* Abandon copy-then-commit: write real paths,
  journal enough to undo on failure. Nothing to expose. **Rejected** — it weakens
  the atomicity guarantee (a crash mid-build leaves partial real outputs until
  recovery), which is the entire reason the atomic layer exists.

**Semantics the merged view must nail (the contract, before any code).** precedence
(staging/add wins over workspace); deletions (staging/del must *hide* a workspace
file — whiteout, not just "absent from add"); `realpath`/normalization (a tool must
not be able to resolve back to the un-staged path); symlinked workspace packages
(pnpm/yarn/npm workspaces symlink `node_modules/@scope/pkg` → the real package dir —
the view must compose with those); case-sensitivity (macOS/Windows); and read-only
enforcement of inputs. Route the incremental-≡-clean oracles **through** the merged
view so a precedence or whiteout bug fails a test.

**Converges with Sandboxing — build them once.** The parked *Sandboxing* project
(run each step so it can see only the files it declared) is the **same primitive
from the other side**: both are "construct and impose a filesystem view on a
subprocess." A view containing exactly {this step's declared inputs, staged outputs
included} *simultaneously* enforces declared-dependency hygiene (the sandbox) and
exposes staged outputs (the overlay). Do not ship two mechanisms.

**Why parked / when it bites.** Correctness-critical foundation under *every* atomic
build (verification-heavy: a wrong merged view is a silent wrong build — differential
oracle + `ultrareview` audit); platform-specific
plumbing for the airtight version; and no real driver yet to pin the semantics — real
TS services, not the `examples/typescript` toy, reveal the `node_modules`/symlink/
`realpath` requirements the view must satisfy. The plain incremental stack builds
cross-module TS *correctly* today; only atomic crash-safety is lost, so the practical
cost of waiting is low.

**Cheap interim guard (optional, do-now).** A targeted loud refusal/warning when a
foreign-resolver action with cross-module `dependencies=` (today `ts.CompileAction`)
runs *under a staging layer*, turning the silent-stale-on-rebuild path into an
explicit "use the plain stack here" error. Small, local, no merged-view machinery.

**How to build it (when it bites).** Design note first (this). Then the merged-view
**semantics contract above + one backend together** (start with the symlink farm for
portability, or commit to overlayfs if Linux-only is acceptable near-term), with the
**sandbox as co-consumer shaping the interface** so the two land as one mechanism.
Route the equivalence oracles through the view; keep the plain stack as the
always-available fallback.
