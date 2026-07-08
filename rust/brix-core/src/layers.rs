//! Build-layer seams: the `BuildLayer` hook interface and `NodeProposal`.
//! Mirrors `src/brix/layers.py` (whose `NodeContext` half lives in
//! `context.rs`).
//!
//! Capabilities (incremental, cache, atomic) attach to execution as per-node
//! middleware plus build-scoped events — never as static plan transforms,
//! which would miss nodes created by runtime expansion.

use crate::context::NodeContext;
use crate::executor::BuildResult;
use crate::plan::{ExecutionPlan, Node};
use crate::process::ProcessResult;

/// Work a layer proposes; the scheduler inserts and wires it.
///
/// This is the insertion channel from the layer side: layers never touch
/// plan structure. Proposals returned from `on_collection_realized` are
/// ordered before the collection's consumers.
#[derive(Debug, Clone)]
pub struct NodeProposal {
    pub action: crate::actions::Action,
    pub label: Option<String>,
}

/// The inner continuation of `around_node`: call it to pass control inward
/// (toward the real process spawn), or answer without calling it (replay,
/// cache hit).
pub type NodeRunner<'a> = dyn Fn(&NodeContext) -> ProcessResult + 'a;

/// Hook interface for build capabilities. All hooks are optional.
///
/// Layers never touch plan structure or node state directly; they obtain
/// results (`around_node`), adjust the context, and propose work through the
/// scheduler. `around_node` runs on worker threads, so implementations are
/// `Sync` and keep mutable state behind interior mutability.
#[allow(unused_variables)]
pub trait BuildLayer: Sync {
    /// Build is starting (e.g. prepare staging, recover a crashed build).
    /// The one hook that may adjust the shared context (runs before any
    /// worker thread exists).
    fn on_build_begin(&self, plan: &ExecutionPlan, ctx: &mut NodeContext) {}

    /// Obtain the node's result: answer without calling `inner` (skip /
    /// cache hit), adjust the context passed inward, or both.
    fn around_node(&self, node: &Node, ctx: &NodeContext, inner: &NodeRunner) -> ProcessResult {
        inner(ctx)
    }

    /// An expansion point's membership just became known. May return node
    /// proposals (e.g. stale-output deletions); the scheduler inserts them
    /// and orders them before the collection's consumers.
    fn on_collection_realized(
        &self,
        node: &Node,
        members: &[String],
        ctx: &NodeContext,
    ) -> Vec<NodeProposal> {
        Vec::new()
    }

    /// Build finished (e.g. commit staged outputs if everything succeeded).
    fn on_build_end(&self, result: &BuildResult, ctx: &NodeContext) {}
}
