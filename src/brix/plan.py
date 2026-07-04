"""Execution plan: the graph-shaped execution structure for one build.

The plan may be partially realized. Expansion points (`ExpandAll`) sit in the
plan as unrealized action nodes and are expanded into concrete process nodes
at runtime, once their collection membership is known.

Ground rules (see docs/build-layers.html): the plan is append-only, node
definitions are frozen at insertion, and everything that varies at run time
lives in the scheduler-owned `NodeStateTable`, where status only moves
forward.
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
    from .layers import BuildLayer


class NodeKind(enum.Enum):
    PROCESS = "process"
    EXPAND = "expand"


class NodeState(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


_STATE_RANK = {
    NodeState.PENDING: 0,
    NodeState.RUNNING: 1,
    NodeState.DONE: 2,
    NodeState.FAILED: 2,
    NodeState.SKIPPED: 2,
}


@dataclass(frozen=True)
class ExecutionNode:
    id: int
    kind: NodeKind
    label: str
    action: Action


@dataclass(frozen=True)
class ExecutionEdge:
    from_id: int
    to_id: int
    reason: str


class NodeStateTable:
    """Per-node execution state, owned by the scheduler.

    Status is monotone: pending → running → done | failed | skipped, never
    backwards and never out of a terminal state. Nodes without an entry are
    pending — so nodes inserted mid-build need no registration.
    """

    def __init__(self) -> None:
        self._status: dict[int, NodeState] = {}
        self._result: dict[int, ProcessResult] = {}

    def status(self, node_id: int) -> NodeState:
        return self._status.get(node_id, NodeState.PENDING)

    def result(self, node_id: int) -> ProcessResult | None:
        return self._result.get(node_id)

    def advance(
        self, node_id: int, status: NodeState, result: ProcessResult | None = None
    ) -> None:
        current = self.status(node_id)
        if _STATE_RANK[status] <= _STATE_RANK[current]:
            raise ValueError(
                f"node {node_id}: cannot move {current.value} -> {status.value}; "
                "status is monotone"
            )
        self._status[node_id] = status
        if result is not None:
            self._result[node_id] = result


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

    def run(
        self,
        jobs: int | None = None,
        echo: bool = True,
        layers: list[BuildLayer] | None = None,
    ) -> BuildResult:
        from .executor import execute

        return execute(self, jobs=jobs, echo=echo, layers=layers)

    def describe(self) -> str:
        """A plain-text rendering of the plan's structure, for inspection."""
        lines = []
        for node in self.nodes:
            marker = "?" if node.kind is NodeKind.EXPAND else "*"
            lines.append(f"{marker} [{node.id}] {node.label}")
            for edge in self.edges:
                if edge.to_id == node.id:
                    lines.append(f"    <- [{edge.from_id}] {self.nodes[edge.from_id].label}: {edge.reason}")
        return "\n".join(lines)
