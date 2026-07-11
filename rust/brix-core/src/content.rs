//! `action_fingerprint`: everything that determines an action's outputs,
//! hashed. Mirrors `action_fingerprint` in `src/brix/incremental.py`. Computed
//! at node-ready time (all inputs exist); the cache layer uses the same value
//! as its key, so it must match Python byte-for-byte.
//!
//! Ported here: the content-hashing core over `file` / `glob` / `regex` /
//! `value` / `tool` atoms, plus the discovered-deps fold (extra inputs a tool
//! reported reading — C/C++ headers from a depfile — content-hashed like any
//! input; absent when nothing was scanned, so it never perturbs an existing
//! fingerprint). Tool atoms carry `[name, resolved, stat_signature]` — a
//! machine-specific mtime, so they are exercised by behavioral tests, never
//! golden-pinned.
//!
//! Also ported: the `worker` toolchain leg — a worker's launch command and
//! inputs join the fingerprint exactly like Python's (a JDK upgrade or a
//! rebuilt worker jar invalidates everything that worker built) — and the
//! built-in `identity=` content identities (`jvm-abi`, `ts-dts`): the
//! fingerprint tail is `[tag, digest]` (e.g. `["jvm-abi@1", "<hex>"]`), or
//! `[tag:raw, byte_hash]` when the plugin cannot interpret the file. An
//! identity name the core does not know still returns an error (a custom
//! skin-registered plugin — the skin's engine owns those graphs), so a
//! caller can never get a silently-wrong key.

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

/// The fingerprint tail for a file carrying a content identity: `[tag,
/// digest]`, or `[tag:raw, byte_hash]` when the plugin cannot interpret the
/// file (mirrors `_identity_material`). Deterministic for the same bytes, so
/// a cross-machine disagreement can only miss, never make a stale hit. An
/// unknown identity name — not a built-in and not in the skin's
/// `content_identity` extension — errors loudly (before the fallback).
fn identity_material(
    name: &str,
    physical: &std::path::Path,
    det: &TimeAndHashChangeDetector,
) -> Result<[Value; 2], String> {
    let Some(tag) = det.identity_tag(name) else {
        return Err(format!("unknown content identity '{name}'"));
    };
    Ok(match det.identity_digest(physical, name) {
        Ok(digest) => [Value::str(&tag), hash_value(digest)],
        Err(_) => [Value::str(format!("{tag}:raw")), hash_value(det.hash_file(physical))],
    })
}

fn atom_fp(
    atom: &DataAtom,
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
) -> Result<Value, String> {
    match atom {
        DataAtom::File { path, identity } => {
            let physical = ctx.read_path(path);
            if let Some(name) = identity {
                let [tag, digest] = identity_material(name, &physical, det)?;
                return Ok(Value::List(vec![Value::str("file"), Value::str(path), tag, digest]));
            }
            Ok(Value::List(vec![
                Value::str("file"),
                Value::str(path),
                hash_value(det.hash_file(&physical)),
            ]))
        }
        DataAtom::Glob { template, identity, exclude } => {
            let pattern = PathPattern::with_exclude(template, exclude)?;
            let mut entries = Vec::new();
            for (logical, physical, _) in ctx.find(&pattern) {
                entries.push(Value::List(match identity {
                    Some(name) => {
                        let [tag, digest] = identity_material(name, &physical, det)?;
                        vec![Value::str(&logical), tag, digest]
                    }
                    None => vec![Value::str(&logical), hash_value(det.hash_file(&physical))],
                }));
            }
            let mut material = vec![
                Value::str("glob"),
                Value::str(template),
                Value::List(entries),
            ];
            // Only when present: pre-exclude fingerprints unchanged.
            if !exclude.is_empty() {
                let mut excl = vec![Value::str("exclude")];
                excl.extend(exclude.iter().map(|p| Value::str(p)));
                material.push(Value::List(excl));
            }
            Ok(Value::List(material))
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
            // Python: `atom.name if "/" in atom.name else shutil.which(...)`;
            // missing -> ["tool", name, None, None]. Workspace-relative paths
            // resolve against the workspace; absolute ones pass through the
            // join unchanged (PathBuf::join, like pathlib, replaces on abs).
            let resolved = if name.contains('/') {
                Some(name.clone())
            } else {
                crate::render::which(name)
            };
            Ok(match resolved {
                None => Value::List(vec![
                    Value::str("tool"),
                    Value::str(name),
                    Value::Null,
                    Value::Null,
                ]),
                Some(resolved) => {
                    let sig = det.stat_signature(&ctx.workspace_root.join(&resolved));
                    Value::List(vec![
                        Value::str("tool"),
                        Value::str(name),
                        Value::str(&resolved),
                        match sig {
                            Some(sig) => Value::List(sig.into_iter().map(Value::Int).collect()),
                            None => Value::Null,
                        },
                    ])
                }
            })
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

/// The digest of a worker's identity — the same `{command, inputs}` section
/// that joins every worker-routed action's fingerprint, digested standalone.
/// This is the cross-build daemon registry key (docs/java-fast-builds.md v2):
/// same JDK + same wrapper jar ⇒ same key, so a JDK upgrade or a rebuilt jar
/// simply misses the registry and starts a fresh daemon.
pub fn worker_fingerprint(
    spec: &crate::actions::WorkerSpec,
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
) -> Result<String, String> {
    let mut section = BTreeMap::new();
    section.insert("command".to_string(), Value::str(&spec.command_line));
    section.insert("inputs".to_string(), exprs_fp(&spec.inputs, ctx, det)?);
    Ok(digest(&Value::Map(section)))
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
    let Action::Process { command_line, inputs, env, worker, .. } = action else {
        return Err("action_fingerprint is only defined for process actions".to_string());
    };
    let mut payload: BTreeMap<String, Value> = BTreeMap::new();
    payload.insert("command".to_string(), Value::str(command_line));
    payload.insert("inputs".to_string(), exprs_fp(inputs, ctx, det)?);
    if !env.is_empty() {
        payload.insert("env".to_string(), exprs_fp(env, ctx, det)?);
    }
    if let Some(spec) = worker {
        // The worker is part of the toolchain (its jar/interpreter determine
        // the output), so its identity joins the fingerprint with the same
        // atom_fp rules: a JDK upgrade or a rebuilt worker jar invalidates
        // everything this worker built, exactly like a compiler change.
        let mut section = BTreeMap::new();
        section.insert("command".to_string(), Value::str(&spec.command_line));
        section.insert("inputs".to_string(), exprs_fp(&spec.inputs, ctx, det)?);
        payload.insert("worker".to_string(), Value::Map(section));
    }
    // Discovered (scanned) dependencies — inputs the tool reported reading
    // last build that the graph didn't declare (C/C++ headers from a
    // depfile). Content-hashed like any input, so editing one re-stales the
    // action. Absent on the first build and for actions with no depfile, so
    // it never perturbs an existing fingerprint. Empty lists are falsy in
    // Python (`if deps:`), so an empty scan adds no key either.
    if let Some(meta) = &det.metadata {
        let deps = crate::keys::action_key(action)
            .and_then(|key| meta.discovered_deps(&key))
            .unwrap_or_default();
        if !deps.is_empty() {
            let mut map = BTreeMap::new();
            for path in deps {
                let hashed = det.hash_file(&ctx.read_path(&path));
                map.insert(path, hash_value(hashed));
            }
            payload.insert("deps".to_string(), Value::Map(map));
        }
    }
    Ok(digest(&Value::Map(payload)))
}
