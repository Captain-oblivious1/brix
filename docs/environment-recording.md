# Recording the build environment

Status: **phase 1 implemented (2026-07-17, Opus)** — diagnostic capture +
`brix.py env` shipped (`src/brix/environment.py`, wired in `src/brix/cli.py`,
`tests/test_environment.py`); phases 2–3 deferred with their boundaries drawn
below. Companion to `docs/content-key.md` (the state-vs-semantics rule this
obeys) and `docs/roadmap.md` (the "record the environment a build used" item).

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
2. **Declared promotion (deferred).** `env_used=["JAVA_HOME", ...]` on an
   action promotes named variables to fingerprint material, so changing one
   re-stales that action. This is the *guard*, opt-in, and it crosses the
   fingerprint path (both engines, byte-identical rule). The variables worth
   naming are usually **discovered** by phase 1's diff — you promote from
   evidence, not from a guess.
3. **Automatic detection (deferred, heavy).** Observe which variables the
   process actually read (`getenv` via `ptrace`/`strace`, or a shim
   environment) and fold them in like depfile-discovered headers
   (`docs/…` incremental depfile path). Turns unknowns into tracked deps with
   no declaration, but is platform-specific and fragile. Parked as the
   "automatic" endgame.

Phase 1 alone is the piece that de-risks the air-gapped monorepo trial: when a
build there behaves differently and the operator is debugging **without AI**,
"these N variables differ from the last recorded good build" is the diagnostic
that turns a blind hunt into a quick answer.

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
- It does **not** try to detect which variables a tool *read* (phase 3); it
  records the whole filtered environment and lets the diff speak.

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
