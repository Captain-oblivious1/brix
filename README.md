# Brix

A build-system toolkit: define build graphs in ordinary Python while the
engine handles planning, dependency inference, and parallel execution.

The full design is in [freshStart.md](freshStart.md). Python is the
user-facing build language; durable and performance-sensitive internals move
to Rust once the API stabilizes, so the same core can later back a
JavaScript/TypeScript API.

## Status

Minimal prototype (steps 1–10 of the implementation order):

- logical data expressions: `file`, `glob`, `regex`, `tool`, `value`, unions
- `Graph.add(...)` with generic `ExternalProcessAction`
- command-line placeholder rendering
- dependency inference from data-set intersections (conservative)
- graph-shaped `ExecutionPlan` with a parallel scheduler
- `ExpandAll` as an unrealized plan node, expanded at runtime — including
  over collections produced by generator actions during the same build
- captured stdout/stderr/exit status as structured `ProcessResult`s
- `CleanBuilder.plan()` / `.build()`

Not yet implemented: typed language actions, incremental builds, caching,
atomic staging, status reporting.

## Layout

The DSL layer (`data.py`, `actions.py`, `graph.py`) is the user-facing
declaration API and stays in each host language. The engine layer
(`planner.py`, `plan.py`, `render.py`, `executor.py`, `process.py`) consumes
the logical graph and is the part scheduled to move to Rust; keep the
boundary between the two clean.

## Quickstart

```sh
python3 -m pytest                        # run the test suite
python3 examples/hello_text/brix.py      # portable text-processing demo
python3 examples/raw_cpp/brix.py         # compile+link demo (needs c++)
```

A build file looks like:

```python
from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph

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
            outputs={"object": data.file("build/obj/{name}.o")},
        ),
    ),
    ExternalProcessAction(
        name="link app",
        command_line="{compiler} {objects} -o {app}",
        inputs={
            "compiler": data.tool("c++"),
            "objects": data.glob("build/obj/{name}.o"),
        },
        outputs={"app": data.file("build/bin/app")},
    ),
)

CleanBuilder(graph).build()
```
