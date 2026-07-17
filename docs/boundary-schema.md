# The boundary schema and the TS mirror API

This pins the language-neutral contract that three consumers build
against: the Rust core's deserializer, the Python API's lowering, and
the future JS/TS API. It exists because this is the one surface that
gets expensive to change once Rust port phase 2 is built against it.
Companion to `docs/rust-port.md`; changes here require bumping
`"version"` and a deliberate decision.

Wire conventions: JSON; **snake_case keys** (matching metadata.json and
the fingerprint payloads — camelCase exists only in the TS *user-facing*
API, mapped at its edge); logical paths are POSIX (`/`) everywhere, on
every platform — conversion to native separators happens only at the
filesystem edge inside the core.

## Graph spec (skin → core, one-way, at plan/build time)

```json
{ "version": 1, "actions": [ <Action>... ], "forced_orders": [ <ForcedOrder>... ] }
```

```
Action        = Process | Expand | Delete
Process       = { "kind": "process", "name": str, "command_line": str,
                  "inputs": { key: DataExpr }, "outputs": { key: DataExpr },
                  "env": { key: DataExpr },           // env may be {}
                  "depfile": str?,                    // absent = no depfile
                  "worker": WorkerSpec? }             // absent = spawn a process
WorkerSpec    = { "name": str, "command_line": str,
                  "inputs": { key: DataExpr } }
Expand        = { "kind": "expand", "element": str,
                  "collection": DataExpr, "child": Action }   // child may be Expand
Delete        = { "kind": "delete", "paths": [str] }
ForcedOrder   = { "before": int, "after": int,
                  "trigger": "on-success" | "on-completion" }
DataExpr      = [ Atom... ]                            // a union is just the list
Atom          = { "kind": "file",  "path": str,     "content_key": str? }
              | { "kind": "glob",  "template": str, "content_key": str?,
                  "exclude": [str]? }                  // fnmatch narrowing
              | { "kind": "regex", "pattern": str }
              | { "kind": "tool",  "name": str }
              | { "kind": "value", "text": str }
```

Notes: `path`/`template` may contain `{binding}` placeholders; `regex`
uses Python-compatible named groups (the core must match Python's `re`
semantics for the subset used: named groups, character classes,
quantifiers — no lookbehind is promised). `content_key` names a content
lens — how the atom is *compared* (`docs/content-key.md`); absent means
byte hashing. The author never types the name: a skin's typed atom
carries it (`brix.java`'s `AbiFile`/`AbiGlob` → `"jvm-abi"`, written
`java.abi(expr)`). The core dispatches the name through its
[`content_key`] registry — the built-in lenses (`jvm-abi`, `ts-dts`) live
in `brix-core::lenses`, outside the neutral fingerprint path; a name the
core does not know is a loud error there, and a skin whose graph uses a
custom skin-registered lens must keep that graph on its own engine (the
Python skin's `supports_rust_engine` does exactly this). The digests are
cross-engine cache keys, so the Python implementations
(`src/brix/jvmabi.py`, `src/brix/tsdts.py`) remain the reference: an
algorithm change is a version bump in the lens's tag (`jvm-abi@1`), and a
bump in one implementation must land in both — the tag joins the cache
key, so an unsynchronized bump splits the cache rather than corrupting
it, but it must not happen. (`content_key` was the `identity` field
before the content-key relocation; the digests are byte-identical across
the rename — only the wire key and authoring surface changed.) `depfile` is the logical path of a Makefile-format dependency
file the tool writes (`cc -MMD -MF`), scanned by the incremental layer
after a successful run; it is not identity (never part of `action_key`).
It was always part of the Python `ExternalProcessAction` but was missing
from this shape when it was first pinned — added (still version 1: it is
optional and additive, both engines default it to absent) when the Rust
incremental layer ported it. `worker` routes the action's rendered argv
to a persistent worker instead of spawning (`src/brix/workers.py` pins
the request/response protocol); the spec's `command_line` + `inputs`
render into the worker's launch argv with the ordinary rules, its input
path atoms create dependency edges like the action's own, and its
identity joins the action fingerprint (a rebuilt worker jar invalidates
what the worker built). Also optional and additive — landed with the
Rust workers slice, still version 1. `exclude` on a glob atom is a list
of fnmatch patterns (`*` crosses `/`, `?`, `[...]`/`[!...]`) matched
against the workspace-relative POSIX path; a match drops the file from
membership (`find`), while `match`/`substitute`/overlap checks ignore it
(excludes only shrink a set, so edge inference stays conservative). It
joins fingerprints and identities only when non-empty, so pre-exclude
graphs hash byte-identically — optional and additive, still version 1
(`docs/content-key.md` has the design). The typed helpers (`cpp`, `java`)
lower **before** this boundary and never appear in it.

`forced_orders` is optional and additive (absent means `[]`, so this remains
version 1). Its integer fields are indices into `actions`. It represents the
exceptional case where two actions must be ordered without sharing data;
ordinary dependencies continue to be inferred from data overlap. Targeting an
`after` action also selects its `before` action. `on-success` is the default;
`on-completion` allows the later action to run after either success or failure.

## Build config (skin → core)

```json
{
  "root": "/abs/workspace",
  "jobs": null,
  "targets": null,
  "layers": [
    { "kind": "cache",       "cache_dir": "build/.brix-cache" },
    { "kind": "incremental", "metadata_dir": "build/.brix" },
    { "kind": "atomic",      "staging_dir": "build/staging", "hold": false }
  ]
}
```

`layers` is **innermost-first**, exactly like `layers()` today; the
recommended order is the one shown. `targets` is a list of output paths
(alias resolution is a skin concern and stays out of the core).

**Ordering rule — the atomic layer must be outermost.** If a `{ "kind":
"atomic" }` entry is present it must be the **last** entry (outermost, since the
list is innermost-first); the core rejects any other position. The atomic layer
establishes the staging overlay and its build carries the P0 staging-hazard
guard and the commit/discard workflow, so nothing may sit outside it. This rule
lives in the core so **every frontend inherits it** rather than reimplementing
it — a frontend should *also* express it in its own type system for early
feedback (the Python skin makes `AtomicBuilder` non-nestable so a type checker
flags a buried stack; a TS skin would use a branded type), but the core is the
authoritative backstop.

## Event stream (core → skin, one callback)

Every event is one object with `"event"` plus fields. `Node` refs are
`{ "id": int, "label": str, "kind": "process" | "expand" }`.

```
build_begin          { plan: { nodes: [Node] } }
node_started         { node }
process_started      { node }                       // real spawn only; worker thread
node_completed       { node, status: "done"|"failed", result: ProcessResult?, error: str? }
node_skipped         { node, why: str }
collection_realized  { node, members: [str], children: [Node] }
node_inserted        { node, reason: str }
build_end            { }        // marker only; the full result is build()'s return value
```

```
ProcessResult = { action_name, command, argv: [str], exit_code: int|null,
                  signal: int|null, stdout, stderr, source: "process"|"replayed"|"cache" }
BuildResult   = { success: bool, errors: [str],
                  states: { node_id: "pending"|"running"|"done"|"failed"|"skipped" },
                  node_results: { node_id: ProcessResult },  // per-node result where one exists
                  process_results: [ProcessResult],
                  plan: { nodes: [Node] } }          // final plan, inserted/expanded nodes included
```

`build_begin.plan` carries the plan as first scheduled; `BuildResult.plan`
carries the final plan (nodes inserted or expanded during the build
included), which is why the skin synthesizes its own `build_end`
reporter call from `build()`'s return value rather than from the marker
event. `node_results` is the per-node view of `process_results` (the
skin's state table answers `states.result(node_id)` from it). All three
additions are additive (still version 1): a consumer that ignores them
sees the original shapes.

**Ordering contract (decided):** all events except `process_started`
are emitted from the scheduler thread, in scheduling order — a node's
`node_started` precedes its `node_completed`; `collection_realized`
precedes its children's events; `build_begin` is first and `build_end`
is last. `process_started` is emitted from worker threads and may
interleave arbitrarily between other nodes' events, but always after
its own node's `node_started` and before its `node_completed`. The core
holds no lock while emitting; a slow consumer stalls only its own event.

## Status and clean (skin → core, no-build entry points)

`status(spec, config)` and `clean(spec, config)` take the same graph spec
as `build` with a smaller config — `{ "root": str, "metadata_dir": str?,
"staging_dir": str?, "targets": [str]|null }` (directory defaults as in
the layer config; `clean` ignores `staging_dir`/`targets`). Nothing
executes. Rendering and exit codes stay in the skin.

```
StatusReport  = { actions: [ { label, state, detail,
                               last_result: ProcessResult|null } ],
                  pending_deletions: [ { expansion, member, paths: [str] } ],
                  staged_files: [str], staged_deletions: [str],
                  interrupted_commit: bool, held: bool }
CleanResult   = { removed: int }      // files deleted; metadata dir removed too
```

`state` is one of `fresh`, `never built`, `inputs changed`, `outputs
missing or modified`, `failed last build`, `outdated` — exactly the
strings `src/brix/status.py` defines (they are user-visible in the CLI).
Additive, still version 1.

## Extension traits (skin → core, optional; call shapes)

```
change_detector:   stat_signature(path) -> [int,int]|null ; hash_file(path) -> str|null
cache_store:       load(fp) -> entry|null ; restore(fp, entry, output_root) -> bool
                   store(fp, result, outputs: {logical: physical}) -> ()
content_identity:  name, version, digest(path) -> str
dependency_scanner (future): scan(node, result) -> [logical_path]
```

`cache_store` and `content_identity` are implemented (phase 4b): a
custom store or plugin rides `build`/`status` as callables the core
invokes across the boundary. Contract notes, binding for every skin:
paths cross as strings; a store's `entry` must be plain JSON-able data
(an entry that isn't serializes to an error, which the cache layer
treats as a miss); `restore`'s third argument is the output root — the
Python skin wraps it in an output-root-only context for existing
`CacheStoreProtocol` stores, and no richer context crosses; a raising
`digest` means "cannot interpret this file" and falls back to `tag:raw`
byte hashing, while an *unregistered* identity name is a loud error
before anything runs. Callbacks arrive on build worker threads, and an
exception from a store is a cache miss / skipped populate, never a
failed build — the same resilience contract the layers have in-process.
`change_detector` has no skin-side seam yet; `dependency_scanner`
remains future.

---

# The TS mirror API

**Principle: the TS API is the Python API, transliterated.** Same class
names, same field names, same graph model, same builder nesting, same
CLI verbs, same docs. A brix user switches languages by switching
syntax, not by relearning the library. Deviations from Python require
being on the sanctioned list below; "more idiomatic" is not a reason.

Sanctioned deviations (exactly three):

1. **Casing**: snake_case becomes camelCase mechanically
   (`command_line` → `commandLine`, `element_name` → `elementName`).
   One rule, no exceptions, stated once in the docs.
2. **`build()` is async** (process spawning must not block the event
   loop): `await builder.build({ jobs, targets, reporter })`. Same for
   `commit()`/`discard()` and `cli.main()`.
3. **Reporters are plain objects** with optional methods — no subclassing:
   `{ processStarted(node) {...}, buildEnd(result) {...} }`.

Everything else mirrors. Side by side:

```python
graph.add(ExpandAll(
    element_name="name",
    collection=data.glob("src/{name}.txt"),
    child=ExternalProcessAction(
        name="upper {name}",
        command_line="{python} {tool} {source} {dest}",
        inputs={"source": data.file("src/{name}.txt"), ...},
        outputs={"dest": data.file("build/{name}.txt")},
    )))
builder = AtomicBuilder(CacheBuilder(IncrementalBuilder(create_graph)))
```

```ts
graph.add(new ExpandAll({
    elementName: "name",
    collection: data.glob("src/{name}.txt"),
    child: new ExternalProcessAction({
        name: "upper {name}",
        commandLine: "{python} {tool} {source} {dest}",
        inputs: { source: data.file("src/{name}.txt"), ... },
        outputs: { dest: data.file("build/{name}.txt") },
    })}));
const builder = new AtomicBuilder(new CacheBuilder(new IncrementalBuilder(createGraph)));
```

Constructors take a single options object (TS has no keyword
arguments); `{binding}` pattern syntax is brix's own and carries over
verbatim. A project's `brix.ts` ends the same way `brix.py` does:
`process.exit(await cli.main(createGraph, { root, argv, aliases,
commands }))`.

Type-level niceties (input/output key checking against command
templates via template-literal types) are welcome as *additions* — they
must never change the runtime surface.
