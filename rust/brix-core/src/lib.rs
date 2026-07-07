//! The brix build engine core, ported to Rust.
//!
//! Phase 1 (see `docs/rust-port.md`): the bottom layer with no scheduler —
//! path patterns, data atoms, the fingerprint digest, and shlex splitting.
//! Every piece here is validated against golden fixtures generated from the
//! real Python implementation (`rust/golden/generate.py`), because these are
//! cross-machine cache keys and wire values: they must match Python
//! byte-for-byte, not merely "closely".
//!
//! No PyO3 in this crate — the Python and (future) JS/TS bindings wrap it.

pub mod actions;
pub mod data;
pub mod fingerprint;
pub mod patterns;
pub mod plan;
pub mod planner;
pub mod shlex;
pub mod spec;
pub mod targets;

pub use actions::Action;
pub use fingerprint::{digest, Value};
pub use patterns::{overlaps, PathPattern};
pub use plan::ExecutionPlan;
pub use planner::plan_graph;
pub use spec::{parse, GraphSpec};
