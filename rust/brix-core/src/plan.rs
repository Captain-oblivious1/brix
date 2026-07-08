//! The execution plan: nodes and edges, plus `describe()` — whose textual
//! output must match Python's `ExecutionPlan.describe()` exactly (it is
//! asserted in the golden tests).
//!
//! Ground rules (mirroring `src/brix/plan.py`, see docs/build-layers.html):
//! the plan is append-only, node definitions are frozen at insertion, and
//! everything that varies at run time lives in the scheduler-owned
//! `NodeStateTable`, where status only moves forward.

use std::collections::HashMap;

use crate::actions::Action;
use crate::process::ProcessResult;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeKind {
    Process,
    Expand,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeState {
    Pending,
    Running,
    Done,
    Failed,
    Skipped,
}

impl NodeState {
    /// Monotone status rank: pending(0) -> running(1) -> terminal(2).
    fn rank(self) -> u8 {
        match self {
            NodeState::Pending => 0,
            NodeState::Running => 1,
            NodeState::Done | NodeState::Failed | NodeState::Skipped => 2,
        }
    }

    pub fn is_terminal(self) -> bool {
        self.rank() == 2
    }
}

/// When an edge lets its target proceed.
///
/// `OnSuccess` is the default: the source must be DONE, and a failure
/// cascades a skip. `OnCompletion` fires whether the source succeeded or
/// failed — for cleanup that must run *because* its trigger failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EdgeTrigger {
    #[default]
    OnSuccess,
    OnCompletion,
}

#[derive(Debug, Clone)]
pub struct Node {
    pub id: usize,
    pub kind: NodeKind,
    pub label: String,
    pub action: Action,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Edge {
    pub from_id: usize,
    pub to_id: usize,
    pub reason: String,
    pub trigger: EdgeTrigger,
}

/// Per-node execution state, owned by the scheduler.
///
/// Status is monotone: pending -> running -> done | failed | skipped, never
/// backwards and never out of a terminal state. Nodes without an entry are
/// pending — so nodes inserted mid-build need no registration.
#[derive(Debug, Default)]
pub struct NodeStateTable {
    status: HashMap<usize, NodeState>,
    result: HashMap<usize, ProcessResult>,
}

impl NodeStateTable {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn status(&self, node_id: usize) -> NodeState {
        self.status.get(&node_id).copied().unwrap_or(NodeState::Pending)
    }

    pub fn result(&self, node_id: usize) -> Option<&ProcessResult> {
        self.result.get(&node_id)
    }

    /// Move a node's status forward. Panics on a non-monotone move — that is
    /// a scheduler bug, not a build failure (mirrors Python's `ValueError`).
    pub fn advance(&mut self, node_id: usize, status: NodeState, result: Option<ProcessResult>) {
        let current = self.status(node_id);
        assert!(
            status.rank() > current.rank(),
            "node {node_id}: cannot move {current:?} -> {status:?}; status is monotone"
        );
        self.status.insert(node_id, status);
        if let Some(r) = result {
            self.result.insert(node_id, r);
        }
    }
}

#[derive(Debug, Clone)]
pub struct ExecutionPlan {
    pub root: String,
    pub nodes: Vec<Node>,
    pub edges: Vec<Edge>,
}

impl ExecutionPlan {
    pub fn new(root: impl Into<String>) -> Self {
        ExecutionPlan { root: root.into(), nodes: Vec::new(), edges: Vec::new() }
    }

    pub fn add_node(&mut self, kind: NodeKind, label: String, action: Action) -> usize {
        let id = self.nodes.len();
        self.nodes.push(Node { id, kind, label, action });
        id
    }

    pub fn add_edge(&mut self, from_id: usize, to_id: usize, reason: String) {
        self.add_edge_triggered(from_id, to_id, reason, EdgeTrigger::OnSuccess);
    }

    pub fn add_edge_triggered(
        &mut self,
        from_id: usize,
        to_id: usize,
        reason: String,
        trigger: EdgeTrigger,
    ) {
        let edge = Edge { from_id, to_id, reason, trigger };
        if !self.edges.contains(&edge) {
            self.edges.push(edge);
        }
    }

    pub fn incoming(&self, node_id: usize) -> impl Iterator<Item = &Edge> {
        self.edges.iter().filter(move |e| e.to_id == node_id)
    }

    pub fn predecessor_ids(&self, node_id: usize) -> Vec<usize> {
        self.edges.iter().filter(|e| e.to_id == node_id).map(|e| e.from_id).collect()
    }

    pub fn successor_ids(&self, node_id: usize) -> Vec<usize> {
        self.edges.iter().filter(|e| e.from_id == node_id).map(|e| e.to_id).collect()
    }

    /// Plain-text rendering, byte-identical to Python's `describe()`:
    /// each node as `<marker> [id] label` (`?` for expansion points, `*`
    /// otherwise), followed by its incoming edges.
    pub fn describe(&self) -> String {
        let mut lines: Vec<String> = Vec::new();
        for node in &self.nodes {
            let marker = if node.kind == NodeKind::Expand { "?" } else { "*" };
            lines.push(format!("{marker} [{}] {}", node.id, node.label));
            for edge in &self.edges {
                if edge.to_id == node.id {
                    lines.push(format!(
                        "    <- [{}] {}: {}",
                        edge.from_id, self.nodes[edge.from_id].label, edge.reason
                    ));
                }
            }
        }
        lines.join("\n")
    }
}
