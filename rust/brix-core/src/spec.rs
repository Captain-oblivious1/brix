//! The graph spec — the language-neutral `{ "version", "actions" }` value the
//! skin lowers into and the core deserializes. See `docs/boundary-schema.md`.

use serde::Deserialize;

use crate::actions::Action;

#[derive(Debug, Clone, Deserialize)]
pub struct GraphSpec {
    #[serde(default)]
    pub version: i64,
    pub actions: Vec<Action>,
}

/// Deserialize a graph spec from JSON.
pub fn parse(json: &str) -> Result<GraphSpec, String> {
    serde_json::from_str(json).map_err(|e| e.to_string())
}
