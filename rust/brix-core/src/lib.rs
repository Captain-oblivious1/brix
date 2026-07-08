//! The brix build engine core, ported to Rust.
//!
//! Phases 1–2b (see `docs/rust-port.md`): patterns, data atoms, fingerprint
//! digest, shlex, planner/targets, and the content fingerprint — validated
//! against golden fixtures generated from the real Python implementation
//! (`rust/golden/generate.py`), because these are cross-machine cache keys
//! and wire values: they must match Python byte-for-byte, not merely
//! "closely". Phase 3 (in progress): the scheduler (`executor.rs`), process
//! execution and rendering, and the build-layer seams — pinned by
//! `tests/executor.rs`, the Rust mirror of `tests/test_scheduler_contract.py`.
//!
//! No PyO3 in this crate — the Python and (future) JS/TS bindings wrap it.

pub mod actions;
pub mod cache;
pub mod content;
pub mod context;
pub mod data;
pub mod detector;
pub mod executor;
pub mod fingerprint;
pub mod fsutil;
pub mod incremental;
pub mod keys;
pub mod layers;
pub mod metadata;
pub mod patterns;
pub mod plan;
pub mod planner;
pub mod process;
pub mod render;
pub mod report;
pub mod shlex;
pub mod spec;
pub mod targets;

pub use actions::Action;
pub use cache::{CacheLayer, CacheStore, CacheStoreProtocol, TwoTierCacheStore};
pub use content::action_fingerprint;
pub use context::NodeContext;
pub use detector::TimeAndHashChangeDetector;
pub use executor::{execute, BuildResult};
pub use fingerprint::{digest, Value};
pub use incremental::IncrementalLayer;
pub use keys::{action_key, membership_key};
pub use layers::{BuildLayer, NodeProposal};
pub use metadata::BuildMetadata;
pub use patterns::{overlaps, PathPattern};
pub use plan::{EdgeTrigger, ExecutionPlan, NodeKind, NodeState, NodeStateTable};
pub use planner::plan_graph;
pub use process::{run_process, ProcessResult};
pub use report::{NullReporter, Reporter};
pub use spec::{parse, GraphSpec};
