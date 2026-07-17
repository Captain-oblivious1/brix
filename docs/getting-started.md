# Getting started

Brix requires Python 3.11 or newer. From a source checkout, create a virtual
environment and install the project in editable mode. Maturin builds the Rust
extension and installs the worker launcher automatically:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Set `BRIX_ENGINE=py` when deliberately running the Python reference engine.

## Try the repository examples

```sh
python3 -m pytest                        # run the test suite
python3 examples/hello_text/brix.py      # portable text-processing demo
python3 examples/raw_cpp/brix.py         # compile+link demo (needs c++)
python3 examples/layers_demo/brix.py     # watch the BuildLayer stack work
python3 examples/incremental_demo/brix.py  # build/rebuild/edit/delete-a-source
python3 examples/cli_demo/brix.py status   # project CLI: status/errors/build/clean
python3 examples/cli_demo/brix.py help     # the build file's live configuration
python3 examples/cpp_app/brix.py           # real C++, typed actions, all layers
python3 examples/cache_store_demo/brix.py  # pluggable/two-tier team cache across machines
```

## Write a build graph

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

Run the file once for a clean build, then again to see unchanged work replayed
when using an incremental builder. The teaching examples under `examples/`
show the incremental, cache, atomic, CLI, C++, Java, and TypeScript APIs in
increasingly realistic combinations. See the [specification](specification.md)
for the complete behavior and the [examples index](../examples/README.md) for
the full catalog.
