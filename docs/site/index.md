# Brix

Developers should not have to become build-system experts—or learn a language
used only by their build system—just to build their code. They want builds to be
easy to describe, fast, correct, and otherwise out of the way.

Brix is a **library of building blocks for creating builds in languages people
already know**—thus the name “Brix.” It currently provides those blocks as an
ordinary Python library, backed by a production Rust engine. You can describe
anything from a tiny build to a large, complex monorepo without implementing a
scheduler, incremental engine, cache, or safety machinery yourself.

You can build TypeScript projects with Brix today, but build definitions must be
written in Python. JavaScript/TypeScript build-file authoring is planned but is
not supported yet.

## What Brix gives you

- **Fast builds.** The performance-sensitive engine is Rust, independent work
  runs in parallel, and work that does not need to happen is avoided.
- **Incremental builds you can trust.** An incremental build must produce what a
  clean build would, including deleting outputs that should no longer exist.
- **Git-inspired atomic builds.** Inspect status, stage a complete build, then
  commit or discard it without leaving a half-updated workspace.
- **Local and shared caching.** Reuse work from another build, workspace,
  teammate, or CI.
- **Less dependency bookkeeping.** Inputs and outputs establish relationships
  and ordering; C/C++ headers and TypeScript imports can be discovered
  automatically.
- **Small project to monorepo.** Distribute build files at package boundaries
  and load only the affected portion of a large repository.
- **Explainable and configurable.** Compose only the layers a project needs,
  while `status`, `graph`, `help`, and `env` explain what Brix sees.

Because Python and JavaScript are widely used and well understood by AI coding
assistants, an assistant can help create, explain, and maintain a Brix build.
The repository also contains working examples and starter templates to copy
instead of beginning with a blank file.

## A small C++ build

```python
from pathlib import Path
from brix import Graph, cli, cpp

ROOT = Path(__file__).resolve().parent

def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        cpp.CompileSourceAction(
            source_file=data.glob("src/{name}.cpp"),
            object_file=data.glob("build/{name}.o"),
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/{name}.o"),
            output_file=data.file("build/app"),
        ),
    )
    return graph

if __name__ == "__main__":
    raise SystemExit(cli.main(create_graph, root=ROOT))
```

## Tenets

These are the principles the design holds to; they explain why the API looks the
way it does.

- **Declare inputs and outputs, not order.** A step names the data it reads and
  writes. Brix infers the dependency edges from overlapping data sets and runs
  independent steps in parallel — you never hand-wire build order. (For the rare
  case where two steps are ordered without sharing a file, `Graph.force_order()`
  is the explicit escape hatch.)

- **An incremental build equals a clean build.** A rebuild produces exactly the
  output state a from-scratch build would — including *removing* outputs whose
  source is gone and rebuilding everything downstream of a real change. There is
  no half-updated tree to reason about.

- **Skip work when the *meaning* did not change.** Content-key lenses compare a
  file by what dependents actually depend on: a Java jar by its ABI, a
  TypeScript module by its declared types. An implementation-only edit produces
  an identical lens digest, so dependents replay instead of rebuilding — while a
  public-interface change still cascades.

- **One model, many languages.** Typed helpers for C++, Java, Kotlin, and
  TypeScript all lower to the same generic action/data model, so languages
  compose in a single graph. Correctness features (incrementality, caching,
  atomic staging) apply uniformly to every language, or to none — never a
  per-language feature matrix.

- **Refuse loudly rather than ship something stale.** Where Brix cannot yet
  guarantee a result — an atomic staging hazard, a lens that would be unsound
  for a language — it fails with a clear, identical error instead of silently
  producing a possibly-wrong build. Correctness outranks cleverness.

- **No surprises in the workspace.** `status` explains what would run and why
  before you run it; atomic staging means a failed build never partly changes
  your tree; and planning rejects any graph where two steps could write the same
  output. A build either completes cleanly or leaves things as they were.

- **Python composes; the engine is shared.** You author in ordinary Python. A
  shared Rust engine owns planning, scheduling, fingerprints, caching, and the
  performance-critical execution — the same language-neutral contract intended
  to back other authoring front-ends.

## API reference

The [API Reference](api/index.md) documents the public authoring surface,
generated directly from the code. It covers only what you build *with* — the
graph and action model, the typed language helpers, the data expressions, the
command-line entry point, and the extension seams — not the engine internals.
