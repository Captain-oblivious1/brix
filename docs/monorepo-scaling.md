# Scaling to a large monorepo: hierarchical build files and lazy loading

Status: **all three tiers implemented** — tier 1 (2026-07-17, Opus), tier 2
(2026-07-17, Opus), tier 3 (2026-07-18, Fable: `src/brix/affected.py`,
`load_affected`, gated by `tests/test_affected.py`). Lazy loading is **opt-in**
— `load_workspace` (full load) stays the default, and *relying* on lazy loading
in anger remains post-trial: the monolithic full-load path is the safe floor
for the air-gapped trial. Companion to `docs/roadmap.md` ("only build what my
change touched"), `src/brix/gitdetect.py` (the change source), and the
`build_stack`/detector-injection work. This note is the contract for the
sequenced plan; build against it, don't re-derive it.

## Measured (2026-07-18): the construction win is real; the selector is O(n²)

`examples/lazy_loading_benchmark.py` isolates graph-construction time, full-load
vs affected-only, across a package-count ladder (20 actions/package):

| packages | full construct | affected construct | speedup | select (1 call) |
|---:|---:|---:|---:|---:|
| 10 | 2.9 ms | 0.34 ms | 8.6× | 82 ms |
| 25 | 18 ms | 0.49 ms | 37× | 0.70 s |
| 50 | 38 ms | 0.75 ms | 50× | 2.8 s |
| 100 | 77 ms | 1.25 ms | 62× | 11 s |

The **thesis holds**: full-load construction grows ~linearly with package count
while affected-only stays flat, so the construction speedup *rises* with repo
size (the O(whole-repo) cost lazy loading removes). **But a defect surfaced**:
`select_affected` re-derives every cross-package edge on each call
(`_derive_deps_from_entries`, O(packages² × modules²), regex-compiling), so the
*selection* grows ~quadratically and dominates past ~a dozen packages — 11 s at
100, which dwarfs the ~75 ms it saves. Net: the loading win is real, but as
shipped the selector is the bottleneck, not the loading.

**Fix (planned):** persist the derived `depends_on` edges in the index at
full-build time (they are computed there already), so `load_index_data` reads
them O(packages) instead of re-deriving. That converts selection to roughly
O(packages + changed×patterns) and lets the construction win actually land. Until
then, lazy loading is best kept to small package counts.

Tier 1 shipped in `src/brix/workspace.py` (`find_packages`, `owner`,
`merge_graphs`, `load_workspace`, `Workspace`), proven by
`tests/test_workspace.py` — a hierarchical workspace builds **identically** to
the equivalent monolith. It is the safe, non-lazy foundation: every package is
loaded and merged, so nothing yet skips a load; the ownership map it builds is
what tiers 2–3 will use.

A runnable end-to-end demonstration lives in `examples/hierarchical/` — a
`core → api → web` chain plus an independent `docs` package and one shared tool,
with `verify_lazy.py` proving that a scoped edit loads only the affected
packages (and that each subset build is byte-identical to a full build). It is
the adoption template: the shape a real service tree copies.

## The problem

With a single root `brix.py`, every build — even a no-op — runs `create_graph`
for the *whole* repo: it constructs every service's action objects and plans
across the entire tree. On a ~50-service monorepo that O(whole-repo) **graph
construction** is the cost that doesn't shrink with the size of the change.
Incremental change detection (plus the git detector) already skips
re-*execution* and re-*hashing*, so the remaining expense a small edit pays is
almost entirely *building the graph it then mostly ignores*.

## The model: hierarchical build files (the Bazel package model)

Split the monolith into `brix.py` files placed at natural module/service
boundaries. Each is a **package**: it is responsible for the files in its
subtree **until interrupted by another `brix.py` deeper down**, which carves out
its own sub-responsibility. Not every directory needs one — placement is sparse,
at boundaries.

- **Ownership is "nearest ancestor."** The `brix.py` owning a path is the
  closest one at or above that path's directory. A whole service's changed files
  collapse to that service's single `brix.py`.
- **One `brix.py` per boundary** so ownership is unambiguous (a file belongs to
  exactly one package, as in Bazel).
- **The monolith is the degenerate case.** One root `brix.py` = today's
  behavior, exactly. Adding a deeper `brix.py` is a purely incremental opt-in to
  finer granularity — no migration, no all-or-nothing switch. A repo sits
  anywhere on the spectrum from "one file, load everything" to "fully
  distributed" and moves gradually. This is the adoption path for the air-gapped
  monorepo: start with one root file (everything already built works unchanged),
  carve into packages later, only once the fundamentals are trusted there.

## The value is lopsided — build the tiers in value order, not sequence order

Three tiers save progressively more, but **not evenly**, and the plan must
respect that or it wastes effort on the thin tier:

1. **Incremental + git detector (done).** Skips re-execution and re-hashing.
2. **Affected-target computation on the fully-loaded graph.** Skips the
   fingerprint-walk of unaffected nodes — but git already made that walk cheap,
   so the marginal win is **thin**. *Deprioritized: build only if a measurement
   shows the fingerprint-walk is a real bottleneck.*
3. **Lazy loading — don't construct the unaffected `brix.py` at all.** This is
   the tier that removes the O(whole-repo) construction cost, i.e. **the actual
   monorepo win.** It is also the correctness-critical, Fable-worthy part.

So the effort goes to tiers 1 (have it) and 3 (want it); tier 2 is a measured
maybe, not a stepping stone.

## The mechanism: derive dependencies, never redeclare them

The cross-module dependency information **is already present** in the data
references — `app`'s `brix.py` declares `classpath=[core.jar]`, `core`'s declares
it builds `core.jar`, and brix already infers that edge when both are loaded.
So there is **no need for explicit cross-module dependency declarations.**
Instead:

- **Derive a reverse-dependency index from a full build** (who consumes whose
  outputs) and **persist it** in the metadata.
- **Bootstrap it on the first/full build** — the one time the whole graph is in
  hand. Chicken-and-egg dissolved: you record the map exactly when you have it.
- **Incremental builds load** the union of: the packages whose persisted
  input/output patterns (or construction footprint) match any changed path,
  plus their **transitive dependents** walked from the persisted index. The
  index is flat persisted data, so the match and the reverse-dep walk need no
  `brix.py` loaded; only the affected set is then loaded to build.

**Correction discovered during tier 3 (load-bearing):** the original sketch
said "the `brix.py` files that *own* any changed file (locality)". That is
**unsound**, because tier 1 deliberately does not enforce a locality rule —
packages may reference paths outside their subtree (a shared `tools/` script is
an input of many packages, and lives in *no* package's subtree in a sparse
layout). Ownership-based selection would see no owner and pick nothing.
Selection is therefore **pattern-based**: each package's index entry persists
its input/output path patterns, extracted with the planner's own atom helpers
(worker inputs included) — the same atoms edge inference and re-fingerprinting
look at, so selection cannot drift from them. Glob excludes are dropped when
matching (over-match only ever over-loads). The cross-package edges are
likewise derived *from the persisted patterns* (`patterns.overlaps`, no shared
bindings — conservative), so a subset load can refresh its own entries and the
edges stay consistent without loading anything else.

## The correctness crux: completeness under partial loading

Loading a subset is correctness-critical in the worst way — **load too few
`brix.py` files and you silently ship a stale build.** The governing invariant
is an asymmetry:

> **Over-loading is a wasted parse (harmless); under-loading is a stale build
> (fatal).** Every uncertainty resolves toward loading *more*.

No index yet → load everything. Footprint changed → reload. Can't *prove* a
package unaffected → load it. Built this way, lazy loading only ever *removes*
loads it can prove unnecessary, and the "silent stale" hazard becomes a bounded,
testable property.

Two dependency-change cases, and why the index alone isn't quite enough:

- **Static deps** (`classpath=[core.jar]`): changing them means *editing the
  `brix.py`*, so that file is in the change set and gets loaded anyway. Airtight
  with the index alone.
- **Dynamic deps** (`classpath=glob("vendor/*.jar")`, or deps read from a config
  file): the dependency set changes when a *file* changes, not when the
  `brix.py` text does. A stale index could miss it. **Fix: track each
  `brix.py`'s loading footprint** — the globs and files it read while
  constructing its slice — and reload it if its own file *or* any footprint
  entry changed. This is brix's existing discovered-inputs mechanism (compiler
  depfiles) turned on the build files themselves — **meta-incrementality**, not
  new invention.

Together — persisted per-package patterns + derived reverse-dep index + loading
footprints — the affected set is provably complete. The shipped selector
(`brix.affected.select_affected`) adds the remaining FULL-load fallbacks:
no index; any `brix.py` itself added/removed/edited (structure may have
changed); a changed path in the discovered-deps table (compiler-scanned inputs
are keyed per action, unattributable to a package without loading it); a
changed path outside the workspace; and failed-build recovery (the previous
selection — `last_selected`, written *at load time* — is unioned back in when
the last build left failed actions, or FULL if it was never recorded).

## The change source: `GitChangeDetector`, extended

The detector (injected per the `build_stack(detector=...)` work) is the natural
source of "what changed" for the loader. **Done (2026-07-18, Fable):**
`changed_paths()` answers the change-set **enumeration** query — "what paths
changed since the last build, period?", independent of any loaded graph — from
the same git plumbing (`status` + `diff <baseline>..HEAD`), returning None
whenever git can't answer (→ load all → safe). Building it surfaced and fixed a
real staleness hole in the *vouching* path too: a file dirty at build N was
hashed with content git never saw, so if it is reverted to HEAD content before
build N+1, git reports it clean while the cached hash is stale. The fix pairs
the persisted baseline with **that build's working-tree dirty set** — a file in
the previous build's dirty set is never vouched and always counts as changed
(`tests/test_git_detect.py` pins the revert case in the git/stat equivalence
sequence).

## Sequencing and model split

1. **Hierarchical files + ownership resolution + subtree loading.** The safe,
   structural prerequisite; subsumes the monolith as the one-root-file case;
   testable on its own. **Opus. Done (2026-07-17) — `src/brix/workspace.py`.**
2. **Derived reverse-dependency index + persistence.** **Done (2026-07-17) —
   `brix.workspace`** (`_derive_package_deps`, `Workspace.depends_on` /
   `dependents_of` / `save_index`, `load_index`). The cross-package graph is
   derived from the actions' data references using the planner's own
   `infer_dependency` (no declaration), and persisted to
   `build/.brix/workspace-index.json`. Safe to build ahead of the trial because
   **nothing consumes it yet** — it is derived and saved, never used to skip a
   load, so there is no staleness surface (the same "record now, act later"
   shape as environment-recording phase 1). **Loading-footprint tracking**
   (reload a package if a glob/file its construction read changed) is deferred
   to land *with* tier 3, since it is only load-bearing once lazy loading
   actually consumes the index; that is the Opus-build + Fable-review piece.
3. **Lazy loading.** The Fable-worthy core — "did we load every affected
   package" is a completeness property ordinary tests miss on small graphs —
   so it was **Fable-authored**, behind the equivalence oracle. The split
   within tier 3:
   - **Mechanical scaffolding (Opus, done 2026-07-17):** the subset-load
     primitive `load_workspace(root, only=…)` (loads exactly the packages it is
     told — no decision), loading-footprint tracking (`Workspace.footprints`, an
     audit-hook recorder that captures the files a package's `create_graph` read
     at construction, so a dynamic-config read triggers a reload), and the
     **equivalence oracle** (`tests/test_workspace_oracle.py`:
     `assert_selective_equals_full` — replays edits full-load vs selector-driven
     and asserts byte-identical outputs; proven to catch a stale-producing
     selector and pass correct ones).
   - **The completeness core (Fable, done 2026-07-18):** `src/brix/affected.py`
     — `select_affected` (pattern + footprint matching, dependents closure over
     the pattern-derived edges, FULL fallbacks listed above) and
     `load_affected(root, changed=None)`, the composition entry point (there is
     no `DistributedBuilder` class — composition over a builder wrapper, per
     the detector-injection precedent): enumerate changes (explicit, or
     `GitChangeDetector.changed_paths()`), select, subset-load, refresh the
     loaded packages' index entries (`save_index` merges; `last_selected` is
     written at load time for failed-build recovery). Gated by
     `tests/test_affected.py`: the real selector replayed against a full load
     across the whole edit alphabet — multi-hop ripple, shared out-of-subtree
     tool edit, dynamic-config (footprint) change, glob-member deletion, new
     package, dependency rewired via `brix.py` edit, irrelevant edit — plus a
     negative control proving the harness still catches an under-selector, and
     unit tests for every FULL fallback. Index format v2
     (`workspace-index.json`): per-package patterns + footprint entries,
     `last_selected`; v1 files degrade to a full load.
4. *(Affected-computation on the full graph — tier 2 above — only if measured to
   matter.)*

## The gate: an equivalence oracle (this is what makes it safe)

The go/no-go is not "trust the careful model" — it is a **mechanical proof**: a
lazy-loaded build must produce **byte-identical results to a full-load build**
across real edit sequences (edit within a package, add a cross-package
dependency, edit a dynamic-glob input, delete a package, a multi-hop dependent
chain). This is the same discipline the git detector's equivalence oracle uses.
If that oracle can't be made solid, that is the signal the design isn't ready —
regardless of which model touches it.

## What this plan explicitly does *not* require

- **No redundant cross-module dependency declarations** — derived from the data
  references that already exist.
- **No `brix.py` in every directory** — sparse placement at package boundaries.
- **No pre-trial adoption** — the monolithic full-load path is the safe floor;
  distribution is grown into afterward.
