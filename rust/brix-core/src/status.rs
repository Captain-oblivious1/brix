//! Build-state inspection without building. Mirrors `src/brix/status.py`
//! (`compute_status`) and the CLI's `_clean` (`src/brix/cli.py`).
//!
//! `compute_status` evaluates the graph against persisted metadata and the
//! workspace as it is *right now*: which actions are fresh, which are stale
//! and why, which failed last time, and which stale outputs are pending
//! deletion because their source vanished. Nothing executes; this is exactly
//! the freshness decision the incremental layer would make at node-ready
//! time, asked hypothetically. Staleness propagates: an action whose
//! fingerprint currently matches is still `outdated` if something upstream
//! will rebuild. Rendering and exit codes stay in the skin — this returns
//! data (docs/rust-port.md: "status and clean call the core").

use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use serde::Serialize;

use crate::actions::Action;
use crate::atomic::{COMMIT_MARKER, HELD_MARKER};
use crate::content::action_fingerprint;
use crate::context::NodeContext;
use crate::data::DataAtom;
use crate::detector::TimeAndHashChangeDetector;
use crate::executor::collection_members;
use crate::incremental::outputs_intact;
use crate::keys::{action_key, membership_key};
use crate::metadata::BuildMetadata;
use crate::patterns::PathPattern;
use crate::plan::NodeKind;
use crate::planner::plan_graph;
use crate::process::ProcessResult;

pub const FRESH: &str = "fresh";
pub const NEVER_BUILT: &str = "never built";
pub const INPUTS_CHANGED: &str = "inputs changed";
pub const OUTPUTS_MISSING: &str = "outputs missing or modified";
pub const FAILED: &str = "failed last build";
pub const OUTDATED: &str = "outdated";

#[derive(Debug, Serialize)]
pub struct ActionStatus {
    pub label: String,
    pub state: String,
    pub detail: String,
    pub last_result: Option<ProcessResult>,
}

#[derive(Debug, Serialize)]
pub struct PendingDeletion {
    pub expansion: String,
    pub member: String,
    pub paths: Vec<String>,
}

#[derive(Debug, Default, Serialize)]
pub struct StatusReport {
    pub actions: Vec<ActionStatus>,
    pub pending_deletions: Vec<PendingDeletion>,
    pub staged_files: Vec<String>,
    pub staged_deletions: Vec<String>,
    pub interrupted_commit: bool,
    pub held: bool,
}

fn action_status(
    action: &Action,
    ctx: &NodeContext,
    det: &TimeAndHashChangeDetector,
    metadata: &BuildMetadata,
) -> Result<ActionStatus, String> {
    let label = action.label();
    let key = action_key(action).unwrap_or_default();
    let Some(record) = metadata.action_record(&key) else {
        return Ok(ActionStatus {
            label,
            state: NEVER_BUILT.to_string(),
            detail: String::new(),
            last_result: None,
        });
    };
    // Lenient like the incremental layer: a malformed stored result degrades
    // to "no result", never to a crash.
    let last_result: Option<ProcessResult> = record
        .get("result")
        .filter(|v| !v.is_null())
        .and_then(|v| serde_json::from_value(v.clone()).ok());
    let status = |state: &str, detail: String, last: Option<ProcessResult>| ActionStatus {
        label: label.clone(),
        state: state.to_string(),
        detail,
        last_result: last,
    };
    if !record.get("ok").and_then(|v| v.as_bool()).unwrap_or(true) {
        let detail = match &last_result {
            // Python: summary().removeprefix("FAILED ").strip()
            Some(r) => r.summary().trim_start_matches("FAILED ").trim().to_string(),
            None => String::new(),
        };
        return Ok(status(FAILED, detail, last_result));
    }
    let fingerprint = action_fingerprint(action, ctx, det)?;
    if record.get("fingerprint").and_then(|v| v.as_str()) != Some(&fingerprint) {
        return Ok(status(INPUTS_CHANGED, String::new(), last_result));
    }
    if !outputs_intact(&record, ctx, det) {
        return Ok(status(OUTPUTS_MISSING, String::new(), last_result));
    }
    Ok(status(FRESH, String::new(), last_result))
}

/// Concrete declared output paths of a substituted (vanished) subtree's
/// process action (mirrors the `FileData ... is_concrete` filter).
fn concrete_outputs(action: &Action) -> Vec<String> {
    let Action::Process { outputs, .. } = action else {
        return Vec::new();
    };
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

fn sorted_files_under(dir: &Path) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(current) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&current) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.is_file() {
                if let Ok(rel) = path.strip_prefix(dir) {
                    out.push(rel.to_string_lossy().replace('\\', "/"));
                }
            }
        }
    }
    out.sort();
    out
}

pub fn compute_status(
    actions: Vec<Action>,
    root: &Path,
    metadata_dir: &str,
    staging_dir: &str,
    targets: Option<&[String]>,
) -> Result<StatusReport, String> {
    let root_str = root.to_string_lossy().to_string();
    let plan = plan_graph(actions, &root_str, targets)?;
    let metadata = BuildMetadata::with_directory(root, metadata_dir);
    metadata.load();
    let det = TimeAndHashChangeDetector::with_metadata(std::sync::Arc::new(metadata));
    // The detector owns the metadata handle now; keep a reader for records.
    let metadata = det.metadata.as_ref().unwrap().clone();
    let mut ctx = NodeContext::for_root(root);

    let mut report = StatusReport::default();
    let staging = root.join(staging_dir);
    if staging.exists() {
        let add_dir = staging.join("add");
        let del_dir = staging.join("del");
        if add_dir.exists() {
            report.staged_files = sorted_files_under(&add_dir);
        }
        if del_dir.exists() {
            report.staged_deletions = sorted_files_under(&del_dir);
        }
        report.interrupted_commit = staging.join(COMMIT_MARKER).exists();
        report.held = staging.join(HELD_MARKER).exists();
        if report.held {
            // Held staging is kept and built on top of, so freshness is
            // asked the way the next build will: through the overlay.
            ctx.output_root = add_dir;
            ctx.deletions_root = Some(del_dir);
        }
    }

    let mut node_entries: HashMap<usize, Vec<usize>> = HashMap::new();
    let mut node_dirty: HashMap<usize, bool> = HashMap::new();

    // Realize an expansion point as of right now, recursing into nested
    // expansion points; returns (action indices, dirty). Mirrors `walk_expand`.
    fn walk_expand(
        expand: &Action,
        label: &str,
        ctx: &NodeContext,
        det: &TimeAndHashChangeDetector,
        metadata: &BuildMetadata,
        report: &mut StatusReport,
    ) -> Result<(Vec<usize>, bool), String> {
        let Action::Expand { element, child, .. } = expand else {
            unreachable!("walk_expand is only called for expand actions");
        };
        let mut indices = Vec::new();
        let mut dirty = false;
        let members = collection_members(expand, ctx)?;
        let previous = membership_key(expand)
            .and_then(|key| metadata.membership(&key))
            .unwrap_or_default();
        let current: BTreeSet<&String> = members.iter().collect();
        let vanished: BTreeSet<&String> =
            previous.iter().filter(|m| !current.contains(m)).collect();
        for member in vanished {
            let gone = child.substitute(&[(element.clone(), member.clone())]);
            report.pending_deletions.push(PendingDeletion {
                expansion: label.to_string(),
                member: member.clone(),
                paths: concrete_outputs(&gone),
            });
            dirty = true;
        }
        for member in &members {
            let concrete = child.substitute(&[(element.clone(), member.clone())]);
            if matches!(concrete, Action::Expand { .. }) {
                let (sub_indices, sub_dirty) =
                    walk_expand(&concrete, &concrete.label(), ctx, det, metadata, report)?;
                indices.extend(sub_indices);
                dirty = dirty || sub_dirty;
            } else {
                let status = action_status(&concrete, ctx, det, metadata)?;
                dirty = dirty || status.state != FRESH;
                indices.push(report.actions.len());
                report.actions.push(status);
            }
        }
        Ok((indices, dirty))
    }

    for node in &plan.nodes {
        let (indices, dirty) = if node.kind == NodeKind::Expand {
            walk_expand(&node.action, &node.label, &ctx, &det, &metadata, &mut report)?
        } else {
            let status = action_status(&node.action, &ctx, &det, &metadata)?;
            let dirty = status.state != FRESH;
            let index = report.actions.len();
            report.actions.push(status);
            (vec![index], dirty)
        };
        node_entries.insert(node.id, indices);
        node_dirty.insert(node.id, dirty);
    }

    // Propagate: anything downstream of dirty work is outdated even if its
    // own fingerprint currently matches — its inputs are about to change.
    let mut changed = true;
    while changed {
        changed = false;
        for edge in &plan.edges {
            if node_dirty.get(&edge.from_id).copied().unwrap_or(false)
                && !node_dirty.get(&edge.to_id).copied().unwrap_or(false)
            {
                node_dirty.insert(edge.to_id, true);
                changed = true;
                for &i in node_entries.get(&edge.to_id).map(Vec::as_slice).unwrap_or(&[]) {
                    if report.actions[i].state == FRESH {
                        report.actions[i].state = OUTDATED.to_string();
                        report.actions[i].detail = "upstream will rebuild".to_string();
                    }
                }
            }
        }
    }

    Ok(report)
}

/// Delete declared outputs (concrete and currently realized), every output
/// recorded in metadata, and the metadata store itself. Returns the number
/// of files removed. Mirrors the CLI's `_clean`.
pub fn clean(actions: Vec<Action>, root: &Path, metadata_dir: &str) -> Result<usize, String> {
    let root_str = root.to_string_lossy().to_string();
    let plan = plan_graph(actions, &root_str, None)?;
    let metadata = BuildMetadata::with_directory(root, metadata_dir);
    metadata.load();

    let mut paths: BTreeSet<String> = BTreeSet::new();
    for node in &plan.nodes {
        // One level only, like Python: a nested expand child has no
        // `outputs`, so its concrete outputs come from metadata records.
        let action = match (&node.kind, &node.action) {
            (NodeKind::Expand, Action::Expand { child, .. }) => child.as_ref(),
            _ => &node.action,
        };
        let Action::Process { outputs, .. } = action else {
            continue;
        };
        for (_, expr) in outputs {
            for atom in expr {
                match atom {
                    DataAtom::File { path, .. } => {
                        if PathPattern::new(path).map(|p| p.is_concrete()).unwrap_or(false) {
                            paths.insert(path.clone());
                        }
                    }
                    DataAtom::Glob { template, .. } => {
                        if let Ok(p) = PathPattern::new(template) {
                            for (rel, _) in p.find(root) {
                                paths.insert(rel);
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    for record in metadata.action_records().values() {
        if let Some(outputs) = record.get("outputs").and_then(|o| o.as_object()) {
            paths.extend(outputs.keys().cloned());
        }
    }

    let mut removed = 0;
    for rel in &paths {
        let target = root.join(rel);
        if target.is_file() {
            std::fs::remove_file(&target)
                .map_err(|e| format!("cannot remove {}: {e}", target.display()))?;
            removed += 1;
        }
    }
    if metadata.directory.exists() {
        std::fs::remove_dir_all(&metadata.directory)
            .map_err(|e| format!("cannot remove metadata dir: {e}"))?;
    }
    Ok(removed)
}

/// The report as the wire JSON of docs/boundary-schema.md (snake_case keys;
/// `ProcessResult` in its usual shape).
pub fn report_to_json(report: &StatusReport) -> String {
    serde_json::to_string(report).expect("report serializes")
}
