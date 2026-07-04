"""Builders: choose how the logical graph is built.

`CleanBuilder` is the root builder for this prototype: it lowers the logical
graph into an execution plan and runs it with an empty layer stack. Wrapper
builders (incremental, cache, atomic) arrive in later milestones; each will
contribute a `BuildLayer` to the stack rather than transforming plans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .graph import Graph
from .plan import ExecutionPlan, NodeState, NodeStateTable
from .planner import plan_graph
from .process import ProcessResult


@dataclass
class BuildResult:
    success: bool
    plan: ExecutionPlan
    states: NodeStateTable = field(default_factory=NodeStateTable)
    process_results: list[ProcessResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        counts: dict[NodeState, int] = {}
        for node in self.plan.nodes:
            status = self.states.status(node.id)
            counts[status] = counts.get(status, 0) + 1
        parts = [f"{count} {state.value}" for state, count in sorted(counts.items(), key=lambda i: i[0].value)]
        status = "BUILD OK" if self.success else "BUILD FAILED"
        return f"{status} ({', '.join(parts)})"


class CleanBuilder:
    """Builds everything the graph declares; no change detection."""

    def __init__(self, graph: Graph, root: str | Path = ".") -> None:
        self.graph = graph
        self.root = Path(root).resolve()

    def plan(self) -> ExecutionPlan:
        return plan_graph(self.graph, self.root)

    def build(self, jobs: int | None = None, echo: bool = True) -> BuildResult:
        result = self.plan().run(jobs=jobs, echo=echo, layers=[])
        if echo:
            print(result.summary())
        return result


class IncrementalBuilder:
    """Skips work whose fingerprints prove it up to date.

    ``graph_supplier`` is a callable so the builder decides when and how
    much graph to load (a plain ``Graph`` is also accepted). Metadata lives
    under ``metadata_dir``; if it is missing the build degrades to clean
    behavior and records everything for next time.
    """

    def __init__(
        self,
        graph_supplier,
        root: str | Path = ".",
        metadata_dir: str | Path = "build/.brix",
    ) -> None:
        self._supplier = graph_supplier if callable(graph_supplier) else lambda: graph_supplier
        self.root = Path(root).resolve()
        self.metadata_dir = metadata_dir

    def plan(self) -> ExecutionPlan:
        return plan_graph(self._supplier(), self.root)

    def build(self, jobs: int | None = None, echo: bool = True) -> BuildResult:
        from .incremental import IncrementalLayer
        from .metadata import BuildMetadata, TimeAndHashChangeDetector

        metadata = BuildMetadata(self.root, self.metadata_dir)
        metadata.load()
        detector = TimeAndHashChangeDetector(self.root, metadata)
        layer = IncrementalLayer(metadata, detector)
        result = self.plan().run(jobs=jobs, echo=echo, layers=[layer])
        if echo:
            print(result.summary())
        return result
