# The asset engine — design contract

Status: **design (spec-only).** The frontend + wire half is shipped; this document
pins the engine half so it can be implemented directly and verified without Fable.

## 1. What exists, what's missing

An `asset` atom names a file **brix itself ships** — a compiler-worker source, a
`--listFiles` recorder, a class-load recorder — by a stable logical `name` (POSIX,
brix-relative) instead of a machine path. The point: a skin references a shipped
helper *portably*, the **engine owns the bytes**, and the **fingerprint is the name
plus the file's content digest — never the resolved path** — so it is identical
across machines and across the Python/TS frontends (a shared cache stays valid).

**Done (do not re-touch):**

- Frontend atom: `AssetData(name)` — `src/brix/data.py:190` (skin-internal, not a
  `data.*` author constructor).
- Wire: `{ "kind": "asset", "name": str }` — emitted by `atom_to_spec`
  (`src/brix/lowering.py:54`), documented in `docs/boundary-schema.md:89-101`.
  Additive, still spec version 1 (a graph using no assets hashes byte-identically).
- TS frontend deep-equal oracle for the emitted atom (skin oracle harness).

**Missing (this contract):** every engine that *consumes* the wire must learn the
`asset` kind. Today both engines' atom `match`/`isinstance` ladders have no `asset`
arm, so an `asset` atom would fall through (Rust: non-exhaustive-match compile
error once the variant exists; Python: the `["other", repr(atom)]` fallthrough at
`src/brix/incremental.py:161`). The skins that would emit assets are therefore still
gated off with explicit `raise`s (java `javac_worker=`, kotlin `kotlincWorker`, the
ts import-tracking wrapper, springboot).

## 2. The exact engine sites (both engines)

There are **three** things every engine does with an atom — deserialize, fingerprint,
render — plus **one** new shared component (the asset store). Named precisely so the
implementation is mechanical:

### Rust engine (`rust/brix-core`)

1. **Deserialize** — add `Asset { name: String }` to `enum DataAtom`
   (`src/data.rs:16`). `serde` tag is already `kind` (rename maps `asset`).
   `DataAtom::substitute` (`src/data.rs:120`) passes it through unchanged (a name
   has no `{binding}`s).
2. **Fingerprint** — new arm in `atom_fp` (`src/content.rs:60`):
   `DataAtom::Asset { name } => Ok(Value::List(vec![Value::str("asset"),
   Value::str(name), <content-digest of the asset bytes>]))`.
   The digest is the store's `digest(name)` (§3-C), **not** `hash_file` of a resolved
   path — the path never appears here.
3. **Render** — new arm in `render_atom` (`src/render.rs:105`):
   `DataAtom::Asset { name } => store.resolve_path(name)` → an absolute real path the
   command line can name. Parallel to `resolve_tool` (`src/render.rs:35`), but the
   store, not `PATH`, is the authority.

### Python engine (`src/brix`)

Mirror sites, byte-for-byte the same material:

1. **Fingerprint** — new `isinstance(atom, AssetData)` branch in the inner
   `_atom_fingerprint` of `action_fingerprint` (`src/brix/incremental.py`, alongside
   the `ToolData`/`ValueData` branches at 152-160): `return ["asset", atom.name,
   store.digest(atom.name)]`.
2. **Render** — new `isinstance(expr, AssetData)` branch in `_render_expr`
   (`src/brix/render.py:54`) → `store.resolve_path(expr.name)`; a resolver analogous
   to `resolve_tool` (`src/brix/render.py:33`).

The Python engine is still the differential oracle for the Rust engine (task #6,
"delete Python engine internals", is deliberately *not* yet done). If it is ever
retired, the verification in §4 must move to golden fixtures alone — call it out
before deleting.

## 3. Design decisions (need Duane's nod before implementing)

These are the load-bearing choices. Everything else is mechanical.

### A. Where the canonical bytes live — the asset store

The helpers exist today under `src/brix/_java/`, `src/brix/_ts/`, `src/brix/_python/`,
referenced by `Path(__file__).parent/...`. That location ships with the *Python
distribution* only; the Rust engine (shared by both frontends) does not have them, and
the TS frontend must not need them.

**DECIDED (Duane, 2026-07-30): a single top-level `assets/` tree = the one source
of truth.** (Details below as recommended.)

- Layout mirrors the logical names: `assets/java/JavacWorker.java`,
  `assets/ts/tsc_listfiles.py`, etc. The **name is the POSIX path under `assets/`**
  (§3-D) — no separate manifest to drift.
- The **Rust engine embeds the tree at build time** (`include_dir!`/generated
  `include_bytes!` table in `brix-core`), so the compiled `.so`/`.node` carries the
  bytes and is portable to the air-gapped machine with nothing external.
- The **Python engine reads the same on-disk `assets/` tree** (packaged with the
  distribution). Same repo → same bytes → same digest.
- `src/brix/_java` etc. are **retired**: skins stop doing `Path(__file__).parent/...`
  and emit `AssetData("java/JavacWorker.java")` instead (§5).

Alternative considered: keep files in `src/brix/_*/` and have `brix-core`'s `build.rs`
reach up into the Python tree to embed them. Rejected — it couples the
"language-neutral" engine to the Python package's internal layout; a top-level
`assets/` is neutral to both frontends.

**Decision needed:** approve `assets/` as the home and the retirement of
`src/brix/_java|_ts|_python`, or keep the helpers in place and embed-by-reference.

### B. Resolution to a real path

Render needs an absolute filesystem path (you cannot pass bytes to `javac @file` or
`java -cp`). Two ways the engine turns a name into a path:

- **Python engine:** the `assets/` dir is on disk → `resolve_path(name)` returns
  `<assets_root>/<name>` directly.
- **Rust engine:** bytes are embedded → on first use, **materialize** into a
  content-addressed cache dir (`<cache>/assets/<digest>/<name>`) and return that path.
  Content-addressed so it is idempotent, concurrent-safe, and self-healing.

Either way the **rendered path is not fingerprint material** (already the rule for
`tool`), so the two engines rendering to different absolute paths is fine and expected.

**Decision needed:** confirm the Rust materialize-to-cache location (reuse the existing
cache root, or a dedicated `assets/` subtree under it).

### C. The digest — the soundness-critical bit

`["asset", name, digest]` where **`digest` = the content hash of the asset bytes**,
using the *same* file-content hash both engines already use for `file` atoms
(`det.hash_file` / detector `hash_file`). It must be:

- a function of **bytes only** — never the path, never mtime/stat. (Contrast `tool`,
  which fingerprints a `stat_signature` of the resolved path; assets deliberately do
  **not**, because the path differs per machine.)
- **identical** across the Rust and Python engines and across machines.

This is the one place a subtle bug (hashing a resolved path, folding in mtime, or the
two engines disagreeing on the hash of the same bytes) would silently break the shared
cache. §4 makes it the primary verified invariant.

### D. Name scheme + unknown names

- Name = POSIX relative path under `assets/`, e.g. `java/JavacWorker.java`. Stable,
  human-legible, and its own manifest.
- A name the store does not ship is a **loud error before anything runs** — both the
  fingerprint and render paths raise (`asset not shipped: <name>`), matching the
  existing "unregistered identity name raises before the build starts" behavior.

## 4. Verification strategy (first-class — this replaces Fable)

Nothing lands without a proof. In severity order:

1. **Cross-engine digest identity (primary).** A graph using an asset, fingerprinted
   under the Rust engine and the Python engine, produces a **byte-identical**
   fingerprint including the `["asset", name, digest]` triple. This is the existing
   differential-oracle machinery (regen goldens via `rust/golden/generate.py`; the
   generator imports the real lowering so fixtures and the live boundary cannot drift).
2. **Path-independence.** Fingerprint the same asset graph with the store resolving
   into two different cache/checkout locations; assert the fingerprint is unchanged.
   This is the cross-machine guarantee, tested locally.
3. **Cross-frontend identity.** The Python and TS frontends both emit
   `{kind:"asset",name}` — the existing deep-equal skin oracle (extend it to a skin
   that actually emits an asset, e.g. the migrated `java.javac_worker`).
4. **`incremental == clean`.** A build consuming an asset skips on no-change and
   **rebuilds when the asset bytes change** (edit the shipped helper → digest changes →
   rebuild). Add to the incremental equivalence suite.
5. **`test-selection == full-suite`.** Assert an asset-byte change does not corrupt the
   affected-set selector (assets are engine-shipped, so a helper edit should behave
   like any other tracked input, never silently drop a test).
6. **Unknown-name loud error.** Both engines raise before running on an unshipped name.
7. **Phase-3 audit = cloud `ultrareview`** over the finished branch — the
   Fable-substitute review for the soundness-critical code. User-triggered.

## 5. Skin migration (after the engine half is green)

Each skin swaps its machine-path helper reference for an `AssetData`. Done one at a
time; the **Python** frontend for all four is complete (commits `b3826be`, `2e16b4d`):

1. **java `javac_worker=`** — worker source + JUnit class-load recorder →
   `assets/java/JavacWorker.java`, `assets/java/junit_classload.py`. **DONE (3a).**
   Per-class Java test-impact now carries a machine-portable fingerprint.
2. **kotlin `kotlincWorker`** → `assets/java/KotlincWorker.java`. **DONE.**
3. **springboot** → `assets/java/BootRepackager.java`. **DONE.**
4. **ts import-tracking wrapper** → `assets/ts/tsc_listfiles.py`. **DONE.**

**TS frontend — deferred.** Each Python migration is meant to be mirrored in the TS
skin (drop the `throw`, emit the same `AssetData(name)`, prove by deep-equal oracle),
but that is frontend parity for TS authors and the real driver uses Python build
files, so it is deferred until TS authoring is needed. When TS `JUnitTestAction` is
wanted, the path is already established (investigated 2026-07-31): its
`ToolData(sys.executable)` for the recorder is **not** a portability wart — it is
consistent with how every `ToolData` fingerprints (resolved path + stat signature,
machine-specific). The `python.PytestAction` and `ts.CompileAction` skins already
solve the cross-frontend case with an optional `python=` param (default
`sys.executable`) whose TS mirror makes `python` required and the oracle passes it
explicitly — a shipped, sanctioned deviation. So: add the same optional `python=` to
`java.JUnitTestAction` (it is the only one of the three lacking it), then mirror it in
TS exactly like `PytestAction`. The `python=` param is the proper fix, not a band-aid.

**Cleanup — DONE (2026-07-31, commit `24bac83`).** The dead duplicate helpers under
`src/brix/_java|_ts` are deleted; `assets/` is the sole source of truth. Wheel
resolution uses **Option A — the engine serves the bytes**: `brix_py` exposes
`asset_digest`/`asset_bytes` over the copy embedded by `build.rs`, and
`src/brix/assets.py` resolves disk-first (source checkout) then falls back to the
embedded extension copy (installed wheel), materializing to a content-addressed temp
dir. No helper files ship in the wheel; the `.so` carries them. Proven by
`scripts/wheel_smoke.py` (an installed wheel resolves the helpers from the embedded
copy). `src/brix/_python/pytest_brixdeps.py` stays (module-name ref, not a path).

## 6. Implementation order (each step individually proven)

1. Create `assets/` tree; move the helpers; leave `src/brix/_*/` shims or delete per
   §3-A decision.
2. Asset store — Rust (embed + materialize) and Python (on-disk) — with `digest(name)`
   and `resolve_path(name)`; unknown-name error. Unit-test digest identity against a
   fixed byte string.
3. Rust `DataAtom::Asset` + fingerprint + render arms.
4. Python `AssetData` fingerprint + render branches.
5. Regen goldens; run the differential oracle (§4.1) + path-independence (§4.2).
6. Migrate skin #1 (java `javac_worker`); prove its skin oracle + `JUnitTestAction`
   incremental equivalence; then #2-#4.
7. `ultrareview` audit; update `docs/roadmap.md` and memory.

## 7. Open decisions for Duane (blockers to step 1)

- **§3-A:** ~~top-level `assets/` as the single source of truth + retire
  `src/brix/_java|_ts|_python`?~~ **DECIDED — `assets/` tree (2026-07-30).**
- **§3-B:** Rust materialize-to-cache location (minor; default = a dedicated
  `assets/` subtree under the existing cache root unless Duane prefers otherwise).
- Confirm the store/registry stays **engine-internal** — no new author-facing API
  surface; `AssetData` remains skin-internal. (Keeps generic classes generic.)
