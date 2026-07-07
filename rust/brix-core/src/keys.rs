//! Structural fingerprint keys — an action's *stable identity* across builds,
//! independent of file content. Mirrors `action_key` / `membership_key` in
//! `src/brix/incremental.py`. These are content-independent (no file I/O), so
//! they are pinned by golden fixtures like the digest primitive.
//!
//! `action_key` = the command template plus input/output/env *shapes*
//! (`_expr_identity`); changing a source's bytes or a value's text changes the
//! *fingerprint*, not the key. `membership_key` = an expansion's element name
//! plus its collection's shape.

use std::collections::BTreeMap;

use crate::actions::Action;
use crate::data::{expr_identity, DataExpr};
use crate::fingerprint::{digest, Value};

fn shapes(pairs: &[(String, DataExpr)]) -> Value {
    let mut map = BTreeMap::new();
    for (key, expr) in pairs {
        map.insert(key.clone(), expr_identity(expr));
    }
    Value::Map(map)
}

/// The stable key of a process action, or `None` for non-process actions.
pub fn action_key(action: &Action) -> Option<String> {
    let Action::Process { command_line, inputs, outputs, env, .. } = action else {
        return None;
    };
    let mut payload = BTreeMap::new();
    payload.insert("command".to_string(), Value::str(command_line));
    payload.insert("inputs".to_string(), shapes(inputs));
    payload.insert("outputs".to_string(), shapes(outputs));
    if !env.is_empty() {
        payload.insert("env".to_string(), shapes(env));
    }
    Some(digest(&Value::Map(payload)))
}

/// The stable key of an expansion point's collection, or `None` otherwise.
pub fn membership_key(action: &Action) -> Option<String> {
    let Action::Expand { element, collection, .. } = action else {
        return None;
    };
    let mut payload = BTreeMap::new();
    payload.insert("element".to_string(), Value::str(element));
    payload.insert("collection".to_string(), expr_identity(collection));
    Some(digest(&Value::Map(payload)))
}
