"""Execution plan: the graph-shaped execution structure for one build.

The plan may be partially realized. Expansion points (`ExpandAll`) sit in the
plan as unrealized action nodes and are expanded into concrete process nodes
at runtime, once their collection membership is known.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .actions import Action
from .process import ProcessResult

if TYPE_CHECKING:
    from .builders import BuildResult


class NodeKind(enum.Enum):
    PROCESS = "process"
    EXPAND = "expand"


class NodeState(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ExecutionNode:
    id: int
    kind: NodeKind
    label: str
    action: Action
    state: NodeState = NodeState.PENDING
    result: ProcessResult | None = None


@dataclass(frozen=True)
class ExecutionEdge:
    from_id: int
    to_id: int
    reason: str


@dataclass
class ExecutionPlan:
    root: Path
    nodes: list[ExecutionNode] = field(default_factory=list)
    edges: list[ExecutionEdge] = field(default_factory=list)

    def add_node(self, kind: NodeKind, label: str, action: Action) -> ExecutionNode:
        node = ExecutionNode(id=len(self.nodes), kind=kind, label=label, action=action)
        self.nodes.append(node)
        return node

    def add_edge(self, from_id: int, to_id: int, reason: str) -> None:
        edge = ExecutionEdge(from_id, to_id, reason)
        if edge not in self.edges:
            self.edges.append(edge)

    def predecessors(self, node_id: int) -> list[ExecutionNode]:
        return [self.nodes[e.from_id] for e in self.edges if e.to_id == node_id]

    def successors(self, node_id: int) -> list[ExecutionNode]:
        return [self.nodes[e.to_id] for e in self.edges if e.from_id == node_id]

    def run(self, jobs: int | None = None, echo: bool = True) -> BuildResult:
        from .executor import execute

        return execute(self, jobs=jobs, echo=echo)

    def describe(self) -> str:
        """A plain-text rendering of the plan, for inspection."""
        lines = []
        for node in self.nodes:
            marker = "?" if node.kind is NodeKind.EXPAND else "*"
            lines.append(f"{marker} [{node.id}] {node.label} ({node.state.value})")
            for edge in self.edges:
                if edge.to_id == node.id:
                    lines.append(f"    <- [{edge.from_id}] {self.nodes[edge.from_id].label}: {edge.reason}")
        return "\n".join(lines)
