//! The reporter event stream. Mirrors the `Reporter` base in
//! `src/brix/report.py` and the event contract in `docs/boundary-schema.md`.
//!
//! Event-ordering contract (pinned by `tests/test_scheduler_contract.py` on
//! the Python side and `tests/executor.rs` here): `build_begin` is first and
//! `build_end` is last; per node, `node_started` < `process_started` <
//! `node_completed`; a `collection_realized` precedes its children's events.
//! `process_started` is emitted from worker threads (hence `Sync`) and fires
//! only when a real process is about to spawn — a replaying/caching layer
//! that answers without calling inward prevents it.

use crate::plan::{ExecutionPlan, Node, NodeState};
use crate::process::ProcessResult;

/// All hooks are optional no-ops; implementations record or print.
/// `&self` receivers + `Sync` because `process_started` arrives from worker
/// threads — implementations use interior mutability to accumulate.
#[allow(unused_variables)]
pub trait Reporter: Sync {
    fn build_begin(&self, plan: &ExecutionPlan) {}
    fn node_started(&self, node: &Node) {}
    fn process_started(&self, node: &Node) {}
    fn node_completed(
        &self,
        node: &Node,
        status: NodeState,
        result: Option<&ProcessResult>,
        error: Option<&str>,
    ) {
    }
    fn node_skipped(&self, node: &Node, why: &str) {}
    fn collection_realized(&self, node: &Node, members: &[String], children: &[&Node]) {}
    fn node_inserted(&self, node: &Node, reason: &str) {}
    fn build_end(&self, result: &crate::executor::BuildResult) {}
}

/// The silent default (Python's bare `Reporter()`).
pub struct NullReporter;

impl Reporter for NullReporter {}
