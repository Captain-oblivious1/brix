# Brix

## TL;DR

Developers should not have to become build-system experts—or learn a language
used only by their build system—just to build their code. They want builds to be
easy to describe, fast, correct, and otherwise out of the way.

Brix is a **library of building blocks for creating builds in languages people
already know**—thus the name “Brix.” Brix currently provides those building
blocks as a Python library, so you can describe anything from a tiny build to a
large, complex monorepo without implementing your own scheduler, incremental
engine, cache, or safety machinery. You can build TypeScript projects with Brix
today, but the build definition itself must be written in Python;
JavaScript/TypeScript build-file authoring is planned but is **not supported
yet**. Python and JavaScript are widely used and well understood by AI coding
assistants, so an assistant can help create, explain, and maintain your build
instead of asking you to master another build language.

The repository also includes plenty of working examples that you can copy, run,
and adapt instead of starting from a blank file.

That is the short version. Since nobody wants to become an expert in a build
system, you should not have to read its entire manual before deciding whether it
is useful. The fuller version—and a small example—follow.

## What Brix gives you

Brix is a library rather than a fixed policy. Start with a direct build, then
add and configure the higher-level pieces your project needs.

### Fast builds

The performance-sensitive engine is implemented in Rust and exposed through an
ordinary Python API. Brix runs independent work in parallel and avoids work that
does not need to happen.

### Incremental builds you can trust

An incremental build must produce exactly what a clean build would. That
includes deleting downstream artifacts that would no longer exist after a clean
build. You should not need to repeatedly clean everything because you have lost
confidence in your incremental build.

### Git-inspired atomic builds

Brix can show the current build status, stage a complete build for review, and
then commit or discard it. A normal atomic build becomes visible only when the
whole build succeeds, so a failure does not leave your workspace half updated.

### Local and shared caching

Brix can reuse work from an earlier build instead of running it again. A shared
cache lets a developer restore work already completed by another workspace, a
teammate, or CI.

### Less dependency bookkeeping

Brix derives relationships and build order from the inputs and outputs your
steps already declare, reducing the need to maintain a separate target
dependency graph. It can also discover fine-grained dependencies such as C/C++
headers and TypeScript imports automatically.

By comparison, Bazel and Buck generally require every target to list its direct
target dependencies explicitly. Brix lets the data flowing between steps define
those relationships instead of asking you to state the same relationship again
as a target-to-target edge.

This does not mean dependencies become magic. You still declare things that a
tool genuinely needs, such as a Java classpath or runtime data. Brix avoids
making you separately hand-wire the same information into a second dependency
graph.

### From a tiny project to a monorepo

A small project can have one short `brix_build.py`. As the repository grows, build
files can be placed at natural package or service boundaries. Brix can read and
execute only the build files needed for the affected part of a large monorepo.

### Explainable and configurable

Incremental building, caching, and atomic updates are composable features rather
than mandatory policy. Commands such as `status`, `graph`, `help`, and `env`
show what Brix sees, what would run, and how the project is configured. Brix
also rejects ambiguous builds, such as two steps that may write the same output,
before running them.

## Set up a Brix project

Install Brix into your project's virtual environment. Until published packages
are available, install from a Brix source checkout or a wheel built from it:

```sh
python -m pip install /path/to/brix
# Or install a wheel you built/downloaded:
python -m pip install "$HOME/Downloads"/brix-*.whl
```

Put this standalone `brix_build.py` in the root of your project:

```python
#!/usr/bin/env python3
from pathlib import Path

from brix import Graph, cli

ROOT = Path(__file__).resolve().parent

def create_graph() -> Graph:
    graph = Graph()
    # Add your build actions here.
    return graph

if __name__ == "__main__":
    raise SystemExit(cli.main(create_graph, root=ROOT))
```

The CLI is now installed for this project. Verify the setup before adding any
build actions:

```sh
python brix_build.py help       # show this project's live Brix configuration
```

Now replace the empty `create_graph()` body with the actions for your project.
The [starter templates](examples/templates/) provide complete Java and
TypeScript projects to copy, and the [examples index](examples/README.md) covers
progressively larger builds.

## A small C++ build

Use this exact import line, then replace the empty `create_graph()` above with
the complete C++ build definition:

```python
from brix import Graph, cli, cpp

def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/{name}.o"),
        ),
        cpp.LinkApplicationAction(
            object_files=data.glob("build/{name}.o"),
            output_file=data.file("build/app"),
        ),
    )
    return graph
```

With one or more `.cpp` files under `src/`:

```sh
python brix_build.py build
./build/app
```

## A small Java build

Java is the same shape. Use this import and `create_graph()` to compile every
source under `src/main/java` into a runnable jar (a JDK must be on `PATH`):

```python
from brix import Graph, cli, java

def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        java.JarAction(
            source_files=data.glob("src/main/java/{name}.java"),
            output_file=data.file("build/app.jar"),
            entry_point="com.example.App",
        )
    )
    return graph
```

Set `entry_point` to the class containing `public static void main`, then run:

```sh
python brix_build.py build
java -jar build/app.jar
```

After the first successful build, you can add caching and atomic updates by
changing the import and final call:

```python
from brix import Graph, build_stack, cli, java  # or cpp

# create_graph() stays exactly the same

if __name__ == "__main__":
    raise SystemExit(
        cli.main(
            create_graph,
            root=ROOT,
            builder_factory=lambda: build_stack(create_graph, root=ROOT),
        )
    )
```

`build_stack` supplies the recommended incremental + cache + atomic ordering.
Pass `cache_dir=None` or `staging_dir=None` to disable either optional layer.

The typed language helpers are optional. `ExternalProcessAction` is the escape
hatch for any application, existing build tool, or project-specific generator.
For example, add it to the import in `brix_build.py`:

```python
from brix import ExternalProcessAction, Graph, cli
```

Then put these actions inside `create_graph()`, after `graph` and `data` are
created and before `return graph`:

```python
# Inside create_graph()
graph.add(
    ExternalProcessAction(
        name="generate protocol sources",
        command_line=(
            "{protoc} --proto_path=schema --cpp_out=build/generated {schema}"
        ),
        inputs={
            "protoc": data.tool("protoc"),
            "schema": data.file("schema/messages.proto"),
        },
        outputs={
            "source": data.file("build/generated/messages.pb.cc"),
            "header": data.file("build/generated/messages.pb.h"),
        },
    ),
    ExternalProcessAction(
        name="compile generated protocol sources",
        command_line="{compiler} -Ibuild/generated -c {source} -o {object}",
        inputs={
            "compiler": data.tool("c++"),
            "source": data.file("build/generated/messages.pb.cc"),
            "header": data.file("build/generated/messages.pb.h"),
        },
        outputs={"object": data.file("build/messages.pb.o")},
    ),
)
```

Brix sees that the compile inputs overlap the generator outputs, so it orders
the actions automatically. Changing the schema regenerates and recompiles;
an unchanged schema lets both actions replay.

See [Getting started](docs/getting-started.md) for runnable examples and the
usual development commands.

## For the technically curious

You do not need these details to use Brix, but they explain how the high-level
promises above are implemented.

- **Rust engine.** Rust handles planning, change detection, scheduling, caching,
  and execution. A Python reference implementation is checked against it with
  shared test cases so changes to the production engine remain trustworthy.
- **Warm compiler processes.** Compilers such as `javac` can remain alive across
  builds, avoiding repeated startup and warm-up costs during the edit-compile
  loop. Independent steps run in parallel.
- **Meaningful-change detection.** Brix can compare the part of a dependency its
  consumers actually use. For Java, that is the ABI—the public classes, methods,
  fields, and related details dependent code can access. For TypeScript, it is
  the emitted type declarations. A private implementation change can therefore
  rebuild and retest its own module without recompiling every dependent module.
- **Discovered dependencies.** Compiler depfiles teach Brix which C/C++ headers
  were actually read. A TypeScript compiler wrapper records the transitive
  import graph through the same mechanism.
- **Incremental-versus-clean checks.** Equivalence tests apply changes, build
  incrementally, build again from scratch, and compare the resulting files.
  These tests cover ordinary files, deleted sources, Java public-interface
  changes, and compiler-discovered dependencies.
- **Atomic staging.** New and deleted outputs are held separately from the
  workspace until the build can be committed as a unit. Interrupted commits can
  be completed safely, while failed staging is kept for inspection or discarded.
- **Content-addressed caching.** Cache entries are keyed by the action and its
  tracked inputs. Entries can live locally, on an HTTP service, or in a two-tier
  local-plus-shared arrangement.
- **Lazy monorepo loading.** An index of package ownership, declared patterns,
  and cross-package dependencies lets Brix identify which distributed build
  files may be affected. Safety tests require a selectively loaded build to
  produce the same result as loading the entire workspace.

<details>
<summary>What the typed C++ compile and link helpers expand into</summary>

The language helpers are conveniences over Brix's generic action model, not a
separate engine. The typed C++ example above corresponds to this basic
`ExternalProcessAction` graph. The helper also supplies C++-specific defaults,
including toolchain-environment and header tracking.

```python
from brix import ExpandAll, ExternalProcessAction, Graph

def create_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.cpp"),
            child=ExternalProcessAction(
                name="compile {name}",
                command_line=(
                    "{compiler} -MMD -MF {depfile} -c {source} -o {object}"
                ),
                inputs={
                    "compiler": data.tool("c++"),
                    "source": data.file("src/{name}.cpp"),
                },
                outputs={
                    "object": data.file("build/{name}.o"),
                    "depfile": data.file("build/{name}.o.d"),
                },
                depfile="build/{name}.o.d",
            ),
        ),
        ExternalProcessAction(
            name="link app",
            command_line="{linker} {objects} -o {app}",
            inputs={
                "linker": data.tool("c++"),
                "objects": data.glob("build/{name}.o"),
            },
            outputs={"app": data.file("build/app")},
        ),
    )
    return graph
```

</details>

## Current state

The Python authoring API is feature-complete for the currently supported use
cases, and the Rust core is the default engine. Python-authored build files can
compile and test Java, Kotlin, C, C++, and TypeScript projects today. Writing the
build file itself in JavaScript or TypeScript is the future API mentioned in the
TL;DR. The two engine implementations are cross-checked with shared golden
fixtures, while real-project examples also exercise Spring Boot, dependency
resolution, warm workers, and the build layers.

The remaining work is mainly Rust-engine soak and cleanup, more real-project
dogfooding, usability improvements, and the future non-Python authoring API.
See the [roadmap](docs/roadmap.md) for the full prioritized list.

**In flight:** [test-impact selection](docs/test-impact-selection.md) — run only
the tests a change could affect, under the same absolute equivalence guarantee
brix already makes for incremental vs. clean builds. The language-agnostic core
and the Java provider are built and held to an oracle that edits code and runs a
real JUnit; nothing is wired into a build yet. Handed to Fable (2026-07-23) for
the escape-completeness audit and then the fingerprint/cache bookkeeping; the
C++ provider waits on that audit.

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
