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

### Tier B — finish + mature the authoring API (by use, before track 3)

1. **Environment isolation (env recording, phase 3).** Fully spec'd: one `env`
   dict with a four-way vocabulary (pin / inherit+fingerprint / inherit-only /
   withhold) that scrubs each step's environment to an allowlist, so an undeclared
   variable is *absent* instead of a silent wrong build. Each `brix.<language>`
   ships a default map naming the latent traps. Reshapes the `env` surface
   (collapses `env=`/`env_used=`), so it lands **before** track 3. Crosses the
   fingerprint path → **Fable review** after build. Spec:
   [environment-recording.md](environment-recording.md).
2. **Mature the authoring API by real-project dogfooding.** A deliberate pass
   where every manual workaround in a real build file becomes an API-gap *fix*
   (add the missing simple feature), not a workaround. Start with the animation
   project (`~/Projects/animation`), whose build file was written with manual
   logic around a missing feature. This *is* finishing the authoring-API
   capability, and it's the honest gate before mirroring it in TS. **Not a
   freeze** — stable by exhaustion of real gaps. Two guardrails: **every API
   change is proposed to Duane first**, and a fix must **keep generic classes
   generic** — never leak a layer-specific or engine-internal detail onto a
   generic `Action`/`Graph`/`Data` to make an implementation easier.

### Tier C — the big new capability, now rewrite-safe

3. **A JavaScript/TypeScript way to write build files (big track 3).** Same
   features as the Python API, authored in TS. Unblocked (the Rust engine is the
   default and carries build/status/clean + extension traits); the binding is
   `rust/brix-py` transliterated over the same JSON boundary, the TS API layer
   mechanical against [boundary-schema.md](boundary-schema.md). Sequenced after
   Tier B so it mirrors a matured surface. Opus-sized, **Fable review** of the FFI
   callback threading. (Accept minor re-mirror churn over a frozen bad API.)

### Tier D — gated, deliberately not "do now"

4. **Delete the old Python engine internals (track 4c).** Finishes the Rust port,
   but **gated on real-world soak** (Duane's call) — the Rust default must run on a
   real project for a work stretch (days) first. Deletes the execution half, keeps
   the reference math (Python stays the golden-fixture oracle). One subtle ordering
   bug breaks builds → **Fable**, line by line. [rust-port.md](rust-port.md).
5. **Per-method test selection (test-impact increment 2) — largely obviated.**
   The motivation was that class-level selection was useless on coupled monoliths;
   runtime discovery *dissolved* that (it selects finely on databind — see Tier A),
   so going sub-class is now low-value. Keep parked unless a real suite shows
   class-level discovery is still too coarse; only then revisit (and the soundness
   kernel — coverage is a past-run record — remains Fable-reserved).
6. **`brix.go` and Protobuf "did the meaningful part change?" skip.** Build Go by
   wrapping the `go` tool; add the interface-change skip — when a project using
   them appears.

### Tier E — small, safe, opportunistic

7. **Draw the build graph + more machine-readable output.** A visual graph
   (boxes/arrows) consuming the `status --json` document; plus `errors --json`.
8. **Document the three official extension points** (cache store, `content_key`,
    change detection) so others can build on brix.
9. **Smaller follow-ups.** A `build --verify-contents` CLI flag; C++ and Kotlin
    starter templates; a `pydoc`/`pdoc` doc-gen action + warm-daemon doc path; more
    "trick brix into a bad skip" oracles (generator-driven membership changes,
    multi-hop ABI chains); default git-detection on for git workspaces (only after
    the air-gapped trial).
10. **Formal Python API v1 label (near the very end).** Once the surface is mature
    by use: cut `1.0.0` (semver on the distribution), lean on the
    `tests/test_public_api.py` contract, a `DeprecationWarning` cycle for breaks —
    and *don't* put the version in the import path. A labeling non-event, never a
    reason to work around a gap.

---

## Needs a decision from you

- **Replacing your Java/TS build tools with brix:** (a) wrap the existing builds
  and replace piece by piece, or replace all at once; and (b) does brix also
  *download* third-party dependencies, or leave that to an existing tool.

---

## Nice-to-have / parked (no driver yet)

- **Resident daemon / warm graph.** Collapse the cold no-op floor (~3–4s at
  ~50-service scale) to instant by keeping the realized graph + watch resident.
  A mild annoyance, not urgent.
- **Filesystem-watcher change detection.** A pluggable `ChangeSource` so a no-op
  is O(changed) not O(all files). Correctness-critical invalidation path
  (Fable-territory); only pays off with the resident daemon; build the seam and
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

**Lazy monorepo loading ("only build what my change touched").** `brix.affected`
with `select_affected`/`load_affected`, index v2 (persisted per-package
patterns/footprints), gated by a full-edit-alphabet oracle. Opt-in; full load
stays the default. Design: [monorepo-scaling.md](monorepo-scaling.md).

**Languages & ecosystems.** Kotlin (single + multi-module, all-open plugin, warm
kotlinc worker); Spring Boot executable fat jars (`brix.springboot`); Rust via
cargo wrap (`brix.rust`, including brix self-hosting); annotation processing.
Dependency resolvers `java.pom`/`java.maven`, `cpp.pkg_config`, `ts.npm` over a
shared `DepSet`. Content lenses `jvm-abi`, `kotlin-abi`, `ts-dts`; `content_key`
refactor + glob `-` exclusion.

**Correctness hardening.** Git-based change detection on both engines;
`verify_contents` re-hash mode; symlink-repoint detection; the delete-cleanup
corner case; environment recording phases 1–2 (`env`/`env_used`).

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
  Fable-territory. And it only pays off with a **resident** watcher (a persistent
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
build (Fable-territory: a wrong merged view is a silent wrong build); platform-specific
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
