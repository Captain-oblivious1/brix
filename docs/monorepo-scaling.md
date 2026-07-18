# Scaling to a large monorepo: hierarchical build files and lazy loading

Status: **tier 1 implemented (2026-07-17, Opus)**; tiers 2–3 designed, not built
(the risky lazy-loading core is **post-trial** — the monolithic full-load path
is the safe floor for the air-gapped trial, this is "scale after proven
correct"). Companion to `docs/roadmap.md` ("only build what my change touched"),
`src/brix/gitdetect.py` (the change source), and the `build_stack`/detector-
injection work. This note is the contract for the sequenced plan; build against
it, don't re-derive it.

Tier 1 shipped in `src/brix/workspace.py` (`find_packages`, `owner`,
`merge_graphs`, `load_workspace`, `Workspace`), proven by
`tests/test_workspace.py` — a hierarchical workspace builds **identically** to
the equivalent monolith. It is the safe, non-lazy foundation: every package is
loaded and merged, so nothing yet skips a load; the ownership map it builds is
what tiers 2–3 will use.

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
- **Incremental builds load** the union of: the `brix.py` files that *own* any
  changed file (locality), plus their **transitive dependents** walked from the
  persisted index. The index is flat persisted data, so the reverse-dep walk
  needs no `brix.py` loaded; only the affected set is then loaded to build.

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

Together — nearest-ancestor ownership + persisted reverse-dep index + loading
footprints — the affected set is provably complete.

## The change source: `GitChangeDetector`, extended

The detector (injected per the `build_stack(detector=...)` work) is the natural
source of "what changed" for the loader — but it needs a capability it doesn't
have yet. Today it answers "is *this declared input* unchanged?" (per-file
vouching inside an already-loaded graph). The loader needs "what paths changed
in the repo, period?" — a change-set **enumeration** query, independent of any
graph, to decide what to load in the first place. Same git plumbing (`status` +
`diff <baseline>..HEAD`), new method (`changed_paths()`). A non-git detector
falls back to a scan-derived change set (or, with no baseline, "everything
changed" → load all → safe).

## Sequencing and model split

1. **Hierarchical files + ownership resolution + subtree loading.** The safe,
   structural prerequisite; subsumes the monolith as the one-root-file case;
   testable on its own. **Opus. Done (2026-07-17) — `src/brix/workspace.py`.**
2. **Derived reverse-dependency index + loading-footprint tracking +
   persistence.** Correctness-sensitive; the metadata change lands in the
   reference (Python) and, later, the Rust core. **Opus build + Fable review**
   (fingerprint/staleness-adjacent, like `content_key`/`env_used`).
3. **Lazy loading (`DistributedBuilder`).** The Fable-worthy core — "did we load
   every affected package" is a completeness property ordinary tests miss on
   small graphs. **Opus builds it behind the equivalence oracle below; Fable
   reviews that core.**
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
