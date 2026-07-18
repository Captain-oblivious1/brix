# Brix

**Brix is a build-system toolkit for defining build graphs in ordinary Python.**
You describe the files and data each step uses; Brix plans the work, infers the
dependencies, runs independent steps in parallel, and skips work whose inputs
have not *meaningfully* changed.

The Python API is the build language. A shared Rust engine handles planning,
dependency inference, scheduling, incremental rebuilds, caching, atomic staging,
persistent workers, and content-aware comparisons — the same engine designed to
back a future JavaScript/TypeScript authoring API as well.

```python
from brix import Graph, cpp, cli
from pathlib import Path

def create_graph():
    g = Graph(); d = g.data
    g.add(cpp.CompileSourceAction(
        source_files=d.glob("src/{name}.c"),
        object_files=d.glob("build/obj/{name}.o"),
    ))
    g.add(cpp.LinkApplicationAction(
        object_files=d.glob("build/obj/{name}.o"),
        output_file=d.file("build/app"),
    ))
    return g

if __name__ == "__main__":
    cli.main(create_graph, root=Path(__file__).parent)
```

## What Brix is for

A build tool you can *read*: small, explicit build files with the safety and
speed of a serious build system. You declare intent — what each step consumes
and produces — and Brix works out the rest, so the build stays correct as a
project grows from one file to a fifty-service monorepo.

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
