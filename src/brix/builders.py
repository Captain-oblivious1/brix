"""Builders: choose how the logical graph is built.

`CleanBuilder` is the root builder for this prototype. Wrapper layers
(incremental, cache, atomic) arrive in later milestones and transform
execution plans rather than user declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .graph import Graph
from .plan import ExecutionPlan, NodeState
from .planner import plan_graph
from .process import ProcessResult


@dataclass
class BuildResult:
    success: bool
    plan: ExecutionPlan
    process_results: list[ProcessResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        counts: dict[NodeState, int] = {}
        for node in self.plan.nodes:
            counts[node.state] = counts.get(node.state, 0) + 1
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
        result = self.plan().run(jobs=jobs, echo=echo)
        if echo:
            print(result.summary())
        return result
