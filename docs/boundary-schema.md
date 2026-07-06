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
{ "version": 1, "actions": [ <Action>... ] }
```

```
Action        = Process | Expand | Delete
Process       = { "kind": "process", "name": str, "command_line": str,
                  "inputs": { key: DataExpr }, "outputs": { key: DataExpr },
                  "env": { key: DataExpr } }          // env may be {}
Expand        = { "kind": "expand", "element": str,
                  "collection": DataExpr, "child": Action }   // child may be Expand
Delete        = { "kind": "delete", "paths": [str] }
DataExpr      = [ Atom... ]                            // a union is just the list
Atom          = { "kind": "file",  "path": str,     "identity": str? }
              | { "kind": "glob",  "template": str, "identity": str? }
              | { "kind": "regex", "pattern": str }
              | { "kind": "tool",  "name": str }
              | { "kind": "value", "text": str }
```

Notes: `path`/`template` may contain `{binding}` placeholders; `regex`
uses Python-compatible named groups (the core must match Python's `re`
semantics for the subset used: named groups, character classes,
quantifiers — no lookbehind is promised). `identity` names a registered
content-identity plugin (`docs/java-fast-builds.md`); absent means byte
hashing. The typed helpers (`cpp`, `java`) lower **before** this
boundary and never appear in it. WorkerSpec (same doc) adds
`"worker": { "name": str, "command_line": str, "inputs": {...} }` to
Process when it lands — reserve the key.

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

## Event stream (core → skin, one callback)

Every event is one object with `"event"` plus fields. `Node` refs are
`{ "id": int, "label": str, "kind": "process" | "expand" }`.

```
build_begin          { }
node_started         { node }
process_started      { node }                       // real spawn only; worker thread
node_completed       { node, status: "done"|"failed", result: ProcessResult?, error: str? }
node_skipped         { node, why: str }
collection_realized  { node, members: [str], children: [Node] }
node_inserted        { node, reason: str }
build_end            { result: BuildResult }
```

```
ProcessResult = { action_name, command, argv: [str], exit_code: int|null,
                  signal: int|null, stdout, stderr, source: "process"|"replayed"|"cache" }
BuildResult   = { success: bool, errors: [str],
                  states: { node_id: "pending"|"running"|"done"|"failed"|"skipped" },
                  process_results: [ProcessResult] }
```

**Ordering contract (decided):** all events except `process_started`
are emitted from the scheduler thread, in scheduling order — a node's
`node_started` precedes its `node_completed`; `collection_realized`
precedes its children's events; `build_begin` is first and `build_end`
is last. `process_started` is emitted from worker threads and may
interleave arbitrarily between other nodes' events, but always after
its own node's `node_started` and before its `node_completed`. The core
holds no lock while emitting; a slow consumer stalls only its own event.

## Extension traits (skin → core, optional; call shapes)

```
change_detector:   stat_signature(path) -> [int,int]|null ; hash_file(path) -> str|null
cache_store:       load(fp) -> entry|null ; restore(fp, entry, output_root) -> bool
                   store(fp, result, outputs: {logical: physical}) -> ()
content_identity:  name, version, digest(path) -> str
dependency_scanner (future): scan(node, result) -> [logical_path]
```

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
