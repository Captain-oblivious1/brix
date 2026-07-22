#!/usr/bin/env python3
"""Incremental build demo: build, rebuild, change a file, delete a source.

Run from anywhere:  python3 examples/incremental_demo/brix_build.py

Four builds of the same graph (uppercase each src/*.txt, then combine),
using IncrementalBuilder so results persist in build/.brix/metadata.json:

1. cold build            -- every action runs
2. nothing changed       -- every action replays its previous result;
                            no process is spawned
3. one source edited     -- only its upper action and the combine rerun
4. one source deleted    -- the stale output is removed by an inserted
                            'rm' node and the combine reruns; the result
                            matches what a clean build would produce
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import ConsoleReporter, ExpandAll, ExternalProcessAction, Graph, IncrementalBuilder

HERE = Path(__file__).parent


def create_graph() -> Graph:
    graph = Graph()
    data = graph.data

    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": data.file("build/all.txt")},
        ),
    )

    return graph


def _real_runs() -> int:
    """The demo tools append a line to runs.log per actual invocation.

    The ConsoleReporter already marks replays via result provenance
    ("ok: upper one (replayed)"); this side channel is independent proof
    that no process was spawned.
    """
    log = HERE / "runs.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def build(title: str) -> None:
    print(f"=== {title} ===")
    before = _real_runs()
    result = IncrementalBuilder(create_graph, root=HERE).build(reporter=ConsoleReporter())
    print(f">>> processes actually spawned: {_real_runs() - before}")
    print(f">>> combined output: {(HERE / 'build/all.txt').read_text()!r}")
    print()
    if not result.success:
        sys.exit(1)


if __name__ == "__main__":
    # Reset the workspace so every demo run starts from the same place.
    if (HERE / "build").exists():
        shutil.rmtree(HERE / "build")
    (HERE / "runs.log").unlink(missing_ok=True)
    (HERE / "src").mkdir(exist_ok=True)
    for name, text in {"one": "brix ", "two": "builds ", "three": "fast"}.items():
        (HERE / "src" / f"{name}.txt").write_text(text)

    build("build 1: cold — everything runs")
    build("build 2: nothing changed — results replay, no processes spawn")

    (HERE / "src/two.txt").write_text("REBUILDS ")
    build("build 3: src/two.txt edited — only 'upper two' and 'combine' run")

    (HERE / "src/three.txt").unlink()
    build("build 4: src/three.txt deleted — watch the inserted 'rm' node")

    print("metadata lives in:", (HERE / "build/.brix/metadata.json").relative_to(HERE))
