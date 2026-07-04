#!/usr/bin/env python3
"""Build-layer demo: watch the middleware stack work on a live build.

Run from anywhere:  python3 examples/layers_demo/brix.py

Two runs:

1. The full graph (uppercase each src/*.txt, then combine) with a TraceLayer
   attached -- it prints every hook the scheduler fires, so you can see the
   nodes created by runtime expansion pass through the same stack as nodes
   that existed at plan time.

2. A single-stage graph with a hand-rolled StagingLayer: outputs land in
   build/staging/ and are only copied into build/ because the build
   succeeded -- a tiny preview of what AtomicBuilder will do. (The real
   atomic layer will also let *downstream* nodes read staged outputs by
   resolving their inputs through the context; that lands with TODO #3/#5,
   which is why this demo stages a single-stage graph.)
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import (
    BuildLayer,
    CleanBuilder,
    ConsoleReporter,
    ExpandAll,
    ExternalProcessAction,
    Graph,
    NodeContext,
)

HERE = Path(__file__).parent


def upper_action(data) -> ExpandAll:
    return ExpandAll(
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
    )


def full_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        upper_action(data),
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


def upper_only_graph() -> Graph:
    graph = Graph()
    graph.add(upper_action(graph.data))
    return graph


class TraceLayer(BuildLayer):
    """Prints every hook. Changes nothing."""

    def on_build_begin(self, plan, ctx):
        print(f"  [trace] build begins: {len(plan.nodes)} node(s) in the plan")

    def around_node(self, node, ctx, inner):
        print(f"  [trace] -> entering {node.label!r}")
        result = inner(ctx)
        print(f"  [trace] <- leaving  {node.label!r} ({result.summary()})")
        return result

    def on_collection_realized(self, node, members, ctx):
        print(f"  [trace] collection realized: members={members}")

    def on_build_end(self, result, ctx):
        print(f"  [trace] build ends: success={result.success}")


class StagingLayer(BuildLayer):
    """Outputs land in build/staging/; commit to real locations on success."""

    def __init__(self, staging: Path):
        self.staging = staging

    def on_build_begin(self, plan, ctx):
        if self.staging.exists():
            shutil.rmtree(self.staging)
        print(f"  [staging] outputs redirected to {self.staging.relative_to(ctx.workspace_root)}/")

    def around_node(self, node, ctx, inner):
        staged = NodeContext(workspace_root=ctx.workspace_root, output_root=self.staging)
        return inner(staged)

    def on_build_end(self, result, ctx):
        if not result.success:
            print("  [staging] build failed -- nothing committed, workspace untouched")
            return
        for staged_file in sorted(self.staging.rglob("*")):
            if staged_file.is_file():
                real = ctx.workspace_root / staged_file.relative_to(self.staging)
                real.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_file, real)
                print(f"  [staging] committed {real.relative_to(ctx.workspace_root)}")
        shutil.rmtree(self.staging)


if __name__ == "__main__":
    if (HERE / "build").exists():
        shutil.rmtree(HERE / "build")

    print("=== run 1: full graph through a TraceLayer ===")
    plan = CleanBuilder(full_graph(), root=HERE).plan()
    print("plan before execution (expansion point still unrealized):")
    print(plan.describe())
    print()
    result = plan.run(layers=[TraceLayer()], reporter=ConsoleReporter())
    print(result.summary())
    print(f"combined output: {(HERE / 'build/all.txt').read_text()!r}")
    if not result.success:
        sys.exit(1)

    print()
    print("=== run 2: outputs through a hand-rolled staging layer ===")
    shutil.rmtree(HERE / "build")
    plan = CleanBuilder(upper_only_graph(), root=HERE).plan()
    # No reporter: the engine is silent; only the staging layer's own prints show.
    result = plan.run(layers=[StagingLayer(HERE / "build" / "staging")])
    print(result.summary())
    print(f"committed output: {(HERE / 'build/upper/one.txt').read_text()!r}")
    sys.exit(0 if result.success else 1)
