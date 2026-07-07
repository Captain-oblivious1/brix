//! `action_fingerprint`: everything that determines an action's outputs,
//! hashed. Mirrors `action_fingerprint` in `src/brix/incremental.py`. Computed
//! at node-ready time (all inputs exist); the cache layer uses the same value
//! as its key, so it must match Python byte-for-byte.
//!
//! Ported here: the content-hashing core over `file` / `glob` / `regex` /
//! `value` atoms. Deliberately *not* ported (each returns an error so a caller
//! can never get a silently-wrong key):
//!   - `tool` atoms — need PATH resolution + a stat signature (machine-specific
//!     mtime), so they can't be golden-pinned;
//!   - `identity=` (content-identity plugins like `jvm-abi`) — need the plugin
//!     registry, which lives in Python;
//!   - the `worker` toolchain leg — the Rust `Action` carries no worker yet.
//! These are the fingerprint's remaining tail, tracked in the README.

use std::collections::BTreeMap;

use crate::actions::Action;
use crate::context::NodeContext;
use crate::data::{DataAtom, DataExpr};
use crate::detector::TimeAndHashChangeDetector;
use crate::fingerprint::{digest, Value};
use crate::patterns::PathPattern;

fn hash_value(h: Option<String>) -> Value {
    match h {
        Some(s) => Value::Str(s),
        None => Value::Null,
    }
}

fn atom_fp(
    atom: &DataAtom,
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
) -> Result<Value, String> {
    match atom {
        DataAtom::File { path, identity } => {
            if identity.is_some() {
                return Err(format!("content-identity atoms are not ported: file {path}"));
            }
            let physical = ctx.read_path(path);
            Ok(Value::List(vec![
                Value::str("file"),
                Value::str(path),
                hash_value(det.hash_file(&physical)),
            ]))
        }
        DataAtom::Glob { template, identity } => {
            if identity.is_some() {
                return Err(format!("content-identity atoms are not ported: glob {template}"));
            }
            let pattern = PathPattern::new(template)?;
            let entries = ctx
                .find(&pattern)
                .into_iter()
                .map(|(logical, physical, _)| {
                    Value::List(vec![Value::str(&logical), hash_value(det.hash_file(&physical))])
                })
                .collect();
            Ok(Value::List(vec![
                Value::str("glob"),
                Value::str(template),
                Value::List(entries),
            ]))
        }
        DataAtom::Regex { pattern } => {
            let entries = ctx
                .iter_files()
                .into_iter()
                .filter(|(logical, _)| atom.regex_match(logical).is_some())
                .map(|(logical, physical)| {
                    Value::List(vec![Value::str(&logical), hash_value(det.hash_file(&physical))])
                })
                .collect();
            Ok(Value::List(vec![
                Value::str("regex"),
                Value::str(pattern),
                Value::List(entries),
            ]))
        }
        DataAtom::Value { text } => Ok(Value::List(vec![Value::str("value"), Value::str(text)])),
        DataAtom::Tool { name } => {
            Err(format!("tool atoms are not ported (PATH + stat signature): {name}"))
        }
    }
}

fn exprs_fp(
    pairs: &[(String, DataExpr)],
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
) -> Result<Value, String> {
    let mut map = BTreeMap::new();
    for (key, expr) in pairs {
        let mut list = Vec::with_capacity(expr.len());
        for atom in expr {
            list.push(atom_fp(atom, ctx, det)?);
        }
        map.insert(key.clone(), Value::List(list));
    }
    Ok(Value::Map(map))
}

/// The content fingerprint of a process action. `Err` for a non-process action
/// or an atom kind whose port is deferred (see the module docs). The payload
/// mirrors Python exactly: `command` + `inputs` (and `env` only when
/// non-empty); `outputs` are intentionally excluded (they do not determine
/// themselves). Keys are digested sorted, so the map ordering does not matter.
pub fn action_fingerprint(
    action: &Action,
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
) -> Result<String, String> {
    let Action::Process { command_line, inputs, env, .. } = action else {
        return Err("action_fingerprint is only defined for process actions".to_string());
    };
    let mut payload: BTreeMap<String, Value> = BTreeMap::new();
    payload.insert("command".to_string(), Value::str(command_line));
    payload.insert("inputs".to_string(), exprs_fp(inputs, ctx, det)?);
    if !env.is_empty() {
        payload.insert("env".to_string(), exprs_fp(env, ctx, det)?);
    }
    Ok(digest(&Value::Map(payload)))
}
