//! The graph spec — the language-neutral `{ "version", "actions" }` value the
//! skin lowers into and the core deserializes. See `docs/boundary-schema.md`.

use serde::Deserialize;

use crate::actions::Action;
use crate::plan::EdgeTrigger;

#[derive(Debug, Clone, Deserialize)]
pub struct ForcedOrder {
    pub before: usize,
    pub after: usize,
    #[serde(default)]
    pub trigger: EdgeTrigger,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GraphSpec {
    #[serde(default)]
    pub version: i64,
    pub actions: Vec<Action>,
    #[serde(default)]
    pub forced_orders: Vec<ForcedOrder>,
}

/// Deserialize a graph spec from JSON.
pub fn parse(json: &str) -> Result<GraphSpec, String> {
    serde_json::from_str(json).map_err(|e| e.to_string())
}
