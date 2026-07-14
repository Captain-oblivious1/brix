# Brix

Brix is a build-system toolkit for defining build graphs in ordinary Python.
You describe the files and data each step uses; Brix plans the work, infers
dependencies, runs independent steps in parallel, and avoids work whose inputs
have not meaningfully changed.

The Python API is the build language. A shared Rust engine handles planning,
dependency inference, scheduling, incremental rebuilds, caching, atomic
staging, persistent workers, and content-aware comparisons. That engine is the
default today and is designed to support a future JavaScript/TypeScript API as
well.

## Why Brix

Brix aims to combine a small, readable authoring experience with the safety and
speed expected from a serious build system:

- Build steps declare logical inputs and outputs instead of hand-written order.
- Dependencies are inferred from overlapping data sets.
- Java ABI and TypeScript declaration lenses avoid downstream rebuilds when a
  public interface did not change.
- Incremental metadata handles changed and deleted sources.
- A shared result cache can restore builds across workspaces.
- Atomic staging prevents failed builds from partly changing the workspace.
- `status` explains what would run and why.
- `Graph.force_order()` covers the exceptional case where two steps need an
  ordering relationship without sharing a file.
- Planning rejects two steps that may write the same output.

## A small example

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
            command_line="{compiler} -c {source} -o {object}",
            inputs={
                "compiler": data.tool("c++"),
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

See [Getting started](docs/getting-started.md) for runnable examples and the
usual development commands.

## Current state

The Python authoring API is feature-complete for the currently supported use
cases, and the Rust core is the default engine. The two implementations are
cross-checked with shared golden fixtures, while real-project examples exercise
Java, C, C++, TypeScript, Spring Boot, dependency resolution, warm workers, and
the build layers.

The remaining work is mainly Rust-engine soak and cleanup, more real-project
dogfooding, usability improvements, and the future TypeScript authoring API.
See the [roadmap](docs/roadmap.md) for the full prioritized list.

## Documentation

- [Getting started](docs/getting-started.md) — run the tests and examples.
- [Specification](docs/specification.md) — behavior, graph model, actions,
  execution plans, builders, and design rules.
- [Python API](docs/python-api.md) — supported top-level authoring and extension
  interfaces.
- [Packaging and releases](docs/packaging.md) — wheels, editable installs, and
  the isolated release smoke test.
- [Roadmap](docs/roadmap.md) — every unfinished, blocked, parked, or undecided
  item from the former README TODO section.
- [Development log](docs/development-log.md) — the detailed completed history.
- [Build-layer diagrams](docs/build-layers.html) — incremental, cache, and
  atomic-layer design and data flow.
- [Class diagrams](docs/class-diagrams.md) — the Python data model and engine
  classes.
- [Boundary schema](docs/boundary-schema.md) — the language-neutral contract
  between an authoring API and the Rust core.

## Repository layout

- `src/brix/` — Python authoring API and reference implementation.
- `rust/brix-core/` — shared Rust engine.
- `rust/brix-py/` — Python/Rust boundary.
- `rust/brix-workerd/` — persistent worker holder.
- `tests/` — Python behavior and cross-engine tests.
- `examples/` — teaching projects and real-project dogfoods.
- `docs/` — design, specification, roadmap, and project history.

## Development

```sh
python3 -m pytest
cd rust && cargo test --workspace
```

The package metadata is in `pyproject.toml`; Brix requires Python 3.11 or
newer. The project is licensed under the MIT license.
