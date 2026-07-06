# Class diagrams

Every class in `src/brix/` (48 total), grouped into five readable diagrams
rather than one unreadable graph. Each diagram below is the Mermaid
`classDiagram` source followed by its rendered SVG; open the SVG directly in
a browser for the sharpest view (GitHub also renders the Mermaid blocks
inline).

Notes on reading these:
- Dashed arrows (`..>`) are dependencies ("uses" / "creates" / "returns"),
  not ownership.
- Filled-diamond arrows (`*--`) are composition (owns, same lifetime);
  open-diamond arrows (`o--`) are aggregation (holds a reference/collection).
- A class shown with no members and a yellow note ("full definition in the
  ___ diagram") is a cross-reference into another diagram — it exists here
  only to show a relationship, not to duplicate its definition.
- Union-ish annotations like `int|None` and generics like `list~str~` are
  Python type hints transliterated into Mermaid's member syntax, not UML
  formalism.

## 1. Data model (`data.py`)

The logical data expressions: `data.file(...)`, `data.glob(...)`, etc. Clean
build truth — no caching or staging concerns here.

```mermaid
classDiagram
    direction TB

    class DataExpr {
        +atoms() Iterator~DataAtom~
        +__add__(other) DataUnion
    }

    class DataAtom {
        +atoms() Iterator~DataAtom~
        +substitute(bindings) DataAtom
    }

    class FileData {
        +path str
        +substitute(bindings) FileData
    }

    class GlobData {
        +substitute(bindings) GlobData
    }

    class RegexData {
        +binding_names tuple~str~
        +match(path) dict|None
        +substitute(bindings) RegexData
    }

    class ToolData {
        +name str
    }

    class ValueData {
        +text str
        +substitute(bindings) ValueData
    }

    class DataUnion {
        +parts tuple~DataExpr~
        +atoms() Iterator~DataAtom~
    }

    class DataFactory {
        +file(path) FileData
        +glob(pattern) GlobData
        +regex(pattern) RegexData
        +tool(name) ToolData
        +value(text) ValueData
    }

    class PathPattern

    note for PathPattern "full definition in the Support diagram"

    DataAtom --|> DataExpr
    DataUnion --|> DataExpr
    FileData --|> DataAtom
    GlobData --|> DataAtom
    RegexData --|> DataAtom
    ToolData --|> DataAtom
    ValueData --|> DataAtom

    DataUnion o-- "*" DataExpr : parts
    FileData *-- PathPattern : pattern
    GlobData *-- PathPattern : pattern

    DataFactory ..> FileData : creates
    DataFactory ..> GlobData : creates
    DataFactory ..> RegexData : creates
    DataFactory ..> ToolData : creates
    DataFactory ..> ValueData : creates
```

![Data model](class-diagrams/data-model.svg)

## 2. Actions (`actions.py`, `java.py`, `cpp.py`)

What the graph declares: generic process actions plus the typed C++/Java
helpers that lower into them.

```mermaid
classDiagram
    direction TB

    class Action

    class Expandable {
        +label str
        +substitute(bindings) Expandable
    }

    class ExternalProcessAction {
        +name str|None
        +command_line str
        +inputs dict~str, DataExpr~
        +outputs dict~str, DataExpr~
        +env dict~str, DataExpr~
        +label str
        +substitute(bindings) ExternalProcessAction
    }

    class DeleteFilesAction {
        +paths list~str~
        +label str
    }

    class ExpandAll {
        +element_name str
        +collection DataExpr
        +child Expandable
        +label str
        +substitute(bindings) Expandable
    }

    class JarAction {
        +source_files DataExpr
        +output_file FileData
        +javac ToolData
        +jar_tool ToolData
        +classes_dir str
        +entry_point str|None
        +lower() list~Action~
    }

    class CompileSourceAction {
        +source_files GlobData
        +object_files GlobData
        +compiler ToolData
        +compile_options
        +lower() list~Action~
    }

    class LinkApplicationAction {
        +object_files DataExpr
        +output_file FileData
        +linker ToolData
        +link_options
        +lower() list~Action~
    }

    class DataExpr
    note for DataExpr "full definition in the Data model diagram"

    Expandable --|> Action
    DeleteFilesAction --|> Action
    JarAction --|> Action
    CompileSourceAction --|> Action
    LinkApplicationAction --|> Action
    ExternalProcessAction --|> Expandable
    ExpandAll --|> Expandable

    ExpandAll *-- Expandable : child
    ExpandAll --> DataExpr : collection
    ExternalProcessAction --> DataExpr : inputs/outputs/env

    JarAction ..> ExternalProcessAction : lower() -> [javac, jar]
    CompileSourceAction ..> ExpandAll : lower() -> expand over sources
    CompileSourceAction ..> ExternalProcessAction : (per-file compile)
    LinkApplicationAction ..> ExternalProcessAction : lower() -> link
```

![Actions](class-diagrams/actions.svg)

## 3. Execution plan (`plan.py`)

The graph-shaped structure of one build: append-only nodes/edges plus the
scheduler-owned state table.

```mermaid
classDiagram
    direction TB

    class NodeKind {
        <<enumeration>>
        PROCESS
        EXPAND
    }

    class NodeState {
        <<enumeration>>
        PENDING
        RUNNING
        DONE
        FAILED
        SKIPPED
    }

    class EdgeTrigger {
        <<enumeration>>
        ON_SUCCESS
        ON_COMPLETION
    }

    class ExecutionNode {
        +id int
        +kind NodeKind
        +label str
        +action Action
    }

    class ExecutionEdge {
        +from_id int
        +to_id int
        +reason str
        +trigger EdgeTrigger
    }

    class NodeStateTable {
        +status(node_id) NodeState
        +result(node_id) ProcessResult|None
        +advance(node_id, status, result)
    }

    class ExecutionPlan {
        +root Path
        +nodes list~ExecutionNode~
        +edges list~ExecutionEdge~
        +add_node(kind, label, action) ExecutionNode
        +add_edge(from_id, to_id, reason, trigger)
        +incoming(node_id) list~ExecutionEdge~
        +predecessors(node_id) list~ExecutionNode~
        +successors(node_id) list~ExecutionNode~
        +run(jobs, layers, reporter) BuildResult
        +describe() str
    }

    class Action
    note for Action "full definition in the Actions diagram"
    class BuildLayer
    note for BuildLayer "full definition in the Builders & layers diagram"
    class Reporter
    note for Reporter "full definition in the Support diagram"
    class ProcessResult
    note for ProcessResult "full definition in the Support diagram"
    class BuildResult
    note for BuildResult "full definition in the Builders & layers diagram"

    ExecutionNode --> NodeKind : kind
    ExecutionNode --> Action : action
    ExecutionEdge --> EdgeTrigger : trigger
    NodeStateTable --> NodeState : tracks
    NodeStateTable --> ProcessResult : result
    ExecutionPlan o-- "*" ExecutionNode : nodes
    ExecutionPlan o-- "*" ExecutionEdge : edges
    ExecutionPlan ..> NodeStateTable : run() creates
    ExecutionPlan ..> BuildLayer : run(layers)
    ExecutionPlan ..> Reporter : run(reporter)
    ExecutionPlan ..> BuildResult : run() returns
```

![Execution plan](class-diagrams/execution-plan.svg)

## 4. Builders & layers (`builders.py`, `layers.py`, `incremental.py`, `cache.py`, `atomic.py`)

The capability stack: `CleanBuilder`/`IncrementalBuilder` are root builders;
`CacheBuilder` and `AtomicBuilder` wrap another builder and each contribute
one `BuildLayer` to the middleware stack (recommended nesting:
`AtomicBuilder(CacheBuilder(IncrementalBuilder(...)))`).

```mermaid
classDiagram
    direction TB

    class BuildResult {
        +success bool
        +plan ExecutionPlan
        +states NodeStateTable
        +process_results list~ProcessResult~
        +errors list~str~
        +summary() str
    }

    class CleanBuilder {
        +graph Graph
        +root Path
        +plan(targets) ExecutionPlan
        +layers() list
        +build(jobs, reporter, targets) BuildResult
    }

    class IncrementalBuilder {
        +root Path
        +metadata_dir Path
        +plan(targets) ExecutionPlan
        +layers() list~BuildLayer~
        +build(jobs, reporter, targets) BuildResult
    }

    class CacheBuilder {
        +inner
        +cache_dir Path
        +root Path
        +plan(targets) ExecutionPlan
        +layers() list~BuildLayer~
        +build(jobs, reporter, targets) BuildResult
    }

    class AtomicBuilder {
        +inner
        +staging_dir Path
        +root Path
        +plan(targets) ExecutionPlan
        +layers(hold) list~BuildLayer~
        +build(jobs, reporter, targets, hold) BuildResult
        +commit() tuple~int,int~|None
        +discard() int|None
    }

    class NodeProposal {
        +action Action
        +label str|None
    }

    class NodeContext {
        +workspace_root Path
        +output_root Path
        +deletions_root Path|None
        +for_root(root)$ NodeContext
        +read_roots() tuple~Path~
        +read_path(logical) Path
        +find(pattern) list
        +iter_files() list
    }

    class BuildLayer {
        +on_build_begin(plan, ctx)
        +around_node(node, ctx, inner) ProcessResult
        +on_collection_realized(node, members, ctx) list~NodeProposal~|None
        +on_build_end(result, ctx)
    }

    class IncrementalLayer {
        +metadata BuildMetadata
        +detector TimeAndHashChangeDetector
        +around_node(node, ctx, inner) ProcessResult
        +on_collection_realized(node, members, ctx)
        +on_build_end(result, ctx)
    }

    class CacheStore {
        +directory Path
        +load(fingerprint) dict|None
        +restore(fingerprint, entry, ctx) bool
        +store(fingerprint, result, outputs)
    }

    class CacheLayer {
        +store CacheStore
        +detector TimeAndHashChangeDetector
        +around_node(node, ctx, inner) ProcessResult
    }

    class AtomicLayer {
        +staging_dir Path
        +hold bool
        +staging Path|None
        +on_build_begin(plan, ctx)
        +around_node(node, ctx, inner) ProcessResult
        +on_build_end(result, ctx)
    }

    class BuildMetadata
    note for BuildMetadata "full definition in the Support diagram"
    class TimeAndHashChangeDetector
    note for TimeAndHashChangeDetector "full definition in the Support diagram"
    class Graph
    note for Graph "full definition in the Support diagram"
    class ExecutionPlan
    note for ExecutionPlan "full definition in the Execution plan diagram"
    class NodeStateTable
    note for NodeStateTable "full definition in the Execution plan diagram"

    IncrementalLayer --|> BuildLayer
    CacheLayer --|> BuildLayer
    AtomicLayer --|> BuildLayer

    CleanBuilder o-- Graph : graph
    CleanBuilder ..> ExecutionPlan : plan() builds

    CacheBuilder ..> IncrementalBuilder : inner (recommended stack)
    AtomicBuilder ..> CacheBuilder : inner (recommended stack)

    note for CacheBuilder "`inner` is untyped/duck-typed: any object with<br/>plan()/layers()/build()/root — not a real type<br/>dependency on a specific builder class. Dashed<br/>arrows here show the recommended stack<br/>AtomicBuilder(CacheBuilder(IncrementalBuilder(...))),<br/>not a compile-time constraint."

    CacheBuilder ..> CacheLayer : layers() prepends
    CacheBuilder ..> CacheStore : creates
    AtomicBuilder ..> AtomicLayer : layers() appends
    IncrementalBuilder ..> IncrementalLayer : layers() creates
    IncrementalBuilder ..> BuildMetadata : creates
    IncrementalBuilder ..> TimeAndHashChangeDetector : creates

    IncrementalLayer --> BuildMetadata : metadata
    IncrementalLayer --> TimeAndHashChangeDetector : detector
    IncrementalLayer ..> NodeProposal : proposes deletions
    CacheLayer --> TimeAndHashChangeDetector : detector

    BuildLayer ..> NodeContext : around_node(ctx)
    BuildLayer ..> NodeProposal : on_collection_realized returns

    BuildResult --> NodeStateTable : states
```

![Builders and layers](class-diagrams/builders-and-layers.svg)

## 5. Support (`metadata.py`, `status.py`, `report.py`, `process.py`, `graph.py`, `patterns.py`, `render.py`)

Persistence, status inspection, reporting, process results, the top-level
`Graph`, and path-pattern matching.

```mermaid
classDiagram
    direction TB

    class BuildMetadata {
        +root Path
        +directory Path
        +path Path
        +load()
        +save()
        +cached_hash(path_key, signature) str|None
        +store_hash(path_key, signature, digest)
        +action_record(key) dict|None
        +record_action(key, record)
        +forget_action(key)
        +action_records() dict
        +membership(key) list~str~|None
        +set_membership(key, members)
    }

    class TimeAndHashChangeDetector {
        +root Path
        +metadata BuildMetadata
        +stat_signature(path) list~int~|None
        +hash_file(path) str|None
    }

    class ActionStatus {
        +label str
        +state str
        +detail str
        +last_result ProcessResult|None
        +is_fresh bool
    }

    class PendingDeletion {
        +expansion str
        +member str
        +paths list~str~
    }

    class StatusReport {
        +actions list~ActionStatus~
        +pending_deletions list~PendingDeletion~
        +staged_files list~str~
        +staged_deletions list~str~
        +interrupted_commit bool
        +held bool
        +has_staged_state bool
        +is_clean bool
        +failures list~ActionStatus~
        +render() str
    }

    class Reporter {
        +build_begin(plan)
        +node_started(node)
        +process_started(node)
        +node_completed(node, status, result, error)
        +node_skipped(node, why)
        +collection_realized(node, members, children)
        +node_inserted(node, reason)
        +build_end(result)
    }

    class ConsoleReporter {
        +stream TextIO
        +show_replays bool
    }

    class ErrorsAtEndReporter {
        +stream TextIO
        +build_end(result)
    }

    class ProcessResult {
        +action_name str
        +command str
        +argv list~str~
        +exit_code int|None
        +signal int|None
        +stdout str
        +stderr str
        +source str
        +ok bool
        +summary() str
    }

    class Graph {
        +data DataFactory
        +actions list~Action~
        +add(actions) Graph
    }

    class PatternError {
        <<Exception>>
    }

    class PathPattern {
        +template str
        +binding_names tuple~str~
        +is_concrete bool
        +match(path) dict|None
        +substitute(bindings) str
        +literal_prefix_dir str
        +find(root) list
    }

    class RenderError {
        <<Exception>>
    }

    class Action
    note for Action "full definition in the Actions diagram"
    class DataFactory
    note for DataFactory "full definition in the Data model diagram"

    ConsoleReporter --|> Reporter
    ErrorsAtEndReporter --|> Reporter

    TimeAndHashChangeDetector o-- BuildMetadata : metadata
    StatusReport o-- "*" ActionStatus : actions
    StatusReport o-- "*" PendingDeletion : pending_deletions
    ActionStatus --> ProcessResult : last_result
    Graph o-- "*" Action : actions
    Graph *-- DataFactory : data
```

![Support](class-diagrams/support.svg)

## Regenerating / verifying

```
cd docs && npx @mermaid-js/mermaid-cli -i class-diagrams/<name>.mmd -o class-diagrams/<name>.svg
```

**Verification gotcha found while making these**: the SVGs Mermaid produces
embed class-member text via `<foreignObject>` HTML, not native SVG `<text>`
elements. `rsvg-convert` silently drops all of that text (it doesn't support
`foreignObject`), so a `rsvg-convert`-rendered PNG looks like empty boxes —
that's a viewer limitation, not a broken diagram. To verify a diagram
actually contains its labels, render straight to PNG with `mmdc` itself
(`mmdc -i file.mmd -o file.png`), which uses its bundled Chromium and
renders the HTML correctly. Headless Vivaldi was also tried for
verification and hung/timed out on this machine (heavy vendor profile
init) — mmdc's own puppeteer-managed Chromium worked reliably instead.
