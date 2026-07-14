# Brix specification

## Design rules

Settled — a fresh session should treat these as fixed unless Duane reopens
them:

1. **The logical graph is clean-build truth.** It never contains
   user-authored nodes for stale-output removal, failed-output deletion,
   cache lookup/restore, diagnostics replay, staging, commit, or recovery.
   Those are capabilities layered on during execution.
2. **Incremental behavior must match clean-build output.** An incremental
   build produces the same final output state a clean build would — which
   forces deleting outputs that no longer belong, deleting failed actions'
   outputs, and rebuilding downstream consistently.
3. **Layers are per-node middleware, not static plan transforms.** The plan
   grows during execution (ExpandAll), so a one-shot
   `ExecutionPlan → ExecutionPlan` rewrite misses runtime-expanded nodes.
   Instead: an `around_node` stack (innermost = process run) plus lifecycle
   events (`on_build_begin`, `on_collection_realized`, `on_build_end`).
   Stack order mirrors builder nesting: `atomic(cache(incremental(run)))`.
   Full design: `docs/build-layers.html`.
4. **Nodes are effectively immutable.** The plan is append-only (nodes and
   edges are only added, via the scheduler's single insertion channel); node
   definitions are frozen at insertion; node status is monotone
   (pending → running → done/failed/skipped). Expansion is refinement, not
   replacement; data realization is resolution (membership unknown → known),
   and the collection identity survives — it's what membership history
   attaches to across builds.
5. **Logical vs physical paths.** Declared inputs/outputs like
   `build/obj/foo.o` are logical names (used by inference, cache keys,
   membership diffs). Layers never rewrite them; `NodeContext`
   (`workspace_root`, `output_root`) maps logical → physical at render time.
6. **Cleanup is not user-authored.** Deleted-source and failed-output
   deletions are layer-proposed *real nodes*, inserted through the same
   channel as expansion. Failure-triggered cleanup uses an edge trigger
   mode: `on-success` (default) vs `on-completion`.
7. **Every shortcut is a result obtained elsewhere.** Up-to-date, cache hit,
   or process run all end in a `ProcessResult`; downstream can't tell the
   difference. Failed results are real results — cacheable, replayable, and
   they cascade normally.
8. **Cache and atomicity are independent.** Neither knows the other exists;
   atomic-outermost stack order alone routes cache-restored outputs through
   staging.
9. **One underlying model.** Typed language helpers (C++, Java, …) lower to
   the same generic action/data representation as `ExternalProcessAction`;
   raw and typed forms compose in one graph.
10. **Generated files must be representable before they exist**, and
    `ExpandAll` expands into schedulable graph nodes, not an opaque loop.
11. **The engine never prints.** Output is a client decision: the scheduler
    emits events to a `Reporter`, output styles are Reporter
    implementations, and results carry provenance (`process` / `replayed` /
    `cache`) so reporting can be honest without scheduling caring how a
    result was obtained. Silent + exit code is a first-class mode.
12. **Python owns user composition; Rust eventually owns the engine** —
    metadata, graph algorithms, scheduling, caching, fingerprints, and
    performance-critical execution live below the DSL boundary.
13. **Uniform capabilities across languages — no per-language feature matrix.**
    A correctness *feature* (atomic staging, caching, incrementality,
    sandboxing) either works for every language or is off for every language;
    languages may differ in *coverage* (which ecosystems have a resolver) and
    *optimization* (which have a semantic lens or a warm worker), never in
    whether a correctness feature applies. The engine stays language-neutral;
    everything language-specific is a plugin over a small fixed seam set
    (content lens, dependency resolver, dependency-discovery reader, warm
    worker, typed helper — `docs/capability-audit.md`). Corollary: where a
    feature *cannot yet* apply uniformly it is **refused loudly and
    identically** for every language, never silently allowed for some — e.g.
    the atomic builder rejecting a tool-self-resolved intra-build artifact
    (`atomic.staging_hazards`) rather than shipping a stale result. A lens is
    only as trustworthy as its equivalence-oracle: a lens that is *unsound* for
    a language (Kotlin `inline`, C++ templates) is a correctness bug, not a
    missed optimization, so each ships an oracle covering that language's
    cross-module-inlining case.

---

# Specification

Everything below is the reference spec (originally `freshStart.md`), updated
to match the current design.

## What Brix does

Users define build graphs in ordinary Python; the engine handles:

- content-hash-based incremental builds
- generated files
- stale output cleanup
- process result persistence
- cache lookup and restore
- stdout/stderr replay
- atomic/staged output commits
- graph visualization (future)
- status reporting

The same user graph must work unchanged under any capability combination:
clean, incremental, cached, atomic, or cached-atomic-incremental.

## Expected user file shape

A project usually has a `brix.py` with three independent responsibilities
(in any order):

1. **Capabilities** — which builder/layers are used.
2. **Dependency graph** — what data exists, what actions consume and
   produce. Clean-build truth.
3. **CLI** — parse project arguments, map aliases to targets, invoke the
   selected builder.

The target composition (builder nesting = layer stack order):

```python
def create_builder():
    change_detector = TimeAndHashChangeDetector(root=".")
    change_set = change_detector.detect_changes()

    return AtomicBuilder(
        CacheBuilder(
            IncrementalBuilder(
                change_set=change_set,
                graph_supplier=create_graph,   # callable, so the builder
            ),                                 # decides how much graph to load
            cache_dir="build/.brix-cache",
        ),
        staging_dir="build/staging",
    )
```

For a simple project that doesn't care about incremental builds:

```python
CleanBuilder(create_graph()).build()
```

Builders expose `plan() -> ExecutionPlan` and `build() -> BuildResult`.

## Logical graph model

The user declares a **logical graph** of two node families:

- **Data** — files, file collections, tools, literal values; later:
  in-memory values, database-backed sets.
- **Actions** — generic external processes, typed language actions,
  repeated-work expansion points.

A common chain is `data -> action -> data -> action -> data`, but a graph
may start with an action. The graph is not a parallel build script: it
declares what actions consume and produce, and the engine **infers**
dependency edges from data-set intersections.

## Data expressions

```python
data.file("src/main.cpp")        # one logical path (need not exist yet)
data.glob("src/{name}.cpp")      # path set with named bindings
data.regex(r"src/(?P<name>[0-9]+)\.cpp")   # for patterns glob can't express
data.tool("c++")                 # tool identity (later: path, version, …)
data.value("-O2 -std=c++20")     # literal data that affects outputs
```

- `glob` bindings are **recursive by default** — a bare `{name}` matches
  across `/` (like make's `%`), so `build/{rel}.class` matches both
  `build/App.class` and `build/com/foo/Bar.class`. Write `{name:*}` to pin a
  binding to a single path segment; `{name:**}` is the explicit recursive form.
  For anything richer than that, drop to `data.regex` and write a full regex.
- `value` exists so options/flags/env are hashable inputs without users
  creating JSON option files.
- Unions compose sets:
  `data.glob("src/{n}.cpp") + data.glob("build/generated/{n}.cpp")`.

Internally all path-like expressions lower into one interned `PathPattern`
representation (`src/brix/patterns.py`): template, binding names, compiled
matcher, literal prefix. Pattern-vs-pattern dependency inference is
conservative: provably disjoint → no edge; otherwise keep a potential edge
and resolve when a side realizes. Planned for scale: exact-path map,
prefix/segment tries, suffix buckets, matcher cache.

## Actions

### ExternalProcessAction

```python
ExternalProcessAction(
    name="compile {name}",            # optional; label falls back to command_line
    command_line="{compiler} {flags} -c {source} -o {object}",
    inputs={
        "compiler": data.tool("c++"),
        "flags": data.value("-O2"),
        "source": data.file("src/{name}.cpp"),
    },
    outputs={"object": data.file("build/obj/{name}.o")},
)
```

`command_line` is a template; placeholders refer to named inputs/outputs and
are rendered lazily (declared-but-unreferenced collections are fine). Every
run captures stdout, stderr, exit code, and termination signal as a
`ProcessResult`; results are persisted (with the incremental/cache layers)
and replayed on cache hits — cached failures replay their diagnostics and
fail the build the same way.

### Typed language actions (future)

Strongly typed, ergonomic helpers — no magic string keys:

```python
cpp.CompileSourceAction(
    source_files=data.glob("src/{name}.cpp"),
    object_files=data.glob("build/obj/{name}.o"),
    compiler=data.tool("c++"),
    compile_options=["-O2", "-std=c++20"],
)
cpp.LinkApplicationAction(
    object_files=data.glob("build/obj/{name}.o"),
    output_file=data.file("build/bin/app"),
    linker=data.tool("c++"),
)
java.JarAction(
    source_files=data.glob("src/main/java/{cls}.java"),
    output_file=data.file("build/libs/app.jar"),
    javac=data.tool("javac"), jar_tool=data.tool("jar"),
)
```

These lower to the same generic model, so raw and typed forms compose — the
proving example: a C++ tool is compiled and linked, runs to generate Java
sources, and a `JarAction` consumes
`data.glob("src/main/java/{cls}.java") + data.glob("build/generated/java/{cls}.java")`.
One graph, three kinds of action, no separate build systems.

### ExpandAll

```python
ExpandAll(
    element_name="name",
    collection=data.glob("src/{name}.cpp"),
    child=ExternalProcessAction(...),      # templated over {name}
)
```

`ExpandAll` is **not an opaque loop**. It sits in the plan as a single
unrealized node until its collection membership is known, then expands into
concrete sibling action nodes (`compile foo`, `compile bar`, …) that
schedule and run in parallel like any other node. The `ExpandAll` node
itself stays in the plan, marked done (see design rule 4).

## Generated files

The user writes a clean logical graph — a generator action whose output is
a glob, and an `ExpandAll` over that glob:

```python
graph.add(
    ExternalProcessAction(
        name="generate sources",
        command_line="{python} {generator} {csv} {out_dir}",
        inputs={...},
        outputs={"generated": data.glob("build/generated/{name}.cpp")},
    ),
    ExpandAll(
        element_name="name",
        collection=data.glob("build/generated/{name}.cpp"),
        child=ExternalProcessAction(...),
    ),
)
```

At plan time the collection and the `ExpandAll` are unrealized and the
downstream link is blocked. After `generate sources` runs, the collection
realizes, the `ExpandAll` expands, and downstream actions wait for the
concrete members. No user-declared "graph-expanding node" is needed. This
works today (see `tests/test_execution.py`).

## ExecutionPlan

The execution graph for one build invocation; may be partially realized.

Current implementation (`src/brix/plan.py`): nodes are **actions** —
`PROCESS` (concrete process) or `EXPAND` (unrealized expansion point) —
plus edges with a reason. Data expressions live on actions as declared
inputs/outputs; collections realize implicitly at expansion/render time.
(The original spec also listed realized/unrealized *data* as plan node
categories; whether data become first-class plan nodes is an open modeling
question — the ground rules in design rule 4 cover them either way.)

Current shape (per `docs/build-layers.html`):

```text
ExecutionPlan            # append-only
  nodes: [ExecutionNode] # definitions frozen at insertion
  edges: from -> to, reason, trigger: on-success | on-completion

NodeStateTable           # scheduler-owned
  node id -> status, result        # pending -> running -> done|failed|skipped
```

The scheduler owns readiness, failure cascade, and the single insertion
channel (used by both expansion and layer-proposed cleanup nodes). Nodes
execute and report results; they never mutate graph structure.

## Builders and capability layers

Root builders (`CleanBuilder`, `IncrementalBuilder`) lower the logical graph
into an `ExecutionPlan`. Capability layers (incremental, cache, atomic) are
**per-node middleware plus lifecycle events** — *not* plan-to-plan
transforms (design rule 3; full rationale and diagrams in
`docs/build-layers.html`):

```python
class BuildLayer:
    def on_build_begin(self, plan, ctx): ...          # atomic: staging/recovery
    def around_node(self, node, ctx, inner): ...      # skip / restore / redirect / run
    def on_collection_realized(self, node, members, ctx): ...  # membership diff
    def on_build_end(self, result, ctx): ...          # atomic: commit
```

- **CleanBuilder** — no change detection; may rebuild everything. Can still
  be wrapped by cache or atomic.
- **IncrementalBuilder** — takes a `ChangeSet` and a `graph_supplier`
  callable (so large projects can load only affected build modules).
  Degrades safely to clean behavior if metadata is missing; if metadata is
  missing but outputs exist, error and require explicit clean/adopt.
- **CacheBuilder** — transparent except for speed; cache key ≡ incremental
  fingerprint (one shared function over command template, tool identity,
  input hashes). Restores results and outputs, replays stdout/stderr,
  replays failures as failures.
- **AtomicBuilder** — outermost, so process outputs *and* cache-restored
  outputs go through staging. Conceptual staging:
  `build/staging/add/…` (files to add/replace) and `build/staging/del/…`
  (deletion markers). All work succeeded → commit; otherwise real outputs
  stay untouched and staged state remains inspectable for status/recovery.

## Incremental build semantics

Persistent metadata must include: file content hashes, cheap scan data
(mtime/size), action fingerprints (command/tool/options/env), discovered
dependencies, declared and extra owned outputs, generated-collection and
expansion membership, and previous process results.

The three defining scenarios:

- **Changed file** — `src/foo.cpp` changed → recompile `foo.o`, relink
  downstream.
- **Failed compile** — remove `build/obj/foo.o`, mark downstream invalid,
  remove/fail downstream outputs exactly as a clean build would have. Never
  keep a stale "last good" output the clean build wouldn't have produced.
- **Deleted source** — previous membership `{foo, bar, baz}`, current
  `{foo, bar}` → delete `build/obj/baz.o` and rebuild collection consumers.
  Requires persisted membership history; file hashes alone can never detect
  this. The deletions enter the plan as layer-proposed nodes (design
  rule 6).

## Change detection

Default: `TimeAndHashChangeDetector(root=".")` — workspace-relative paths,
timestamp/size as cheap scan, content hash when needed, ignores build
metadata directories, follows file symlinks as ordinary files, rejects
case-only path collisions. Future detectors: filesystem watcher, FUSE-like
live tracker, source-control based, remote metadata service.

## Modular build files

A graph-construction feature, orthogonal to incremental builds. Simple
projects: one `brix.py`, ordinary Python imports. Large projects (later):
`ChangeSet → DistributedGraphLoader → affected logical graph →
IncrementalBuilder`.

## CLI direction

Brix is primarily a library; projects may expose `brix.py`. CLI parsing
(`build`, `status`, `clean`, `graph`, alias→target mapping, project
arguments) stays out of `Graph` and builders.

## Class inventory

Superseded by [docs/class-diagrams.md](class-diagrams.md), which
diagrams the engine's classes in `src/brix/` (grouped: data model,
actions, execution plan, builders & layers, support). Not yet in a
diagram: the JVM phase 1–2 additions (`identity.ContentIdentity` /
`JvmAbiIdentity`, `jvmabi`, `workers.WorkerSpec` / `Worker` / `WorkerPool`
/ `WorkerRegistry`), plus still-planned `ChangeSet` (a pluggable
change-detector abstraction — see TODO) and
`brix.cpp.CompileApplicationAction`. A `content identity` + `workers`
diagram group is a good small follow-up.

---

## Layout

The DSL layer (`src/brix/data.py`, `actions.py`, `graph.py`) is the
user-facing declaration API and stays in each host language. The engine
layer (`planner.py`, `plan.py`, `render.py`, `executor.py`, `process.py`)
consumes the logical graph and is the part scheduled to move to Rust; keep
the boundary between the two clean.

## Process notes

- Design changes: short back-and-forth discussion first; only edit docs or
  code after agreement.
- Duane is a visual guy — prefer diagrams; `docs/build-layers.html` is
  viewed directly in a browser.
- **Update the project documents at every commit** — move finished roadmap
  items to the development log, reorder priorities, and record newly settled
  decisions in the appropriate specification or design document.
