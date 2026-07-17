# Python API

The names exported directly from `brix` are the supported v1 Python API. The
contract is intentionally smaller than the implementation: build files should
not need to know about execution nodes, scheduler state tables, concrete layer
objects, or metadata storage.

## Build authoring

- `Graph`
- `ExternalProcessAction`, `ExpandAll`, and `Expandable`
- `CleanBuilder`, `IncrementalBuilder`, `CacheBuilder`, and `AtomicBuilder`
- `EdgeTrigger`
- `DataExpr` and `DepSet`
- `cpp`, `java`, `springboot`, and `ts`
- `cli`

`Graph.data` is the normal way to create file, glob, regex, tool, and value
expressions. The concrete data classes and `DataFactory` remain in `brix.data`
for Brix's implementation and focused testing; they are not separate authoring
entry points.

`cli.main(create_graph, ...)` is the command-line entry point a build file ends
with; its `aliases=` and `commands=` add target shorthands and build-then-act
subcommands. An alias may be a bare path (or list of paths) or a
`cli.Alias(target, description)` when a one-line explanation is worth surfacing
in `brix.py help`, which prints the live builder stack, aliases, and commands.

## Results and reporting

- `BuildResult` and `ProcessResult`
- `Reporter`, `ConsoleReporter`, and `ErrorsAtEndReporter`
- `ActionStatus`, `StatusReport`, and `compute_status`

## Supported extension seams

- `BuildLayer`, `NodeContext`, and `NodeProposal`
- `CacheStoreProtocol`, `CacheStore`, `HttpCacheStore`, and `TwoTierCacheStore`
- `WorkerSpec`
- `identity`

These are public because projects may implement reporters, cache stores,
content identities, workers, or build layers without changing the engine.

## Engine internals

Execution plans and nodes, node state, concrete incremental/cache/atomic
layers, metadata storage, delete actions, and built-in lens implementations are
not top-level API. They remain in modules such as `brix.plan`, `brix.metadata`,
and `brix.incremental` for Brix's own implementation and tests.

There is no deprecation bridge for the former prototype exports. Brix has no
external consumers yet, so the v1 boundary is being established directly.
