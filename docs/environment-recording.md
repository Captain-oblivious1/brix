# Recording the build environment

Status: **phases 1–2 implemented (2026-07-17, Opus)** — phase 1 diagnostic
capture + `brix.py env` (`src/brix/environment.py`, `tests/test_environment.py`)
and phase 2 the `env_used=` guard (`src/brix/actions.py`,
`tests/test_env_used.py`, golden-pinned cross-engine); phase 3 (automatic
`getenv` discovery) **struck as unsound (2026-07-18)** and replaced by an
isolation direction — scrub-to-allowlist — now **fully spec'd (2026-07-19)**:
the four-way `env` vocabulary + per-language default maps (see the phase-3 spec
section below), ready to build (Fable-reviewed at implementation). **Phase 2
Fable-reviewed (2026-07-18), verdict positive**:
unset-vs-empty is deterministic and engine-identical (canonical sorted JSON,
`null` ≠ `""`); the snapshot never reaches the process environment or the
command; the verbatim-through-`substitute` copy holds under nested expansion
(the frozen snapshot is handed to each copy explicitly); and the golden
genuinely exercises the path (the stored spec carries `_brix_env_used`; Rust
re-derives the pinned fingerprint). One hardening from the review: the
reserved input key can no longer be supplied directly — it previously
*replaced* the live snapshot with the author's constant (a guard bypass in the
stale direction); it now raises, and `substitute` threads the frozen snapshot
through a private parameter instead of relying on key presence. Fingerprints
byte-identical before/after (golden regeneration: zero diff). Known miss-only
edge: an env value that is not valid UTF-8 (surrogate-escaped bytes) fails
loudly at the boundary rather than fingerprinting silently.
Companion to `docs/content-key.md` (the state-vs-semantics rule this obeys) and
`docs/roadmap.md` (the "record the environment a build used" item).

## The gap

brix fingerprints what a build *declares*: source files, tool identities,
options, declared `env=` variables. It is blind to what a tool reads from the
**inherited** environment without anyone declaring it — `JAVA_HOME`, `CC`, a
locale, `SOURCE_DATE_EPOCH`, a flag someone exported in their shell. Such a
variable can change a build's output while brix, seeing no declared input
change, serves the result as up to date. This is the classic "works on my
machine, fails on the server": the same graph, a different environment, a
silently different build.

The declared `env=` field on `ExternalProcessAction` already *is* fingerprint
material (a `ValueData` per variable), so variables you name are handled. The
gap is precisely the variables you **did not** name — the unknown-unknowns.

## Phasing (and why this order)

The roadmap phrases the goal as *"recording what was actually used is the first
step to catching it."* That word — *first* — sets the order:

1. **Diagnostic capture (this phase).** Snapshot the build's environment each
   run into metadata; never fingerprint it. A `brix.py env` command diffs the
   current environment against the last recorded build and shows what changed.
   A *detective, not a guard*: it makes a silent difference **visible**, but
   does not itself rebuild. Cannot bust a cache, slow a build, or cause a
   spurious rebuild — it only records.
2. **Declared promotion (implemented).** `env_used=["JAVA_HOME", ...]` on an
   `ExternalProcessAction` promotes named variables to fingerprint material, so
   changing one re-stales that action. This is the *guard*, opt-in; the
   variables worth naming are usually **discovered** by phase 1's diff — you
   promote from evidence, not a guess. **How it stays off the Rust neutral
   path:** each named variable is read from `os.environ` on the Python side and
   snapshotted into a single fingerprint-only input (`_brix_env_used`, a
   `ValueData`) — the `classpath_track` pattern. Both engines already
   fingerprint a `ValueData` input identically, so only the resolved *value*
   crosses the boundary as ordinary data; Rust needs no change, and a golden
   fingerprint case (`env_used tracked variable` in `rust/golden/generate.py`)
   pins the parity mechanically. Load-bearing distinctions from `env=`: the
   snapshot is **never set** in the process environment (it tracks, it does not
   inject) and **never** substituted into the command; an **unset** variable is
   recorded as JSON `null`, kept distinct from set-to-empty; and the snapshot is
   copied **verbatim** through `substitute`, so an env value containing a
   `{binding}`-shaped substring cannot be rewritten during expansion. Resolved
   once, at action construction — which, since `graph_supplier` is called per
   build, is per build (a graph object constructed once and reused across env
   changes would hold a stale snapshot; the standard callable pattern does
   not).
3. **Isolation, not automatic detection (the sound path; opt-in).** The
   original plan here was to *discover* which variables a tool read (`getenv`
   via `ptrace`/`strace`, or a shim environment) and fold them in like
   depfile-discovered headers. **That is unsound, and is struck.** Observing
   that a tool read `CFLAGS` on *this* build does not bound what it reads on the
   *next* one: a tool can take a different branch on a different input and read a
   variable it did not touch this time, so a discovered env set is only ever a
   **lower bound**. Used as fingerprint material, a change to a variable the tool
   *would* read slips through as an up-to-date cache hit — a silently wrong
   build.

   The depfile analogy is false, and the reason is worth recording. File
   discovery is **self-healing**: when a source is edited to `#include` a new
   header, that edit is *itself a tracked file change*, so the action re-runs,
   re-emits its depfile, and re-discovers the new dependency — the trigger to
   re-discover is always observed. Environment discovery has **no such
   trigger**: flipping `EXPERIMENTAL=1` changes a tool's behavior with *zero*
   change to any tracked file, so nothing re-runs and last build's stale env set
   persists. Depfiles work precisely because the change that alters the
   dependency set is visible; env "discovery" fails precisely because it is not.

   So for the environment the sound design is **isolation, not detection**: run
   each action with a scrubbed environment — empty except an explicit
   **allowlist** — so an undeclared variable is simply *absent*, identical on
   every build by construction, and *cannot* be a hidden input. This makes the
   enumeration problem moot (you never have to know what a tool *might* read) and
   flips the failure mode from silent-wrong to **loud-and-fixable**: a tool that
   needs an undeclared variable fails immediately (add it to the allowlist)
   rather than succeeding with a stale result. A sane build-wide default
   (`PATH`, `HOME`, `LANG`, `TMPDIR`, …) means nobody starts from zero. The
   mechanism is pure subprocess-environment manipulation, so — unlike a `ptrace`
   tracer — it is **portable** across platforms. Opt-in, and a natural post-trial
   item.

   **Settled API shape (2026-07-18).** Rather than a second field, the allowlist
   *is* the declared environment. Collapse today's two fields — `env=` (set a
   variable to a value) and `env_used=` (track an ambient variable) — into a
   single `env` dict whose **value** carries the intent, via module-level
   sentinels:

   - `env={"LANG": "C"}` — **set** the variable to a literal value (today's
     `env=`); fingerprinted.
   - `env={"JAVA_HOME": SYSTEM}` — **take it from the ambient environment**, use
     that value, and fingerprint it, so a change since the last build re-stales
     the action (today's `env_used=`, but it now also *supplies* the value, and
     under scrub is what permits the variable through).
   - `env={"SECRET": UNSET}` — **force-absent** for this subprocess, even when
     the ambient environment or the build-wide default would otherwise supply it.
     This is how you *withhold* a variable from one action (the question a pure
     allowlist can't answer): with scrub on, not naming a variable already hides
     it, and `UNSET` overrides the default allowlist for a single action; with
     scrub off, `UNSET` is the only way to hide an inherited variable.

   This **deletes** the `env_used` concept (and its awkward past-tense name)
   instead of renaming it: one field, one mental model — *"here is this action's
   environment; each variable is pinned, taken from the system, or hidden."* The
   scrubbed environment a process sees is exactly `env`'s literal + `SYSTEM`
   entries, plus the build-wide default allowlist, minus its `UNSET` entries.

   Semantics to preserve when built: (a) **additive, not total** — declaring
   `env` adds to the default allowlist rather than replacing the whole
   environment, so today's no-scrub behavior is unchanged and *scrub* (the
   opt-in mode) is what strips undeclared variables; the sentinels work in both
   modes (scrub only changes the fate of variables you did *not* name).
   (b) **`SYSTEM` preserves unset-vs-empty** — an ambient-unset variable resolves
   to *absent*, not `""` (the phase-2 review's invariant), via the same per-build
   snapshot `env_used` does today (resolved when `graph_supplier` runs, so a
   changed value re-stales). (c) **`SYSTEM` is declared, not discovered** — so it
   does not reintroduce the unsoundness struck above; the completeness burden is
   the bounded, loud-under-scrub one, not path-dependent auto-detect.

   This is presentational with respect to fingerprints — a `SYSTEM` entry
   fingerprints as `env_used` does today, a literal as `env=` does today, `UNSET`
   is absence — so the *shape* introduces no new fingerprint hazard. The real
   subtleties (scrub × fingerprint, cross-engine byte-identity, the guard-bypass
   surface the phase-2 review probed) live in the **implementation**, which
   crosses the fingerprint path and is therefore reserved for a **Fable review**
   when built — the same gate phase 2 passed, not something to spend before code
   exists. Merging a shipped, golden-pinned field costs more than a rename
   (`actions.py`, the reserved-key plumbing, `tests/test_env_used.py`, the
   golden, these docs), but it is a net *simplification* (one field, one model,
   one fewer concept), and pre-1.0 is when to do it.

Phase 1 alone is the piece that de-risks the air-gapped monorepo trial: when a
build there behaves differently and the operator is debugging **without AI**,
"these N variables differ from the last recorded good build" is the diagnostic
that turns a blind hunt into a quick answer.

## Phase-3 spec: the env vocabulary and per-language default maps (2026-07-19)

This refines the "Settled API shape" above into the buildable design. It closes
the **silent latent-env** hole — a variable set in the ambient environment (a
shell profile, a Nix shell, a CI system) that a tool reads but the build file
never declared, so it changes output invisibly (the classic
`JAVA_TOOL_OPTIONS="-Dfile.encoding=…"` that silently re-encodes every `javac`).

### The four-way vocabulary

The `env` dict value carries the intent. Three sentinels were settled on
2026-07-18; a fourth (`PASS`) is required, for the reason below:

| `env[name]` | in the process? | fingerprinted? | for |
|---|---|---|---|
| `"literal"` | yes, pinned to the literal | yes | a reproducible pin (`LANG="C"`, `SOURCE_DATE_EPOCH="0"`) |
| `SYSTEM` | yes, the ambient value | **yes** | behaviour-affecting vars — inherit the machine's value but re-stale if it changes |
| `PASS` | yes, the ambient value | **no** | plumbing — needed to run, but must not enter the fingerprint |
| `UNSET` | no (forced absent) | — | forbid a variable for this action |

`PASS` is not redundant with `SYSTEM`, and conflating them breaks a shipped
feature. `PATH`, `HOME`, `TMPDIR` are needed for the tool to *run* (find `ld`/`as`,
locate `~/.cargo`/`~/.m2`), but their values are **noisy and machine-specific** —
`PATH` differs on every machine and reorders between shells. If they were
`SYSTEM` (fingerprinted), the *same logical build* would get a different
fingerprint on the dev laptop and the CI server, and the **shared result cache
would never hit across machines** — killing one of brix's headline features.
(`PATH`'s real risk — *which* compiler resolves — is already covered: `ToolData`
hashes the resolved tool binary.) So plumbing is `PASS` (present, unfingerprinted)
and behaviour vars are `SYSTEM` (present, fingerprinted).

### Base map + per-language overlay

The knowledge of *which variables matter to a toolchain* lives in the language
module, not the user's head. So each `brix.<language>` ships a **default `env`
map**, and an action's effective map is a shared **base** (universal plumbing)
merged with the module's **overlay** (its behaviour vars), with any user entries
layered on top (last wins):

- **Base (all actions):** `PATH`, `HOME`, `TMPDIR` → `PASS`; `LANG`/`LC_ALL` →
  `SYSTEM` (locale affects text decoding, so it is behaviour, not plumbing).
- **`brix.java` overlay:** `JAVA_HOME`, `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`,
  `CLASSPATH` → `SYSTEM`. (`JAVA_TOOL_OPTIONS`/`_JAVA_OPTIONS` are applied to
  *every* JVM launch, so they silently reach `javac`; this is the one that most
  needs catching.)
- **`brix.cpp` overlay:** `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`,
  `LIBRARY_PATH`, `SOURCE_DATE_EPOCH` → `SYSTEM`.
- **`brix.rust` overlay:** `CARGO_HOME`, `RUSTFLAGS`, `RUSTUP_TOOLCHAIN`,
  `CARGO_NET_OFFLINE` → `SYSTEM`. (`brix.rust` already threads `env=`/`env_used=`
  through; it becomes the reference for the others.)
- **`brix.kotlin`/`brix.springboot`** inherit the JVM overlay.

The user overrides any entry: `SYSTEM → "literal"` to pin for reproducibility,
`→ UNSET` to forbid ("this build must not be affected by `JAVA_TOOL_OPTIONS`"),
`→ PASS` to stop fingerprinting a var that turns out to be noisy in their setup.

### The payoff lands even without scrub

Crucially, defaulting the behaviour vars to `SYSTEM` **closes the silent-drift
hole with scrub *off*** — i.e. today's default, additive environment. With those
vars tracked, editing `JAVA_TOOL_OPTIONS` now re-stales the Java build
automatically, no user action, because the language module named the trap. And
it costs nothing in the common case: a `SYSTEM` var that is *unset* records as
absent (stable — no churn); only a var that is *set and changes* triggers a
rebuild, which is correct. **Scrub** (making `env` authoritative — the process
gets *only* the effective map, nothing else) is the second, opt-in step that adds
the *guarantee* of no undeclared var; the per-language maps are what make it
painless, since they are the allowlist.

### The worker set-vs-track asymmetry

A warm `javac`/`kotlinc` worker is a persistent process launched once with its
environment fixed at launch. Per-action `env=` **setting** (`literal`) cannot
reach it — the process already exists — so a worker-routed compile can only
**track** (`SYSTEM`/`PASS`, which are fingerprint-only and need not reach the
process), not **set** or **scrub**. So: `literal`/`UNSET`/scrub apply to the
spawn path; `SYSTEM`/`PASS` tracking applies to both. An action that both routes
through a worker and pins a variable must fail loudly at construction rather than
silently ignore the pin (the guard-bypass class the phase-2 review cared about).

### Boundary, fingerprints, and completeness

- **Fingerprint path / Fable.** `SYSTEM`/`PASS`/`literal` produce fingerprint
  material (the resolved snapshot, via the phase-2 `env_used` mechanism the Fable
  review already blessed); scrub changes *which* environment the process runs
  under. Both cross the fingerprint path and both engines must agree
  byte-for-byte, so the built implementation gets a **Fable review** — gated on
  the code existing, not on any external event.
- **Cross-engine.** Snapshots resolve on the Python side into one fingerprint-only
  `ValueData` (the `classpath_track`/`env_used` pattern), so only resolved values
  cross the boundary and the Rust core needs no env logic; a golden pins parity.
  Scrub is applied where the process is spawned (`src/brix/process.py` and the
  Rust executor), authoritative-replace instead of `{**os.environ, **env}`.
- **Incompleteness is safe.** The per-language lists are best-effort and will lag
  new tool versions. That is never a correctness dependency: with scrub off a
  missing var behaves exactly as today (leaks, untracked); with scrub on a missing
  *needed* var fails **loudly** (add it), never silently wrong.

## Where this sits in the state-vs-semantics rule

`docs/content-key.md` draws the line: comparison **semantics** (a static
declaration of *how* a datum is compared) may live in the graph; comparison
**state** (actual digests, last-build values) never does — it lives in
metadata. Environment recording splits cleanly across that line:

- The captured **values** are comparison *state* → metadata only, never the
  graph. Same category as fingerprints and depfile-discovered deps.
- A future `env_used=` **declaration** (phase 2) is comparison *semantics* →
  graph-legit, the same category as the existing `env=` field.

So phase 1 introduces **no** graph or wire change, and — being pure diagnostic
state that Python owns — needs **no** Rust engine change and does not enter the
fingerprint path.

## What phase 1 captures, and the noise filter

The environment is full of session noise that is not build-relevant and would
bury a real signal: `_`, `PWD`, `OLDPWD`, `SHLVL`, `TERM`, `COLUMNS`/`LINES`,
`SSH_*` connection vars, terminal/`DBUS`/session-cookie junk. The filter is a
conservative **ignore-list**, not an allow-list:

- **Ignore-list, because completeness beats quiet for a diagnostic.** Showing a
  harmless extra variable is cheap; hiding the one variable that actually
  mattered defeats the purpose. So the default drops only names known to be
  pure session volatiles and keeps everything else — `PATH`, `HOME`,
  `JAVA_HOME`, `CC`, `*FLAGS*`, locale, and anything project-specific.
- **Project-overridable.** `cli.main(..., env_ignore=[...])` extends the
  default ignore-list with additional names/globs (e.g. a CI system's
  per-build tokens), so a noisy shop can quiet its own known volatiles without
  losing the completeness default.

Granularity is **per build**, not per action: the environment is essentially
uniform across a build, and the "works on my machine" question is build-wide.
Per-action capture is a possible future refinement if a real case needs it; it
is not worth the volume now.

## Capture point

Capture happens at the **CLI boundary** (`cli.main`), on a `build` or
build-then-act command — the point at which a human (or the air-gapped
operator) actually runs a build. The Python CLI process *is* the environment
the build ran under (it invokes the Rust core in-process), so `os.environ` read
there is exactly what the build saw, on either engine. Programmatic
`builder.build()` calls (tests, verifier scripts) do not capture; they are not
the diagnostic target. Capture runs regardless of build success — a failed
build's environment is equally worth diffing.

## File format

A single JSON file beside the action metadata, written atomically (tmp +
replace), independent of `metadata.json`:

```
build/.brix/environment.json
{
  "captured_at": "2026-07-17T14:03:11Z",   # ISO-8601 UTC, when the build ran
  "brix_command": "build",                  # which command captured it
  "variables": {                            # the filtered environment, sorted
    "PATH": "...",
    "JAVA_HOME": "...",
    ...
  }
}
```

It is diagnostic state, so it is disposable: `clean` removes it with the rest of
the metadata directory, and a missing file simply means "no build recorded yet."

## The `env` command

`brix.py env` prints the recorded build environment and, when the current
(filtered) environment differs, the diff:

```
$ brix.py env
Last build environment (captured 2026-07-17T14:03:11Z, command: build):
  42 variables recorded.

Changes since then (current vs. recorded):
  ~ JAVA_HOME   /opt/jdk-17  ->  /opt/jdk-21
  + CC          clang
  - LANG        (was en_US.UTF-8)
```

Exit code: `0` when the environment is unchanged (or nothing recorded yet), `1`
when it differs — so it composes into scripts the way `status` does (0 = clean,
1 = attention). When nothing is recorded yet it prints a short note and exits 0.

A one-line summary also folds into `status` (`environment: 3 variables changed
since last build — run 'brix.py env'`) when a record exists and differs, so the
signal surfaces in the command people already run without changing its exit
semantics.

## What phase 1 explicitly does **not** do

- It does **not** rebuild on an environment change (that is phase 2's guard).
- It does **not** fingerprint anything, split any cache, or enter the Rust
  path.
- It does **not** try to detect which variables a tool *read* (that ambition is
  struck as unsound — see Phasing §3, which replaces it with scrub-to-allowlist
  isolation); it records the whole filtered environment and lets the diff speak.

## Verification plan

- `tests/test_environment.py`: capture writes a filtered record (noise names
  absent, build-relevant names present); the ignore-list override extends the
  defaults; `read`/`diff` classify added / removed / changed correctly; `env`
  exit codes (0 unchanged, 1 changed, 0 when unrecorded); `clean` removes the
  file; capture survives a failed build.
- A CLI-level test drives `cli.main(argv=["build"])` then
  `cli.main(argv=["env"])` under a mutated `os.environ` and asserts the diff.
- Engine-agnostic by construction (Python-only, no fingerprint material); a
  smoke check that `env` reads identically after a build on each engine.
