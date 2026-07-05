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

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return plan_graph(self.graph, self.root, targets)

    def layers(self) -> list:
        return []

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None) -> BuildResult:
        return self.plan(targets).run(jobs=jobs, layers=self.layers(), reporter=reporter)


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

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return plan_graph(self._supplier(), self.root, targets)

    def layers(self) -> list:
        from .incremental import IncrementalLayer
        from .metadata import BuildMetadata, TimeAndHashChangeDetector

        metadata = BuildMetadata(self.root, self.metadata_dir)
        metadata.load()
        detector = TimeAndHashChangeDetector(self.root, metadata)
        return [IncrementalLayer(metadata, detector)]

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None) -> BuildResult:
        return self.plan(targets).run(jobs=jobs, layers=self.layers(), reporter=reporter)


class AtomicBuilder:
    """Either the whole build lands, or nothing on disk changes.

    Wraps another builder; its layer sits outermost, so process outputs
    and (later) cache-restored outputs all pass through staging.
    """

    def __init__(self, inner, staging_dir: str | Path = "build/staging") -> None:
        self.inner = inner
        self.staging_dir = staging_dir
        self.root: Path = inner.root

    def plan(self, targets: list[str] | None = None) -> ExecutionPlan:
        return self.inner.plan(targets)

    def layers(self) -> list:
        from .atomic import AtomicLayer

        return [*self.inner.layers(), AtomicLayer(self.staging_dir)]

    def build(self, jobs: int | None = None, reporter=None,
              targets: list[str] | None = None) -> BuildResult:
        return self.plan(targets).run(jobs=jobs, layers=self.layers(), reporter=reporter)
