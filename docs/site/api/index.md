# API Reference

This reference is generated directly from the source docstrings, so it always
matches the installed version. It documents **only the public authoring
surface** — what you write build files with. The engine internals (planner,
scheduler, incremental metadata, the Rust core) are deliberately not here.

Everything you author against is reachable from the top-level `brix` package:

```python
from brix import Graph, IncrementalBuilder, cli, cpp, java
```

| Section | What's in it |
|---|---|
| [Core & builders](core.md) | The graph and action model, the builder/layer stack, reporters, results, and workspace loading. |
| [Data model](data.md) | Data expressions — files, globs, values, tools — the inputs and outputs actions declare. |
| [C++](cpp.md) · [Java](java.md) · [Kotlin](kotlin.md) · [TypeScript](ts.md) · [Spring Boot](springboot.md) | Typed language helpers that lower to the generic model. |
| [Command line](cli.md) | The `cli.main` entry point a project's `brix.py` exposes. |
| [Extending brix](extending.md) | The plug-in seams: cache stores, content lenses, and change detectors. |
