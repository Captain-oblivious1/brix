//! The execution plan: nodes and edges, plus `describe()` — whose textual
//! output must match Python's `ExecutionPlan.describe()` exactly (it is
//! asserted in the golden tests).

use crate::actions::Action;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeKind {
    Process,
    Expand,
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
        let edge = Edge { from_id, to_id, reason };
        if !self.edges.contains(&edge) {
            self.edges.push(edge);
        }
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
