# Brix

Brix is a library for building your project from your own Python code — an API
small enough to read in a sitting, yet comprehensive enough to express a real,
multi-language build. You describe the files and data each step uses; Brix plans
the work, infers dependencies, runs independent steps in parallel, and avoids
work whose inputs have not meaningfully changed.

The Python API is the build language; the engine underneath gives you three
things a serious build needs:

- **Fast.** The engine is written in Rust and owns the planning, scheduling, and
  change detection, and it keeps compiler processes warm between builds — so even
  large builds, and every edit-compile loop, stay quick.
  // Mention that the below are optional and configurable
- **Atomic.** A build is all-or-nothing, like a `git` commit — it either applies  // Maybe also mention that this is inspired by git in that users can get status, stage, and commit builds
  in full or leaves your workspace exactly as it was, so a failure never leaves a
  half-built tree.
- **Incremental builds that match a clean build, exactly.** Rebuilding only what  // Make clear the *why*. That users no longer fall back to clean builds out of fear of an incorrect build
  changed is never a correctness compromise: an incremental build produces the
  same result a from-scratch build would — down to removing outputs whose source
  is gone.

That engine is the default today and is designed to back a future  // This makes it seem like the above features won't exist in Javascript/Typescript API
JavaScript/TypeScript API as well.

## In plain terms

A build tool turns your source code into the finished program. The slow way is
to rebuild everything from scratch every time you change one file. The fast way
is to redo only the parts your change affects — but most tools that try to be
fast sometimes get it wrong and hand you a stale or broken result.  // losing confidence and resulting in reliance of clean builds all the time

Brix is the fast way *without* the wrong. You write a short Python file that
says, for each step, which files go in and which come out. Brix reads that and  // Not sure what you mean by "which files go in and which come out"
works out the rest: the right order to do things, what can run at the same time,
and — the important part — exactly what needs redoing when you change something.
Change one file and only the steps that truly depend on it run again; change
nothing that matters and it does no work at all. Think of a recipe with many
steps: swap one ingredient and you only redo the steps that used it, not the
whole meal.

And because the build file is just a small, plain Python file, you don't even
have to write it yourself — an AI assistant can write it for you and check it by
running the build.

## Why Brix

Almost nobody wants to become an expert in their build system. People want it to
be easy to write, fast, and correct — and to otherwise stay out of the way.

Yet build tools make you pick the wrong trade-off. On one side, simple-but-unsafe: a
Makefile is quick to write but silently wrong — it misses a changed header, has
no real notion of what "changed" means, and happily ships a stale build. On the
other, correct-but-heavy: Bazel and Buck are correct, but they are a sealed world
with their own language, their own sandbox, and a rewrite-everything migration;
Gradle is powerful, but that power turns build files into imperative code that
rots.

Brix takes a different bet: **write your build in ordinary Python — declare what  // Mention that brix provides users the building blocks (thus the name brix.. as in bricks) to make a simple and/or powerful build system as they want.
each step reads and writes — and get the incremental correctness and safety of
the heavy tools without the DSL, the sandbox, or the migration cliff.** You  // What is DSL?0
author with Python's full power (a loop to stamp out fifty services, a function
to factor a shared pattern), but a step's *meaning* is a small, declarative
vocabulary: its inputs and its outputs — declared coarsely, as patterns, not
file-by-file. You name `src/*.c`; Brix discovers each file's actual `#include`
headers (and a TypeScript module's imports) for you, so you never hand-maintain a
dependency list. From there the engine infers the dependency edges, runs
independent work in parallel, skips anything whose meaning has not changed, and
refuses any graph it cannot build correctly. Expressiveness for assembling the
build; rails around what the build means.

This is not aspirational. The engine is cross-checked between a Python and a Rust
implementation that must produce byte-identical decisions; content-key *lenses*  // I know that you use "lenses" a lot, but is that a common term (that I'm unfamiliar with)?  Will users know what that means? Or are you defining a new meaning here?
compare a Java jar by its ABI and a TypeScript module by its declared types, so  // Things like ABI seems to be advanced. It seems like these techincal details (lenses, ABI, etc. maybe should be put in a details section.)
an implementation-only edit lets dependents replay instead of rebuild; atomic
staging means a failed build never half-updates your tree; and on JUnit 5's
platform modules the warm edit-compile loop runs 5–12× faster than Gradle. Brix
can adopt an existing project's Maven or Gradle classpath, so you migrate
incrementally rather than all at once.

And because those build files are small, declarative Python — the language coding
assistants know best — an assistant can *write and maintain them for you*, and
check its own work by running the build: `status` explains what would happen
before it does, and the planner rejects an ambiguous graph outright. It is not  // You mention "planner" but is that detail that the users needs? What if we just say "brix"?
raw configurability that makes this work but expressiveness with rails — a small,
checkable vocabulary over a language the model already knows. The examples in   // language model and most users already know
this repository were themselves authored this way.

## What you get

A small, readable authoring experience with the safety and speed expected from a
serious build system:

- Build steps declare logical inputs and outputs instead of hand-written order.
- Dependencies are inferred from overlapping data sets.
- Transitive dependencies are discovered automatically — a C file's `#include`
  headers, a TypeScript module's imports — so you declare intent coarsely, never
  a file-by-file dependency list.
- Java ABI and TypeScript declaration lenses avoid downstream rebuilds when a
  public interface did not change.
- Incremental metadata handles changed and deleted sources.
- On a large monorepo, Brix loads and builds only the slice a change affects,  // maybe word this that "in order to support large monorepos, brix enables brix.py files to be spread across the repository. And only the ones necessesssary are read and executed.
  not the whole repository.
- Persistent compiler workers stay warm between builds, so the edit-compile loop
  does not pay compiler startup each time.
- A shared result cache can restore builds across workspaces — a build CI or a
  teammate already ran restores instantly instead of rebuilding.
- Generate your project's own API docs (`javadoc`, `typedoc`, `doxygen`) as
  tracked, incremental build outputs, like anything else in the graph.
- Atomic staging prevents failed builds from partly changing the workspace.
- `status` explains what would run and why.
- `Graph.force_order()` covers the exceptional case where two steps need an
  ordering relationship without sharing a file.
- Planning rejects two steps that may write the same output.

## A small example

// Add comments to this code:

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
Java, Kotlin, C, C++, TypeScript, Spring Boot, dependency resolution, warm
workers, and the build layers.

The remaining work is mainly Rust-engine soak and cleanup, more real-project
dogfooding, usability improvements, and the future TypeScript authoring API.
See the [roadmap](docs/roadmap.md) for the full prioritized list.

## Documentation

The user-facing **HTML site** (purpose, tenets, and an API reference generated
from the source docstrings — public authoring API only). A prebuilt copy is
committed under `site/` — open `site/index.html` in a browser. To rebuild it
from `mkdocs.yml` (all offline, no network):

```
python -m venv .venv-docs && .venv-docs/bin/pip install -r docs/requirements.txt
.venv-docs/bin/mkdocs serve     # live preview at http://127.0.0.1:8000
.venv-docs/bin/mkdocs build     # regenerate ./site
```

The design notes below are the internal contracts (engine, wire schema, design
rules) — deeper than the site, for working *on* brix rather than *with* it:

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
