# Roadmap

The one place for what's left and what's done. **Remaining work is at the top in
priority order; completed work is a flat list at the bottom.** Each line is one
plain-English item; where a thing has a design contract, the doc is linked — the
detail lives there and in the [development log](development-log.md), not here.

---

## Remaining — highest priority first

Guiding rule: **finish a capability before starting a new one** (we may lose the
capable model at any time; brix must always be shippable). So test-impact — the
one feature currently in flight — gets closed out first.

1. **Per-method test selection (test-impact increment 2).** Go finer than
   file/class using runtime coverage (JaCoCo / gcov). This is the *only* way to
   skip tests inside a tightly-coupled module — the jackson-databind dogfood
   showed class-level selection skips ~0% there, correctly, because everything
   routes through a reflective, widely-referenced `ObjectMapper`. Design +
   discipline in [test-impact-selection.md](test-impact-selection.md).

2. **Harden Java "something failed last build" from global to per-suite.** Today
   any failed action anywhere pins *every* suite to a full run (`prior_failed`).
   Safe but wasteful at 50-service scale; make it per-suite.

3. **C++ test selection on Windows (#43).** Detect `LoadLibrary`/`GetProcAddress`
   (the Windows analogue of `dlopen`, currently a hole on MinGW+`nm`), and handle
   `.exe` / path normalization. Correctness gap, but Linux-only target for now.

4. **Environment isolation (env recording, phase 3).** Fully spec'd and ready to
   build: one `env` dict with a four-way vocabulary (pin / inherit+fingerprint /
   inherit-only / withhold) that scrubs each step's environment to an allowlist,
   so an undeclared variable is *absent* instead of a silent wrong build. Each
   `brix.<language>` ships a default map naming the latent traps. Crosses the
   fingerprint path → **Fable review** after build. Supersedes the older
   "thread `env=`/`env_used=` into the JVM/C++ wrappers" follow-up. Spec:
   [environment-recording.md](environment-recording.md).

5. **A JavaScript/TypeScript way to write build files (big track 3).** Same
   features as the Python API, authored in TS. Unblocked now that the Rust engine
   is the default and carries build/status/clean + extension traits. The binding
   is `rust/brix-py` transliterated over the same JSON boundary; the TS API layer
   is mechanical against the contract in [boundary-schema.md](boundary-schema.md).
   Opus-sized, with a **Fable review** of the FFI callback threading.

6. **Delete the old Python engine internals (track 4c).** On hold until the Rust
   default has soaked on a real project for a real work stretch (days). Deletes
   the execution half, keeps the reference math (Python stays the golden-fixture
   oracle). One subtle ordering bug breaks builds → **Fable**, line by line.
   Boundary spelled out in [rust-port.md](rust-port.md).

7. **`brix.go` and Protobuf "did the meaningful part change?" skip.** Build Go by
   wrapping the `go` tool, and add the interface-change skip (as Java/TS already
   have) for Go and Protobuf — when a project using them actually appears.

8. **Draw the build graph + more machine-readable output.** A visual graph
   (boxes/arrows) instead of a text list, consuming the `status --json` document;
   plus a matching `errors --json`.

9. **Document the three official extension points.** Where cached results are
   stored, how brix judges whether a change "matters" (content_key), and how it
   detects file changes — written up so others can build on brix.

10. **Smaller follow-ups (do when convenient).** A `build --verify-contents` CLI
    flag for a one-off paranoid run; C++ and Kotlin starter templates; a
    `pydoc`/`pdoc` doc-gen action + a warm-daemon path for doc generation; more
    "try to trick brix into a bad skip" oracles (generator-driven membership
    changes, multi-hop ABI chains); default git-detection on for git workspaces
    (only after the air-gapped trial).

---

## Needs a decision from you

- **Replacing your Java/TS build tools with brix:** (a) wrap the existing builds
  and replace piece by piece, or replace all at once; and (b) does brix also
  *download* third-party dependencies, or leave that to an existing tool.
- **Declare v1 of the Python API.** Recommendation on the table: cut `1.0.0`
  (semver on the distribution), freeze the top-level contract (already pinned by
  `tests/test_public_api.py`), a `DeprecationWarning` cycle for breaks — and
  *don't* put the version in the import path. Capture-only until you decide.

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

**Test-impact selection — wired for Java, C++, and TS in real builds.**
Language-agnostic core + per-language providers (`jvm-abi` refs, C++ `-MMD`+link
graph, `tsc`/import graph), each proven against a real runner (JUnit/gtest/vitest)
with a teeth-checked equivalence oracle, under an absolute "selection == full-run
verdict" guarantee with dynamic-loading fail-open. Java selects via eval-time
`plan_suite`; C++/TS are engine-native (run unit = selection unit). **Dogfooded on
the real jackson-databind suite** (708 files, ~3,400 tests): proved class-level
selection is sound but low-payoff on a coupled monolith — the motivation for
increment 2 above. See [test-impact-selection.md](test-impact-selection.md).

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
