# Rust engine port — design

This document is the execution contract for porting the brix engine to
Rust. It is written so that a model (or person) who did **not** design
brix can carry out the port without reinterpreting anything. Read it with
`README.md` (the spec) and treat `tests/` as the acceptance suite.

**Prime directive: the Python test suite is the spec.** The tests run
unchanged against the Python API throughout the port. If a behavior seems
odd, it is load-bearing until proven otherwise — check the
"do-not-reinterpret" list below before "fixing" anything. When Rust and
this doc disagree with the tests, the tests win.

## Goals and non-goals

- One Rust core (`brix-core`) that the Python API wraps today and a JS/TS
  API wraps later. Python remains the user-facing build language; project
  `brix.py` files do not change at all.
- Real parallelism: the scheduler and hashing escape the GIL.
- Non-goal: changing any semantics. The port is a re-implementation of
  fixed behavior, not a redesign. No new features ride along.
- Non-goal (v1): supporting user-defined `Action`/`Expandable` subclasses
  from Python. The library's action types (`ExternalProcessAction`,
  `ExpandAll`, `DeleteFilesAction`) are the supported vocabulary;
  `cpp.py`/`java.py` sugar already lowers to these in Python before the
  boundary, so it keeps working unchanged.

## Crate layout

```
rust/
  brix-core/        pure Rust, no PyO3 anywhere (JS/TS reuse depends on this)
    patterns.rs     PathPattern: match, substitute, overlaps, find
    data.rs         atom types (File/Glob/Regex/Tool/Value), DataExpr, unions
    actions.rs      ExternalProcessAction, ExpandAll, DeleteFilesAction,
                    substitution (the Expandable behavior)
    graph.rs        logical graph
    planner.rs      dependency inference, plan lowering, input/output atoms
    targets.rs      demand-driven selection, binding extraction, shortest stem
    plan.rs         ExecutionPlan, nodes, edges, triggers, NodeStateTable
    scheduler.rs    execute(): readiness, cascade, insertion channel, thread pool
    context.rs      NodeContext overlay (read_path/find/iter_files)
    layers.rs       BuildLayer trait + incremental / cache / atomic layers
    fingerprint.rs  action_key, membership_key, action_fingerprint, hashing
    metadata.rs     metadata.json load/store, TimeAndHashChangeDetector
    process.rs      shlex split, spawn, capture, ProcessResult
    render.rs       command/env rendering, output-dir preparation
    status.rs       compute_status
  brix-py/          PyO3 bindings only; compiles to the `brix._core` module
```

Python `src/brix/*.py` shrinks to: the unchanged public API surface
(class names, constructor signatures, `build()`/`plan()`/`layers()`),
delegating to `brix._core`. `cli.py`, `report.py` (the Reporter base and
stock reporters), `cpp.py`, `java.py` stay pure Python.

Use `maturin` for packaging; `pip install -e` must keep working for the
animation project. Keep the pure-Python engine in-tree until the port is
complete (see Phasing) — selected by `BRIX_ENGINE=py|rust` (default
`rust` once phase 3 lands), so every pytest run can be executed against
both engines during the transition.

## The boundary contract

The boundary is **coarse and data-shaped**, not object-shaped. Python
never holds references into live Rust scheduler state. The exact wire
schema (graph spec, config, event stream, extension-trait call shapes)
is pinned in **`docs/boundary-schema.md`** — build phase 2 against that
document, not against prose.

1. **Graph goes in as a value.** Users build the `Graph` with today's
   Python classes; at `plan()`/`build()` time it lowers to a declarative
   spec (actions with their name/command/inputs/outputs/env as plain
   data; atoms tagged by kind). This is a one-way serialization — Rust
   never calls back into Python to interrogate the graph.
2. **Configuration goes in as a value.** The builder stack is described
   by config (incremental: metadata_dir; cache: cache_dir or store
   handle; atomic: staging_dir; jobs; targets), not by passing layer
   objects. Builder *classes* stay in Python for API compatibility, but
   `layers()` composition becomes "which core layers to enable, in what
   order."
3. **Results come out as values.** `BuildResult`, `ProcessResult`,
   `StatusReport` cross as data and are rehydrated into today's Python
   dataclasses.
4. **Events stream out through one callback.** Rust emits the reporter
   events (`build_begin`, `node_started`, `process_started`,
   `node_completed`, `node_skipped`, `collection_realized`,
   `node_inserted`, `build_end`) to a single boxed callback; `brix-py`
   adapts that to the Python `Reporter` object, acquiring the GIL per
   event. `process_started` is emitted from worker threads (see
   subtleties); all other events from the scheduler loop. Reporter
   exceptions must not kill the build: catch, report once to stderr,
   continue.
5. **Extension points cross as narrow traits with Python adapters.**
   These are the only places Rust calls Python, each optional:
   - `ChangeDetector` (future `IncrementalBuilder(detector=...)`),
   - `CacheStore` (TODO #2 — `load`/`restore`/`store`; the filesystem
     store is native Rust, a Python object implementing the three
     methods gets wrapped),
   - `DependencyScanner` (post-run hook; **implemented Python-side** — see
     the discovered-deps note under Fingerprints below).
   The general `BuildLayer` trait is **Rust-internal**; Python-defined
   layers are not part of v1's contract.

## What must be byte-compatible

- **`metadata.json`**: same schema, `"version": 1`. Rust must read
  Python-written metadata and vice versa during the transition. JSON
  writing: keys sorted, `indent=1` is cosmetic (readers don't care).
- **Fingerprints and keys are cross-machine cache keys** — this is the
  hard compatibility requirement. `_digest` is
  `sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))`.
  Rust must reproduce this **byte for byte**: same payload structure,
  sorted keys, no spaces, and Python's `json` string escaping (escape
  `"` `\` and control chars as `\uXXXX` for chars < 0x20; non-ASCII
  passes through — `ensure_ascii=False`? **No**: Python's default is
  `ensure_ascii=True`, so non-ASCII becomes `\uXXXX` escapes. Match it.)
  All payload values are strings/lists/dicts/None — no floats, so no
  float-formatting hazard. Write cross-language golden tests: a fixture
  file of payloads with the Python-computed digests, asserted from Rust.
- **Cache store layout**: `<fp[:2]>/<fp>/entry.json` + `files/<n>` blobs,
  temp-dir-then-rename writes. A Rust build and a Python build sharing
  one cache directory must interoperate. The **remote** store
  (`cache_http.py`, `HttpCacheStore`) mirrors this layout over HTTP GET/PUT
  (same paths, so a local dir and a remote endpoint are the same shape), with
  one addition the local layout doesn't need: an entry carries `file_modes`
  (`{logical: mode}`) because bytes over HTTP lose the executable bit the local
  blob keeps for free via `copy2` — a native remote store must record and
  re-apply modes on restore likewise, and blobs must be written before the
  entry so a partial upload reads back as a miss.

## Do not reinterpret (load-bearing subtleties)

Ground rules (from the spec; the scheduler's correctness rests on these):

- The plan is **append-only**; node definitions are frozen after
  creation; the insertion channel is the *only* way structure grows.
- `NodeStateTable` transitions are monotone:
  pending→running→done|failed|skipped. The scheduler owns all state.
- Layers never touch plan structure or node state; they return results,
  adjust the context, and propose nodes (`NodeProposal`).

Scheduler:

- Layer stack order: list is innermost-first; the *last* layer wraps
  outermost. Composition today: `[cache, incremental, atomic]`.
- Edge triggers: `on-success` (default) cascades SKIPPED through
  successors on failure; `on-completion` successors run after success
  *or* failure. The cascade walks only on-success edges.
- Stuck-node handling: when nothing is running and nothing schedulable,
  judge **every** stuck node against the same state snapshot before
  advancing any of them (advancing one to SKIPPED must not soften the
  verdict on the next); all-terminal predecessors → quiet skip, else
  "unreachable (dependency cycle?)" error.
- Runtime expansion: an EXPAND node realizes its collection through the
  overlay, creates children wired to the parent's predecessors and
  successors; a nested `ExpandAll` child becomes another EXPAND node.
  Members are the sorted unique bindings of `element_name`; a collection
  atom that doesn't bind it is an error.
- `process_started` fires at the **innermost** point of the stack — only
  when a real process is about to spawn. A replaying/caching layer that
  answers without calling inward must prevent the event. It fires on the
  worker thread.
- Layer proposals from `on_collection_realized` are inserted ordered
  *before* the expansion's consumers.

Patterns, targets, substitution:

- Target binding extraction uses probe substitution: fill the need
  pattern's binding names with `"\x00target\x00"`, match against the
  producer's output pattern, drop any binding whose value contains the
  probe. No match → `overlaps()` decides related-but-unprovable (`{}`).
- **Shortest-stem rule** (make's): among candidate producers for a need,
  keep only those with minimal total bound-text length; `{}` (unprovable)
  is infinitely unspecific. Without this, suffix pipelines
  (`{video}.mpg` vs `wealth.raw.mpg`) diverge into `.raw.raw...` loops.
- `ExpandAll.substitute(bindings)`: if its own `element_name` is bound,
  it **collapses to the substituted child** (not a one-element
  expansion). Otherwise substitute collection + child and keep the
  ExpandAll shape.
- `RegexData.substitute`: replace each `(?P<name>...)` group with
  `re.escape(value)` — the result is a *narrower regex*, not a literal.
- Actions may be selected multiple times under different bindings (two
  targets → two constrained instances); dedupe needs by `repr`-identity
  of the atom, dedupe selections by (action index, sorted bindings).

Fingerprints and incremental:

- `action_key` (structural identity: command template, input/output
  *shapes*, env shape) vs `action_fingerprint` (content: command, tool
  identity, input hashes, env values, `ValueData` text). Records are
  keyed by action_key; freshness compares fingerprints. `ValueData` text
  is fingerprint material but **not** identity.
- **Discovered dependencies** (`DependencyScanner`, e.g. C/C++ headers from a
  compiler depfile): after a run, an action carrying a `depfile` marker has its
  emitted Makefile-format depfile scanned (`scan_depfile`), and the reported
  paths are recorded in metadata under `deps` keyed by action_key. On the next
  build `action_fingerprint` appends a `"deps"` map (`{path: hash}`, sorted) to
  the payload — so the Rust port must reproduce this component byte-for-byte or
  cache keys diverge. It is absent on the first build and for actions with no
  depfile, so it never perturbs an existing fingerprint. Both layers read the
  deps via the shared detector's metadata, so the *cache* key includes them too
  (no stale-header hits); after a run the fingerprint is recomputed with deps
  folded in (record is dep-aware immediately → next build fresh), and the scan
  also runs after a cache hit (a cache-fed machine still learns the headers).
  Not identity: `depfile` and discovered deps never enter `action_key`.
- Tool fingerprint = resolved path (PATH lookup if no `/`) + stat
  signature (mtime_ns, size), *not* content hash. Missing tool →
  `[tool, name, None, None]`, still a valid fingerprint.
- Hash cache: sha256 keyed by absolute path, validated by (mtime_ns,
  size); persisted in metadata so unchanged files cost one stat.
  Directories and missing files hash as None (never an error).
- Freshness = record exists ∧ record.ok ∧ fingerprint equal ∧ recorded
  output hashes still match **through the overlay** (`ctx.read_path`).
- Failure handling: outputs of a failed action are **deleted**
  (clean-build parity; directories left in place), the failure recorded
  with `ok: false` — never fresh (always reruns) but reported by
  status/errors with replayed diagnostics.
- Collection membership is recorded per `membership_key` but applied to
  metadata **only if the whole build succeeds**; a failed build must
  re-detect deletions next time. Vanished members propose
  `DeleteFilesAction` cleanup and forget the vanished action's record. A
  vanished *nested* expansion recurses: its inner collection never realizes
  (the subtree is pruned), so cleanup reads the vanished member's recorded
  inner membership back from metadata — the inner `membership_key` is
  deterministic from the substituted collection, so it matches what the
  previous build recorded. This read must **not** mutate membership records
  (a failed build re-detects via the still-committed outer membership and
  walks back in); the one unrecoverable case — an inner membership never
  recorded (prior failure, or nesting added since) — is left for a future
  build rather than guessed.
- Metadata saves at build end (success or failure), atomic
  write-tmp-then-rename.

Cache:

- Cache layer answers **only stale** nodes (it sits inside incremental).
  Store only results with `source == "process"`, a real `exit_code`, no
  signal. Failures are cached with zero outputs and replay as failures.
  Corrupt/partial entry → treat as miss and fall through to the process.
  Restores go through `ctx.output_root` and use copy-with-mode (`copy2`)
  so executables stay executable.
- Cache and incremental share one change detector per build (hash each
  file at most once). Cache and atomicity are independent — stack order
  alone routes restored outputs through staging.

Overlay and atomic:

- Read order: `output_root` shadows `workspace_root`; a deletion marker
  hides a file unless re-created in staging. `find` merges roots in that
  order; `iter_files` excludes the staging tree's parent and any path
  with a `.brix`-prefixed component.
- Atomic: staging `add/` + `del/`; `DeleteFilesAction` is intercepted by
  the atomic layer into deletion markers (no workspace touch). Commit:
  write `COMMIT` marker, copy `add/` into workspace, unlink `del/`
  targets, remove staging. On build begin: a `COMMIT` marker means a
  crashed commit of a *successful* build — finish it; staging without a
  marker is failed-build debris — discard it. (TODO #1, held staging,
  will add a third state; don't preclude a "held" marker.)
- Output-directory convention: directories a tool writes into are
  declared as concrete `data.file` *outputs* (rendered through
  output_root) and consumed as `data.file` *inputs* (overlay-resolved).

Process execution:

- Command lines are rendered strings split with **shlex rules** (the
  `shlex` crate matches POSIX mode closely; golden-test the corner cases
  used in tests: single-quoted `'...'` values containing spaces).
  Empty/unsplittable command → failed ProcessResult with `brix:` stderr,
  exit_code None (and therefore never cached).
- `env=` entries extend and shadow the inherited environment. Negative
  return code means killed-by-signal: `signal = -rc`, `exit_code = None`.
- `prepare_output_dirs` creates parent dirs (and declared directory
  outputs) under `output_root` before spawning.

CLI/status stay in Python but depend on core answers:

- status asks freshness *hypothetically* (never mutates), recursing
  through nested expansion points; exit codes: status 0 clean / 1 work
  pending; errors 1 if failures; build 0/1; ValueError (unknown target)
  → message on stderr, exit 2.

## Threading model

- Scheduler loop on the calling thread; node execution on a worker pool
  (`jobs` or `min(32, cpus)`). Process spawning is blocking per worker —
  plain threads are fine (no async runtime needed; don't introduce one).
- Metadata and cache-store writes happen from worker threads: keep the
  same coarse-lock discipline (`Mutex` around metadata map; store writes
  are temp-dir + rename, last-wins).
- Python callback events: `process_started` originates on workers —
  acquire the GIL per call in `brix-py`; core must not hold any lock
  while emitting events (a slow reporter must not stall other workers,
  and a reporter that calls back into brix must not deadlock).

## Phasing (each phase lands green on the full pytest suite)

1. **`brix-core` bottom layer + golden tests** — **DONE**: patterns, data
   atoms, fingerprint digests, shlex split. Cross-language golden fixtures
   (payload → digest; pattern → match/overlap results) generated by a
   small Python script (`rust/golden/generate.py`) running the **real**
   Python code, checked into `rust/brix-core/tests/golden/`.
2. **Planner + targets** — **DONE**: graph spec deserialization, inference,
   target selection, `plan.describe()` parity. *Plus* the phase-2 deferred
   pieces landed ahead of phase 3 (see Status below): the fingerprint keys,
   `RegexData` matching/substitution, and the whole content fingerprint.
3. **Scheduler + layers + context**: the big one. Python builders
   delegate `build()` end-to-end; reporters and results cross the
   boundary. All tests pass with `BRIX_ENGINE=rust`.
   *Slice 1 — the scheduler core — is DONE*: `executor.rs` (readiness,
   on-success-only cascade, stuck-node snapshot rule, runtime expansion,
   insertion channel), `process.rs`, `render.rs`, and the
   `BuildLayer`/`Reporter` trait seams, on plain scoped threads. Clean
   builds run end-to-end in pure Rust, pinned by
   `rust/brix-core/tests/executor.rs` — the Rust mirror of
   `tests/test_scheduler_contract.py` (same cases, same names).
   *Slice 2 — overlay parity under execution — is DONE* (staged writes,
   merged reads, deletion markers, redirected deletes; pinned in the same
   test file). *Slice 3 — the incremental layer — is DONE*: `metadata.rs`
   (the metadata.json store: lenient loads, sorted tmp+rename saves,
   sig-validated hash cache preserving Python-written identity digests),
   the detector's persistent hash memo, tool atoms and the
   discovered-deps fold in `content.rs`, and `incremental.rs`
   (fresh-replay with stored stdout/stderr, failure-is-never-fresh with
   output removal, vanished-member Delete proposals with nested reaping,
   depfile scanning with same-build refingerprint), pinned by
   `rust/brix-core/tests/incremental.rs` — the mirror of
   `tests/test_incremental.py` (same cases, same names).
   *Slice 4 — the cache layer — is DONE*: `cache.rs` (the
   `CacheStoreProtocol` trait, the local `CacheStore` sharing Python's
   on-disk layout so both engines can share one cache directory,
   `TwoTierCacheStore` with back-hit promotion, and `CacheLayer` where
   any store error degrades to a miss / skipped populate, never a failed
   build), pinned by `rust/brix-core/tests/cache.rs` — the mirror of
   `tests/test_cache.py` + `tests/test_cache_store.py` — including
   read/write interop pins on the entry layout.
   *Slice 5 — the atomic layer — is DONE*: `atomic.rs` (staging
   redirection at build begin, staged deletion markers, the journaled
   COMMIT protocol with idempotent crash recovery, stale-staging
   discard, and held staging: HELD marker, build-on-top composition,
   `commit_staging`/`discard_staging` with their refusal rules), pinned
   by `rust/brix-core/tests/atomic.rs` — the mirror of
   `tests/test_atomic.py` + `tests/test_held_staging.py` (minus the
   status/CLI cases, which stay Python until the boundary) — plus the
   full three-layer stack proof (cache hits restored via staging, then
   committed).
   *Slice 6 — the PyO3 boundary — is DONE*: the `brix-py` cdylib crate
   exposes one function, `brix_py.build(spec_json, config_json,
   on_event=None) -> result_json` — three JSON strings per
   `docs/boundary-schema.md`, GIL released for the whole build,
   re-acquired per event, callback exceptions stashed and re-raised
   after the build. Python-side: `brix/lowering.py` (the graph→spec
   lowering, moved out of `rust/golden/generate.py`, which now imports
   it — goldens stayed byte-identical), and `brix/engine.py` (extension
   discovery: installed module → `$BRIX_RUST_ENGINE_LIB` → dev tree;
   `_EventAdapter` replaying wire events onto Python reporters;
   `maybe_build` dispatch every builder's `build()` calls first).
   `BRIX_ENGINE=rust` runs the whole Python suite through the Rust
   engine; graphs the port can't take yet (`identity=` plugins, custom
   Python cache stores) silently fall back to the Python engine, while
   a missing extension raises. Boundary pins in
   `tests/test_rust_engine.py`.
   *Slice 7 — persistent workers — is DONE*: `workers.rs` mirrors
   `workers.py` (the JSON line protocol, one request in flight per
   worker, lazy start, pools per spec, build-end teardown, crash /
   startup failures as `exit_code: None` uncacheable results with no
   fallback to spawning). The wire `Process` shape gained the reserved
   `worker` key (`WorkerSpec`: name, command_line, inputs); worker
   inputs create dependency edges and the worker's identity joins
   `action_fingerprint` (golden-pinned for byte-parity — fingerprints
   are cross-engine cache keys). One deliberate translation: Python
   keys pools by spec *object* identity, the Rust core by spec
   *content* (across the JSON boundary every node carries a copy of
   its spec, so structural identity is the faithful reading). Pinned
   by `rust/brix-core/tests/workers.rs` — the mirror of
   `tests/test_workers.py` (same cases, same names). With workers
   crossing, `tests/test_java_worker.py` also runs on the Rust engine
   under `BRIX_ENGINE=rust`.
   *Slice 8 — built-in content identities — is DONE*: `jvmabi.rs` and
   `tsdts.rs` mirror `jvmabi.py`/`tsdts.py`, registered under the same
   versioned tags (`jvm-abi@1`, `ts-dts@1`) in `identity.rs`; the
   fingerprint tail (`[tag, digest]`, or `[tag:raw, byte_hash]` when the
   plugin errors) is byte-identical (golden-pinned over the committed
   `tests/fixtures/jvmabi` class bytes; `rust/brix-core/tests/identity.rs`
   asserts the exact digests pinned in `tests/test_content_identity.py`).
   Identity digests are cached in `metadata.json` under their tag with
   the same stat-signature validation as byte hashes, interoperably with
   Python-written entries. **Python remains the reference
   implementation**: an algorithm change is a version bump, and a bump
   in one implementation must land in both (the tag joins the cache
   key, so an unsynchronized bump splits the cache, never corrupts it).
   Where Python's polymorphic sort would raise on malformed class files,
   Rust uses a total type-rank order — a divergence there can only cause
   a rebuild, never a stale hit. `supports_rust_engine` narrowed: only a
   *custom* skin-registered identity (not `jvm-abi`/`ts-dts`) falls back
   to the Python engine now, and `tests/test_rust_engine.py` proves a
   Python-engine build replays byte-for-byte on the Rust engine through
   shared metadata.
4. **Flip the default**, keep `BRIX_ENGINE=py` working one release,
   verify the animation project (full dryRun cycle, targeted builds,
   clean-then-cache-restore, `view`), then delete the Python engine
   internals, keeping the API wrappers.
   *The flip is DONE*: unset `BRIX_ENGINE` runs the Rust engine;
   `py` forces the Python engine; an explicit `rust` with no built
   extension raises, while the default falls back to Python (a
   pure-Python checkout keeps building). Animation verified end-to-end
   on both engines against shared metadata — which caught one real
   inference bug, fixed in both engines: same-collection expansions'
   bindings are now unified during edge inference (`overlaps`'
   `shared=` / `overlaps_shared`), because recursive-by-default
   `{video}` otherwise splits `out/{video}.mpg` vs
   `out/{video}/{scene}.mpg` two ways and fabricates a
   combine↔finalize cycle.
   *Phase 4a is DONE*: `status` and `clean` call the core (see the
   Decisions list below).
   *Phase 4b is DONE*: the `cache_store` and `content_identity`
   extension traits are live — custom Python stores and skin-registered
   identity plugins cross the boundary as callables the core invokes
   (shapes + contract notes in `docs/boundary-schema.md`), so nothing
   falls back to the Python engine anymore except a missing extension
   under the *default*. Remaining: one release of soak, then the
   deletion (4c).

Phases 1–2 are safe for a cheaper model working alone against the golden
tests. Phase 3 is where the do-not-reinterpret list matters most —
review the scheduler and layer implementations against it line by line
before trusting green tests, because several subtleties (event ordering,
stuck-node snapshots, cascade rules) are only partially pinned by tests
(now backstopped by `tests/test_scheduler_contract.py`, an executable spec
either engine must pass).

### Status (2026-07)

Phases 1 and 2 are complete and, because they were cheap to bring forward,
so are the fingerprint pieces the phasing above had implicitly parked with
phase 3. All of the following are ported and pinned by golden fixtures
generated from the real Python (`cargo test`: 9 unit + 9 golden):

- **Digest + patterns + shlex** (`fingerprint.rs`, `patterns.rs`,
  `shlex.rs`) — the byte-compatible cache-key primitive and the hand-rolled
  `PathPattern` matcher.
- **Spec + planner + targets + plan** (`spec.rs`, `actions.rs`, `data.rs`,
  `planner.rs`, `targets.rs`, `plan.rs`) — inference, shortest-stem
  selection, `describe()` parity.
- **Fingerprint keys** (`keys.rs`) — `action_key` / `membership_key`.
- **`RegexData`** matching + `re.escape` substitution (`data.rs`), so
  `atoms_overlap` is faithful (regex-vs-regex never overlaps;
  regex-vs-concrete full-matches).
- **Content fingerprint** — `PathPattern::find` (`patterns.rs`), the change
  detector's `hash_file`/`stat_signature` (`detector.rs`; the `(mtime,size)
  → hash` memo is a perf cache deferred with the engine), `NodeContext`
  overlay reads (`context.rs`), and `action_fingerprint` (`content.rs`)
  over file/glob/regex/value atoms, pinned against a committed fixture tree
  (`rust/golden/tree/`).

Note the crate layout above is the original sketch; the hashing split
evolved into `keys.rs` / `content.rs` / `detector.rs` rather than one
`fingerprint.rs` + `metadata.rs`.

**Deferred tail** (the port *errors* on these rather than emit a possibly
wrong key): *custom* skin-registered content-identity plugins, which live
in the skin's runtime (graphs using one fall back to the Python engine).
`tool` atoms and the `metadata.json` hash-cache landed with phase-3
slice 3; the cache-store layout with slice 4; the `worker` toolchain leg
with slice 7; the built-in identities (`jvm-abi`, `ts-dts`) with slice 8.

**Remaining for phase 3:** nothing — phase 3 is complete. The phase-4
default flip has landed too (see the phasing above); what remains of
phase 4 is one release of soak time with `BRIX_ENGINE=py` available,
then deleting the Python engine internals.

## Decisions (were open questions; settled — do not reopen during the port)

- **Event ordering**: all events except `process_started` are emitted
  from the scheduler thread in scheduling order; `process_started`
  comes from worker threads and may interleave, but always between its
  own node's `node_started` and `node_completed`. Full contract in
  `docs/boundary-schema.md`.
- **Paths**: logical paths are POSIX (`/`) everywhere — metadata, cache
  entries, specs, events — on every platform; conversion to native
  separators happens only at the filesystem edge inside the core.
- **`status` and `clean` call the core.** Freshness logic exists once;
  the Python `compute_status`/`_clean` become thin clients over core
  calls, same as `build`. (CLI rendering and exit codes stay Python.)
  *DONE* (phase 4a): `status.rs` + `brix_py.status`/`brix_py.clean`,
  wire shapes in `docs/boundary-schema.md`, mirrored by
  `rust/brix-core/tests/status.rs` (the core cases of
  `tests/test_status.py`, same names).
