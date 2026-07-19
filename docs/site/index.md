# Brix

**Brix is a library for building your project from your own Python code** — an
API small enough to read in a sitting, yet comprehensive enough to express a
real, multi-language build. You describe the files and data each step uses; Brix
plans the work, infers the dependencies, runs independent steps in parallel, and
skips work whose inputs have not *meaningfully* changed.

The Python API is the build language; the engine underneath gives you three
things a serious build needs:

- **Fast.** The engine is written in Rust and owns the planning, scheduling, and
  change detection, and it keeps compiler processes warm between builds — so even
  large builds, and every edit-compile loop, stay quick.
- **Atomic.** A build is all-or-nothing, like a `git` commit — it either applies
  in full or leaves your workspace exactly as it was, so a failure never leaves a
  half-built tree.
- **Incremental builds that match a clean build, exactly.** Rebuilding only what
  changed is never a correctness compromise: an incremental build produces the
  same result a from-scratch build would — down to removing outputs whose source
  is gone.

That engine is the default today and is designed to back a future
JavaScript/TypeScript authoring API as well.

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

## Why Brix

Almost nobody wants to become an expert in their build system. People want it to
be easy to write, fast, and correct — and to otherwise stay out of the way.

Yet build tools make you pick the wrong trade-off. On one side,
simple-but-unsafe: a Makefile is quick to write but silently wrong — it misses a
changed header, has no real notion of what "changed" means, and happily ships a
stale build. On the other, correct-but-heavy: Bazel and Buck are correct, but
they are a sealed world with their own language, their own sandbox, and a
rewrite-everything migration; Gradle is powerful, but that power turns build
files into imperative code that rots.

Brix takes a different bet: **write your build in ordinary Python — declare what
each step reads and writes — and get the incremental correctness and safety of
the heavy tools without the DSL, the sandbox, or the migration cliff.** You
author with Python's full power (a loop to stamp out fifty services, a function
to factor a shared pattern), but a step's *meaning* is a small, declarative
vocabulary: its inputs and its outputs — declared coarsely, as patterns, not
file-by-file. You name `src/*.c`; Brix discovers each file's actual `#include`
headers (and a TypeScript module's imports) for you, so you never hand-maintain
a dependency list. From there the engine infers the dependency edges, runs
independent work in parallel, skips anything whose meaning has not changed, and
refuses any graph it cannot build correctly. Expressiveness for assembling the
build; rails around what the build means.

This is not aspirational. The engine is cross-checked between a Python and a Rust
implementation that must produce byte-identical decisions; content-key *lenses*
compare a Java jar by its ABI and a TypeScript module by its declared types, so
an implementation-only edit lets dependents replay instead of rebuild; atomic
staging means a failed build never half-updates your tree; and on JUnit 5's
platform modules the warm edit-compile loop runs 5–12× faster than Gradle. On a
large monorepo Brix loads and builds only the slice a change affects, and it can
adopt an existing project's Maven or Gradle classpath — so you migrate
incrementally rather than all at once.

And because those build files are small, declarative Python — the language
coding assistants know best — an assistant can *write and maintain them for
you*, and check its own work by running the build: `status` explains what would
happen before it does, and the planner rejects an ambiguous graph outright. It
is not raw configurability that makes this work but expressiveness with rails — a
small, checkable vocabulary over a language the model already knows. The examples
in this repository were themselves authored this way.

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
