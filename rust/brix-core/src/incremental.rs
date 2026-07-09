//! The incremental layer: complete fresh nodes with their previous results.
//! Mirrors `IncrementalLayer` and friends in `src/brix/incremental.py`.
//!
//! A node is fresh when its fingerprint — command template, tool identities,
//! and the content hashes of every input — matches the record from the last
//! successful run, and its recorded outputs are still intact on disk. Fresh
//! nodes are completed with their previous ProcessResult (stdout/stderr
//! replay included) without running a process.
//!
//! The fingerprint is `content::action_fingerprint` — deliberately a
//! standalone function: the cache layer uses the same computation as its key.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;

use serde_json::{json, Map, Value};

use crate::actions::Action;
use crate::content::action_fingerprint;
use crate::context::NodeContext;
use crate::data::DataAtom;
use crate::detector::TimeAndHashChangeDetector;
use crate::executor::BuildResult;
use crate::keys::{action_key, membership_key};
use crate::layers::{BuildLayer, NodeProposal};
use crate::metadata::BuildMetadata;
use crate::patterns::PathPattern;
use crate::plan::Node;
use crate::process::ProcessResult;

/// Parse a Makefile-format dependency file (as emitted by `cc -MMD -MF`)
/// into the list of prerequisite paths. `target: prereq1 prereq2 \` with line
/// continuations and backslash-escaped spaces. Missing/unreadable -> `[]`.
///
/// Only relative paths are kept: absolute prerequisites are system /
/// toolchain files, already covered by the tool's own fingerprint, and would
/// not resolve through the workspace overlay. Mirrors `scan_depfile`.
pub fn scan_depfile(path: &Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let text = text.replace("\\\n", " "); // join line continuations
    let Some((_, rhs)) = text.split_once(':') else {
        return Vec::new(); // no "target:" head
    };
    rhs.replace("\\ ", "\0")
        .split_whitespace()
        .map(|token| token.replace('\0', " "))
        .filter(|dep| !dep.is_empty() && !dep.starts_with('/'))
        .collect()
}

/// Do the recorded outputs still exist with their recorded content?
/// Reads through the overlay: an output produced earlier in this build lives
/// in staging; one committed by a previous build lives in the workspace.
/// Either satisfies freshness. Mirrors `outputs_intact`.
pub(crate) fn outputs_intact(
    record: &Value,
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
) -> bool {
    let Some(outputs) = record.get("outputs").and_then(|o| o.as_object()) else {
        return true; // absent key: Python's .get("outputs", {}) — nothing to check
    };
    for (path, stored) in outputs {
        let current = det.hash_file(&ctx.read_path(path));
        let matches = match (stored, &current) {
            (Value::String(s), Some(c)) => s == c,
            (Value::Null, None) => true,
            _ => false,
        };
        if !matches {
            return false;
        }
    }
    true
}

/// Hash every concrete declared output (files by path, globs realized
/// through the overlay). Mirrors `output_hashes`.
fn output_hashes(action: &Action, ctx: &NodeContext, det: &TimeAndHashChangeDetector) -> Value {
    let Action::Process { outputs, .. } = action else {
        return json!({});
    };
    let mut hashes: Map<String, Value> = Map::new();
    for (_, expr) in outputs {
        for atom in expr {
            match atom {
                DataAtom::File { path, .. } => {
                    if let Ok(p) = PathPattern::new(path) {
                        if p.is_concrete() {
                            let h = det.hash_file(&ctx.read_path(path));
                            hashes.insert(path.clone(), opt_str(h));
                        }
                    }
                }
                DataAtom::Glob { template, .. } => {
                    if let Ok(p) = PathPattern::new(template) {
                        for (logical, physical, _) in ctx.find(&p) {
                            let h = det.hash_file(&physical);
                            hashes.insert(logical, opt_str(h));
                        }
                    }
                }
                _ => {}
            }
        }
    }
    Value::Object(hashes)
}

fn opt_str(v: Option<String>) -> Value {
    match v {
        Some(s) => Value::String(s),
        None => Value::Null,
    }
}

/// Remove declared outputs after a failure (a clean build would not have
/// produced them; neither may we). Directory outputs are left in place.
/// Mirrors `remove_outputs`.
fn remove_outputs(action: &Action, ctx: &NodeContext) {
    let Action::Process { outputs, .. } = action else {
        return;
    };
    for (_, expr) in outputs {
        for atom in expr {
            match atom {
                DataAtom::File { path, .. } => {
                    if let Ok(p) = PathPattern::new(path) {
                        if p.is_concrete() {
                            let target = ctx.output_root.join(path);
                            if target.is_file() {
                                let _ = std::fs::remove_file(target);
                            }
                        }
                    }
                }
                DataAtom::Glob { template, .. } => {
                    if let Ok(p) = PathPattern::new(template) {
                        for (rel, _) in p.find(&ctx.output_root) {
                            let _ = std::fs::remove_file(ctx.output_root.join(rel));
                        }
                    }
                }
                _ => {}
            }
        }
    }
}

/// Skip fresh nodes; record fingerprints and results after real runs.
pub struct IncrementalLayer {
    pub metadata: std::sync::Arc<BuildMetadata>,
    pub detector: TimeAndHashChangeDetector,
    /// Current-build membership, applied to metadata only if the build
    /// succeeds: a failed build must re-detect deletions next time.
    pending_memberships: Mutex<HashMap<String, Vec<String>>>,
}

impl IncrementalLayer {
    pub fn new(metadata: std::sync::Arc<BuildMetadata>) -> Self {
        let detector = TimeAndHashChangeDetector::with_metadata(metadata.clone());
        IncrementalLayer { metadata, detector, pending_memberships: Mutex::new(HashMap::new()) }
    }

    /// Concrete stale outputs under a vanished expansion member, forgetting
    /// its fingerprint records along the way.
    ///
    /// Recurses through *nested* expansions: their inner collections never
    /// realize this build (the whole subtree is pruned when the outer member
    /// disappears), so their membership is read back from metadata — the
    /// inner `membership_key` is deterministic from the substituted
    /// collection, so it matches what the previous build recorded for this
    /// member. Reads, but never *mutates*, membership records (a failed
    /// build must re-detect this deletion next time, and that re-detection
    /// walks back in here and needs the inner lists intact). Mirrors
    /// `_reap_vanished`.
    fn reap_vanished(&self, action: &Action) -> Vec<String> {
        match action {
            Action::Process { outputs, .. } => {
                if let Some(key) = action_key(action) {
                    self.metadata.forget_action(&key);
                }
                outputs
                    .iter()
                    .flat_map(|(_, expr)| expr.iter())
                    .filter_map(|atom| match atom {
                        DataAtom::File { path, .. }
                            if PathPattern::new(path).map(|p| p.is_concrete()).unwrap_or(false) =>
                        {
                            Some(path.clone())
                        }
                        _ => None,
                    })
                    .collect()
            }
            Action::Expand { element, child, .. } => {
                let mut stale: Vec<String> = Vec::new();
                let members = membership_key(action)
                    .and_then(|key| self.metadata.membership(&key))
                    .unwrap_or_default();
                for member in members {
                    let concrete = child.substitute(&[(element.clone(), member)]);
                    stale.extend(self.reap_vanished(&concrete));
                }
                stale
            }
            Action::Delete { .. } => Vec::new(), // nothing to reap
        }
    }
}

impl BuildLayer for IncrementalLayer {
    fn around_node(
        &self,
        node: &Node,
        ctx: &NodeContext,
        inner: &crate::layers::NodeRunner,
    ) -> ProcessResult {
        let action = &node.action;
        let Action::Process { depfile, .. } = action else {
            return inner(ctx); // Delete nodes pass through un-recorded
        };
        let key = action_key(action).expect("process actions always have a key");
        let mut fingerprint = match action_fingerprint(action, ctx, &self.detector) {
            Ok(fp) => fp,
            // Python raises here (unknown identity plugin etc.) and the build
            // dies with a traceback; failing the node is the closest
            // non-crashing behavior. Deliberate divergence.
            Err(exc) => {
                return ProcessResult::did_not_run(&node.label, "", format!("brix: {exc}"))
            }
        };
        if let Some(record) = self.metadata.action_record(&key) {
            let ok = record.get("ok").and_then(|v| v.as_bool()).unwrap_or(true);
            let same = record.get("fingerprint").and_then(|v| v.as_str()) == Some(&fingerprint);
            if ok && same && outputs_intact(&record, ctx, &self.detector) {
                if let Some(stored) = record.get("result") {
                    if let Ok(mut replayed) =
                        serde_json::from_value::<ProcessResult>(stored.clone())
                    {
                        replayed.source = "replayed".to_string();
                        return replayed;
                    }
                    // A malformed stored result degrades to a rerun (Python
                    // would crash constructing ProcessResult(**...)).
                }
            }
        }
        let result = inner(ctx);
        if result.ok() {
            if let Some(depfile) = depfile {
                // Fold the tool's reported inputs (e.g. C/C++ headers) into
                // the fingerprint, then recompute it so this same build's
                // record is already dep-aware — the next unchanged build is
                // fresh, with no spurious extra rebuild. Runs after a cache
                // hit too (the depfile is a restored output), so a cache-fed
                // machine also learns them.
                self.metadata
                    .set_discovered_deps(&key, scan_depfile(&ctx.read_path(depfile)));
                if let Ok(fp) = action_fingerprint(action, ctx, &self.detector) {
                    fingerprint = fp;
                }
            }
            self.metadata.record_action(
                &key,
                json!({
                    "ok": true,
                    "fingerprint": fingerprint,
                    "result": serde_json::to_value(&result).expect("result serializes"),
                    "outputs": output_hashes(action, ctx, &self.detector),
                }),
            );
        } else {
            // A clean build would not have produced these outputs; neither
            // may we. The failure itself is recorded — never as fresh (a
            // failed action always reruns), but status/errors can report it.
            remove_outputs(action, ctx);
            self.metadata.record_action(
                &key,
                json!({
                    "ok": false,
                    "fingerprint": fingerprint,
                    "result": serde_json::to_value(&result).expect("result serializes"),
                    "outputs": {},
                }),
            );
        }
        result
    }

    fn on_collection_realized(
        &self,
        node: &Node,
        members: &[String],
        _ctx: &NodeContext,
    ) -> Vec<NodeProposal> {
        let action = &node.action;
        let Action::Expand { element, child, .. } = action else {
            return Vec::new();
        };
        let Some(key) = membership_key(action) else {
            return Vec::new();
        };
        let previous = self.metadata.membership(&key);
        self.pending_memberships
            .lock()
            .unwrap()
            .insert(key, members.to_vec());
        let Some(previous) = previous else {
            return Vec::new();
        };
        let mut removed: Vec<String> = previous
            .iter()
            .filter(|m| !members.contains(m))
            .cloned()
            .collect();
        removed.sort();
        let mut proposals = Vec::new();
        for member in removed {
            let vanished = child.substitute(&[(element.clone(), member)]);
            let stale_outputs = self.reap_vanished(&vanished);
            if !stale_outputs.is_empty() {
                proposals.push(NodeProposal {
                    action: Action::Delete { paths: stale_outputs },
                    label: None,
                });
            }
        }
        proposals
    }

    fn on_build_end(&self, result: &BuildResult, _ctx: &NodeContext) {
        if result.success {
            let pending = std::mem::take(&mut *self.pending_memberships.lock().unwrap());
            for (key, members) in pending {
                self.metadata.set_membership(&key, members);
            }
        } else {
            self.pending_memberships.lock().unwrap().clear();
        }
        // Python's save() raises on an IO error and the build dies loudly;
        // a silent failure here would quietly disable incrementality.
        self.metadata.save().expect("cannot save build metadata");
    }
}
