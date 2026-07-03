# Brix Fresh Start

This document is intended for a completely fresh chat or empty directory. It
does not assume access to prior Brix code, prototypes, diagrams, examples, or
README files.

It describes the current intended architecture, public API direction, execution
model, and implementation order for Brix.

## What Brix Is

Brix is a build-system toolkit.

The intended split is:

- **Python** is the user-facing build language.
- **Rust** eventually owns durable and performance-sensitive internals.
- A JavaScript or TypeScript API may come later, but the first API is Python.

Brix should let users define build graphs using ordinary Python code while the
engine handles:

- content-hash-based incremental builds
- generated files
- stale output cleanup
- process result persistence
- cache lookup and restore
- stdout/stderr replay
- atomic/staged output commits
- graph visualization
- status reporting

The main correctness requirement is:

> An incremental build must produce the same final output state as a clean build
> would produce.

That means Brix must delete outputs that no longer belong, delete outputs of
failed actions, and rebuild or remove downstream outputs consistently.

## High-Level Architecture

Brix has three conceptual layers:

1. **User declarations**
   - Python objects describing logical data and actions.
   - Clean-build truth only.
   - No cache, atomic, or incremental cleanup details.

2. **Planning**
   - Converts logical declarations into an execution plan.
   - Applies build capabilities such as incremental, caching, and atomic
     staging.
   - Keeps generated collections and repeated work partially unrealized until
     runtime when necessary.

3. **Execution**
   - Schedules concrete work.
   - Dynamically realizes generated file collections.
   - Expands repeated work into concrete parallel actions.
   - Captures process results.
   - Updates metadata.

The user graph should be independent of the selected build policy. The same
dependency graph should work with:

- clean build
- incremental build
- cached build
- atomic build
- cached atomic incremental build

## Expected User File Shape

A project usually has a `brix.py`.

That file conceptually contains three independent sections:

1. **Capabilities**
   - Which builder/layers are used.
   - Example: clean, incremental, cache, atomic.

2. **Dependency graph**
   - What data exists.
   - What actions consume and produce.
   - This is clean-build truth.

3. **CLI**
   - Parses project command-line arguments.
   - Maps aliases to targets.
   - Invokes the selected builder.

These sections do not need to appear in that order, but they are separate
responsibilities.

## Core User API Sketch

This is the kind of Python API Brix is aiming for.

```python
#!/usr/bin/env python3

from brix import (
    AtomicBuilder,
    CacheBuilder,
    CleanBuilder,
    ExternalProcessAction,
    ExpandAll,
    Graph,
    IncrementalBuilder,
    TimeAndHashChangeDetector,
)
from brix import cpp


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.cpp"),
            child=ExternalProcessAction(
                name="compile {name}",
                command_line="{compiler} {compile_options} -c {source} -o {object}",
                inputs={
                    "compiler": data.tool("c++"),
                    "compile_options": data.value("-O2 -std=c++20"),
                    "source": data.file("src/{name}.cpp"),
                },
                outputs={
                    "object": data.file("build/obj/{name}.o"),
                },
            ),
        ),
        ExternalProcessAction(
            name="link app",
            command_line="{linker} {objects} -o {app}",
            inputs={
                "linker": data.tool("c++"),
                "objects": data.glob("build/obj/{name}.o"),
            },
            outputs={
                "app": data.file("build/bin/app"),
            },
        ),
    )

    return graph


def create_builder():
    change_detector = TimeAndHashChangeDetector(root=".")
    change_set = change_detector.detect_changes()

    return AtomicBuilder(
        CacheBuilder(
            IncrementalBuilder(
                change_set=change_set,
                graph_supplier=create_graph,
            ),
            cache_dir="build/cache",
        ),
        staging_dir="build/staging",
    )


if __name__ == "__main__":
    create_builder().build()
```

For a simple project that does not care about incremental builds:

```python
if __name__ == "__main__":
    CleanBuilder(create_graph()).build()
```

## Logical Graph Model

The user declares a **logical graph**.

Logical graph nodes are:

- **Data**
  - files
  - file collections
  - tools
  - literal values
  - future in-memory values
  - future database-backed sets

- **Actions**
  - generic external processes
  - typed language actions
  - repeated-work expansion points

A common chain looks like:

```text
data -> action -> data -> action -> data
```

But a graph does not have to start with data. It may start with an action if
that action has no upstream project data input.

The graph is not a direct parallel build script. It declares what actions
consume and produce. The engine infers dependency relationships from data-set
intersections.

## Clean-Build Truth

The logical graph describes what a clean build should produce.

It should not contain explicit user-authored nodes for:

- removing stale outputs
- deleting failed outputs
- cache lookup
- cache restore
- diagnostics replay
- staging writes
- atomic commit
- crash recovery

Those are build capabilities applied during planning/execution.

## Data Expressions

Brix should expose a small set of logical data constructors.

```python
data.file("src/main.cpp")
data.glob("src/{name}.cpp")
data.regex(r"src/(?P<name>[A-Za-z0-9_]+)\.cpp")
data.tool("c++")
data.value("-O2 -std=c++20")
```

### `file`

Represents one path.

```python
data.file("src/foo.cpp")
```

This does not necessarily mean the file exists when `brix.py` starts. It is a
logical path.

### `glob`

Represents a path set with optional named bindings.

```python
data.glob("src/{name}.cpp")
```

If the workspace contains:

```text
src/foo.cpp
src/bar.cpp
```

Then the collection can realize to:

```text
name=foo -> src/foo.cpp
name=bar -> src/bar.cpp
```

### `regex`

Represents a path set for patterns glob cannot express.

```python
data.regex(r"src/(?P<name>[0-9]+)\.cpp")
```

Regex should be available, but the engine should not rely on brute-force regex
matching for all dependency inference.

### `tool`

Represents a tool identity.

```python
data.tool("gcc")
data.tool("/usr/bin/g++")
```

Eventually this should include resolved path, version, and other identity data
that affects outputs.

### `value`

Represents literal data that affects an output.

```python
data.value("-Iinclude -DNDEBUG")
```

Options, command-line fragments, environment values, and other build settings
should be hashed as inputs. Users should not need to create JSON option files
just to make settings hashable.

### Data Unions

Data expressions should be composable.

```python
sources = data.glob("src/{name}.cpp") + data.glob("build/generated/{name}.cpp")
```

The exact operator can change, but the API needs a way to represent unions of
data sets.

## Internal Path Pattern Representation

User-facing declarations may use file, glob, or regex syntax. Internally they
should lower into one common path-pattern representation.

Suggested internal structure:

```text
PathPattern
  original_text
  kind: file | glob | regex
  normalized_segments
  literal_prefix
  suffix_hints
  binding_names
  compiled_matcher
```

The engine should compile/intern path patterns once. It should not repeatedly
parse strings or run every regex against every other regex.

For performance, maintain indexes such as:

- exact path map
- literal-prefix trie
- path-segment trie
- suffix or extension buckets
- compiled matcher cache

Pattern-vs-pattern dependency inference can be conservative:

- If patterns can be proven disjoint cheaply, no dependency.
- If overlap cannot be proven or disproven cheaply, keep a potential dependency.
- Resolve potential dependencies when one side realizes to concrete paths.

## Actions

Actions consume named data expressions and produce named data expressions.

### ExternalProcessAction

The generic action is `ExternalProcessAction`.

```python
ExternalProcessAction(
    name="compile {name}",
    command_line="{compiler} {compile_options} -c {source} -o {object}",
    inputs={
        "compiler": data.tool("c++"),
        "compile_options": data.value("-O2"),
        "source": data.file("src/{name}.cpp"),
    },
    outputs={
        "object": data.file("build/obj/{name}.o"),
    },
)
```

`command_line` is a string template. Placeholders refer to named inputs and
outputs. When an action is realized, placeholders are replaced with concrete
values.

This action should eventually capture:

- stdout
- stderr
- exit code
- signal/termination status
- declared artifact outputs
- diagnostics replay data

### Typed Language Actions

Typed language actions should be strongly typed and ergonomic. They should not
make users spell magic string keys.

Example C++ declarations:

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
    link_options=[],
)
```

Example Java declaration:

```python
java.JarAction(
    source_files=data.glob("src/main/java/{class_name}.java"),
    output_file=data.file("build/libs/app.jar"),
    javac=data.tool("javac"),
    jar_tool=data.tool("jar"),
    options=[],
)
```

Under the hood, typed language actions lower into the same generic logical
action/data representation used by `ExternalProcessAction`.

There should not be separate incompatible build systems for generic rules,
C++, Java, Python, etc. They must compose.

## ExpandAll

`ExpandAll` represents repeated work over a data collection.

Example:

```python
ExpandAll(
    element_name="name",
    collection=data.glob("src/{name}.cpp"),
    child=ExternalProcessAction(
        name="compile {name}",
        command_line="{compiler} -c {source} -o {object}",
        inputs={
            "compiler": data.tool("c++"),
            "source": data.file("src/{name}.cpp"),
        },
        outputs={
            "object": data.file("build/obj/{name}.o"),
        },
    ),
)
```

Important semantics:

> `ExpandAll` is not an opaque loop that runs internally.

It is an unrealized expansion point in the execution plan. Once its collection
membership is known, it expands into concrete sibling action nodes.

For example:

```text
src/foo.cpp
src/bar.cpp
src/main.cpp
```

realizes into:

```text
compile foo:  src/foo.cpp  -> build/obj/foo.o
compile bar:  src/bar.cpp  -> build/obj/bar.o
compile main: src/main.cpp -> build/obj/main.o
```

Those concrete actions are normal schedulable nodes and may run in parallel.

This is essential for generated files, because the files may not exist when
`brix.py` starts.

## Generated Files

Generated file support should not require the user to declare a special
"graph-expanding node."

The user should be able to write a clean logical graph such as:

```python
graph.add(
    ExternalProcessAction(
        name="generate sources",
        command_line="{python} {generator} {csv} {out_dir}",
        inputs={
            "python": data.tool("python"),
            "generator": data.file("tools/generate.py"),
            "csv": data.file("src/schema.csv"),
            "out_dir": data.value("build/generated"),
        },
        outputs={
            "generated": data.glob("build/generated/{name}.cpp"),
        },
    ),
    ExpandAll(
        element_name="name",
        collection=data.glob("build/generated/{name}.cpp"),
        child=ExternalProcessAction(
            name="compile generated {name}",
            command_line="{compiler} -c {source} -o {object}",
            inputs={
                "compiler": data.tool("c++"),
                "source": data.file("build/generated/{name}.cpp"),
            },
            outputs={
                "object": data.file("build/obj/generated/{name}.o"),
            },
        ),
    ),
)
```

Initial execution plan:

```text
generate sources
unrealized collection: build/generated/{name}.cpp
unrealized ExpandAll: compile generated files
downstream link blocked on generated objects
```

After generation:

```text
build/generated/foo.cpp
build/generated/bar.cpp
```

Then `ExpandAll` expands into:

```text
compile generated foo
compile generated bar
```

Downstream actions wait until required concrete members are known and produced.

## Builders And Capability Layers

Builders choose how the graph is built.

### CleanBuilder

```python
CleanBuilder(graph).build()
```

Clean build means no change detection is used. It may rebuild everything. It
can still be wrapped by caching or atomic staging.

### IncrementalBuilder

```python
change_detector = TimeAndHashChangeDetector(root=".")
change_set = change_detector.detect_changes()

IncrementalBuilder(
    change_set=change_set,
    graph_supplier=create_graph,
).build()
```

`graph_supplier` is a function so the builder can decide when and how much graph
to load. For small projects, it may load the full graph. For large projects, a
custom loader may use the `ChangeSet` to load only relevant build modules.

### CacheBuilder

```python
CacheBuilder(
    IncrementalBuilder(change_set, create_graph),
    cache_dir="build/cache",
).build()
```

Caching should be transparent except for speed.

If a cached result is a failure, Brix should replay the same stdout/stderr and
fail the build the same way. Cached failures are useful and should be
representable.

### AtomicBuilder

```python
AtomicBuilder(
    CacheBuilder(
        IncrementalBuilder(change_set, create_graph),
    ),
    staging_dir="build/staging",
).build()
```

Atomic/staged build behavior should be independent of caching.

Conceptual staging:

```text
build/staging/add/...  files to add or replace
build/staging/del/...  deletion markers
```

If all work succeeds, Brix commits staged additions/deletions to real output
locations. If the build fails or crashes, real outputs remain unchanged and
status/recovery can inspect the staged state.

### Preferred Layer Order

For all capabilities:

```text
AtomicBuilder(
    CacheBuilder(
        IncrementalBuilder(...)
    )
)
```

Atomic should be outermost so both process outputs and cache-restored outputs
go through staging.

## Builder Interface

Builders should conceptually expose:

```python
builder.plan() -> ExecutionPlan
builder.build() -> BuildResult
```

Root builders such as `CleanBuilder` and `IncrementalBuilder` convert logical
graphs into execution plans.

Wrapper layers such as cache and atomic transform plans:

```text
ExecutionPlan -> ExecutionPlan
```

## ExecutionPlan

The execution plan is the execution graph for one build invocation.

It may be partially realized.

Node categories:

- **realized data**
  - concrete path such as `src/foo.cpp`

- **unrealized data**
  - collection such as `build/generated/{name}.cpp`

- **realized action**
  - concrete process such as `compile foo`

- **unrealized action**
  - expansion point such as `ExpandAll`

The plan should eventually be graph-shaped, not just a list.

Implementation direction:

```text
ExecutionPlan
  nodes: Vec<Node>
  edges: Vec<Edge>

Node
  id
  kind
  origin_logical_id
  inputs
  outputs
  state

Edge
  from
  to
  reason
```

Use IDs and references to avoid copying the entire logical graph into every
planning layer.

## Dynamic Realization

Execution can reveal more graph structure.

Example:

```text
generate sources -> build/generated/{name}.cpp
ExpandAll compile generated
link app
```

The scheduler initially sees unrealized nodes. After `generate sources`
finishes, the generated collection realizes to concrete files. Then the
`ExpandAll` node expands into concrete compile nodes. Then the link node waits
for those concrete object files.

The scheduler should own readiness. Nodes should execute and report results.
Planning layers and realization logic respond to events.

Avoid making nodes mutate arbitrary external graph structure themselves.

## Incremental Build Semantics

Incremental correctness requires comparing current state to previous metadata.

Persistent metadata must include:

- file content hashes
- cheap scan data such as mtime/size
- action fingerprints
- command/tool/options/env fingerprints
- discovered dependencies
- declared outputs
- extra owned outputs
- generated collection membership
- expansion membership
- previous process results

### Changed File

If `src/foo.cpp` changes:

```text
src/foo.cpp -> compile foo -> build/obj/foo.o -> link -> build/bin/app
```

Then Brix should recompile `foo.o` and relink downstream outputs.

### Failed Compile

If `compile foo` fails:

- remove `build/obj/foo.o`
- mark downstream outputs invalid
- remove or fail downstream outputs consistently with clean-build behavior

Do not preserve a previous successful object file if the clean build would have
failed and not produced it.

### Deleted Source

If a source disappears from a wildcard/collection, Brix must remove the old
output.

Example:

Logical graph:

```text
src/{name}.cpp -> build/obj/{name}.o
```

Previous collection membership:

```text
foo
bar
baz
```

Current collection membership:

```text
foo
bar
```

Removed binding:

```text
baz
```

Therefore Brix must:

- delete `build/obj/baz.o`
- rebuild downstream collection consumers such as static libraries
- link downstream outputs using only remaining objects

This requires persistent membership history. File hashes alone are not enough.

## Clean Build Vs Incremental Build

Without incremental enabled, Brix may simply rebuild everything every time. For
simple projects, that is acceptable.

With incremental enabled, Brix needs change detection and metadata.

If the build directory and metadata are missing, `IncrementalBuilder` can safely
degrade to clean behavior.

If metadata is missing but outputs exist, the safe policy is probably to report
an error and require explicit clean/adopt behavior.

## Change Detection

Default detector:

```python
TimeAndHashChangeDetector(root=".")
```

Default behavior:

- workspace-relative paths
- timestamp/size as cheap scan
- content hash when needed
- ignore build metadata directories
- follow file symlinks as ordinary files
- reject path collisions that differ only by case

Future detectors:

- filesystem watcher
- FUSE-like live tracker
- source-control based detector
- remote metadata service

## Modular Build Files

Multiple build files are a graph-construction feature, not the same as
incremental builds.

Simple project:

```text
one brix.py -> full graph -> CleanBuilder or IncrementalBuilder
```

Large project:

```text
ChangeDetector -> ChangeSet
ChangeSet -> DistributedGraphLoader
DistributedGraphLoader -> affected logical graph
IncrementalBuilder -> execution plan
```

Ordinary Python imports should remain the simplest modular mechanism.

Automatic distributed loading can be added later.

## Process Results

Every process invocation should produce:

- declared artifact outputs
- stdout
- stderr
- exit code
- termination information

Those results should be persisted.

On cache hit, Brix should replay stdout/stderr as if the process ran.

On cached failure, Brix should replay the failure diagnostics and fail the build
the same way.

## Status

Status should eventually answer:

- what files are stale
- what actions are stale
- what outputs are missing
- what outputs are stale because source membership changed
- whether staged changes exist
- whether an interrupted atomic build needs recovery
- whether cached failures/results exist

Status is build-state inspection, not a project-specific dry-run mode.

## CLI Direction

Brix is primarily a library. Projects may expose `brix.py`.

The CLI should be separate from `Graph` and builders.

CLI responsibilities:

- parse commands such as `build`, `status`, `clean`, `graph`
- parse project-specific arguments if desired
- map aliases to targets
- call builder methods

Avoid putting CLI parsing directly in `Graph` or `Project`.

## Example Raw C++ Graph

This is the lower-level form.

```python
def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.cpp"),
            child=ExternalProcessAction(
                name="compile {name}",
                command_line="{compiler} {flags} -c {source} -o {object}",
                inputs={
                    "compiler": data.tool("c++"),
                    "flags": data.value("-O2 -std=c++20"),
                    "source": data.file("src/{name}.cpp"),
                },
                outputs={
                    "object": data.file("build/obj/{name}.o"),
                },
            ),
        ),
        ExternalProcessAction(
            name="link app",
            command_line="{compiler} {objects} -o {app}",
            inputs={
                "compiler": data.tool("c++"),
                "objects": data.glob("build/obj/{name}.o"),
            },
            outputs={
                "app": data.file("build/bin/app"),
            },
        ),
    )

    return graph
```

## Example Typed C++ Graph

This is the helper form. It should lower to the same underlying model.

```python
def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/obj/{name}.o"),
            compiler=data.tool("c++"),
            compile_options=["-O2", "-std=c++20"],
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/bin/app"),
            linker=data.tool("c++"),
        ),
    )

    return graph
```

The raw and typed forms must compose. For example, a C++ executable can generate
Java files, which then feed a Java JAR action.

## Example Generated C++ To Java Flow

```python
def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("tools/schema_generator/{name}.cpp"),
            object_files=data.glob("build/tools/schema_generator/{name}.o"),
            compiler=data.tool("c++"),
            compile_options=["-O2"],
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/tools/schema_generator/{name}.o"),
            output_file=data.file("build/tools/schema_generator/schema_generator"),
            linker=data.tool("c++"),
        ),
        ExternalProcessAction(
            name="generate java sources",
            command_line="{generator} --input {schema} --output-dir {out_dir}",
            inputs={
                "generator": data.file("build/tools/schema_generator/schema_generator"),
                "schema": data.file("src/schema.csv"),
                "out_dir": data.value("build/generated/java"),
            },
            outputs={
                "generated_sources": data.glob("build/generated/java/{class_name}.java"),
            },
        ),
        java.JarAction(
            source_files=(
                data.glob("src/main/java/{class_name}.java")
                + data.glob("build/generated/java/{class_name}.java")
            ),
            output_file=data.file("build/libs/app.jar"),
            javac=data.tool("javac"),
            jar_tool=data.tool("jar"),
        ),
    )

    return graph
```

This example is important because it proves Brix must not have separate
incompatible systems for C++, Java, and generic actions.

## Minimal Class List

A first prototype can start with these Python classes.

```text
Graph
DataFactory
DataExpr
DataAtom
DataUnion
ExternalProcessAction
ExpandAll
CleanBuilder
IncrementalBuilder
CacheBuilder
AtomicBuilder
ExecutionPlan
ExecutionNode
ExecutionEdge
ProcessResult
BuildResult
ChangeSet
TimeAndHashChangeDetector
```

Typed language modules:

```text
brix.cpp.CompileSourceAction
brix.cpp.LinkApplicationAction
brix.cpp.CompileApplicationAction
brix.java.JarAction
```

## Minimal Prototype Semantics

The first working implementation should support:

1. Create a `Graph`.
2. Add `ExternalProcessAction`.
3. Add `ExpandAll` over `data.glob("src/{name}.txt")`.
4. `CleanBuilder.plan()` returns an `ExecutionPlan`.
5. `ExecutionPlan.run()` executes concrete processes.
6. `ExpandAll` is initially unrealized.
7. At runtime, `ExpandAll` expands into concrete process nodes.
8. Process stdout/stderr/exit code are captured.
9. Failed process raises/returns a structured failure.

This proves the core model before adding real incremental behavior.

## Implementation Order

Recommended order from empty directory:

1. Implement data expression classes.
2. Implement `Graph.add(...)`.
3. Implement `ExternalProcessAction`.
4. Implement command-line placeholder rendering.
5. Implement `ProcessResult` and process execution.
6. Implement `ExecutionPlan` as a graph-shaped structure.
7. Implement `CleanBuilder.plan()` and `CleanBuilder.build()`.
8. Implement glob binding extraction for simple `{name}` patterns.
9. Implement `ExpandAll` as an unrealized execution-plan node.
10. Implement runtime expansion into concrete process nodes.
11. Add typed C++ actions that lower to generic actions.
12. Add dependency inference between actions through data-set intersections.
13. Add persistent metadata.
14. Add `TimeAndHashChangeDetector`.
15. Add previous/current collection membership comparison.
16. Add deleted-source cleanup.
17. Add failed-output cleanup.
18. Add process result persistence.
19. Add cache layer.
20. Add atomic/staging layer.
21. Move stable internals into Rust.

## Design Rules To Preserve

- The logical graph is clean-build truth.
- Incremental behavior must match clean-build final output.
- Generated files must be representable before they exist.
- `ExpandAll` expands into schedulable graph nodes, not an opaque internal loop.
- Typed language helpers and generic rules must share one underlying model.
- Build capabilities should layer around the graph, not pollute user
  declarations.
- Cache and atomicity are independent optional capabilities.
- Failed results are real results and should be cacheable/replayable.
- Deleted sources from wildcard collections must remove stale outputs.
- Python owns user composition.
- Rust eventually owns durable metadata, graph algorithms, scheduling, caching,
  and performance-critical execution.
