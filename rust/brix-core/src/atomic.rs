//! The atomic layer: either the whole build lands, or nothing changes.
//! Mirrors `src/brix/atomic.py`.
//!
//! Staging layout, under the workspace:
//!
//! ```text
//! build/staging/add/<logical path>   outputs written by this build
//! build/staging/del/<logical path>   deletion markers
//! build/staging/COMMIT               commit began (recovery journal)
//! build/staging/HELD                 successful build held for review
//! ```
//!
//! `on_build_begin` points the build context's `output_root`/`deletions_root`
//! at the staging tree, so every write lands there and every read sees the
//! workspace as it will look after commit (the overlay in `NodeContext`).
//! Deletion actions become staged markers instead of immediate removals.
//!
//! `on_build_end` commits only if the whole build succeeded: the COMMIT
//! marker is written first, then staged files move into place and marked
//! deletions are applied, then staging is removed. A crash mid-commit leaves
//! the marker, and the next build's `on_build_begin` finishes the commit
//! (copying is idempotent). A failed or interrupted build leaves staging in
//! place, inspectable, and it is discarded when the next build starts.
//!
//! **Held staging** (review before commit): with `hold=true` a successful
//! build writes the HELD marker instead of committing — the workspace stays
//! untouched until an explicit `commit_staging` (or `discard_staging`). Held
//! staging is *kept* at the next build begin and built on top of: the
//! overlay already reads it as if committed, so further builds compose; a
//! plain successful build commits everything, held work included. The HELD
//! marker is *consumed* when a new build starts on top: the new build's own
//! outcome decides what staging becomes (re-held, committed, or failed-build
//! debris) — a failed build on top of held staging leaves a mixed tree that
//! must never commit, so it is discarded at the next build like any debris.

use std::path::{Path, PathBuf};

use crate::actions::Action;
use crate::context::NodeContext;
use crate::executor::BuildResult;
use crate::fsutil::walk_files;
use crate::layers::BuildLayer;
use crate::plan::{ExecutionPlan, Node};
use crate::process::ProcessResult;

pub const COMMIT_MARKER: &str = "COMMIT";
pub const HELD_MARKER: &str = "HELD";

fn sorted_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = walk_files(dir);
    files.sort();
    files
}

fn staged_counts(staging: &Path) -> (usize, usize) {
    (sorted_files(&staging.join("add")).len(), sorted_files(&staging.join("del")).len())
}

fn touch(path: &Path) -> Result<(), String> {
    std::fs::write(path, "").map_err(|e| format!("cannot write {}: {e}", path.display()))
}

/// The journaled commit: copy `add/`, apply `del/`, remove staging.
/// Idempotent — safe to rerun after a crash mid-copy. Mirrors `_apply_commit`.
fn apply_commit(workspace: &Path, staging: &Path) -> Result<(), String> {
    let add_dir = staging.join("add");
    let del_dir = staging.join("del");
    for staged in sorted_files(&add_dir) {
        let rel = staged.strip_prefix(&add_dir).expect("staged file under add/");
        let target = workspace.join(rel);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
        }
        std::fs::copy(&staged, &target)
            .map_err(|e| format!("cannot commit {}: {e}", target.display()))?;
    }
    for marker in sorted_files(&del_dir) {
        let rel = marker.strip_prefix(&del_dir).expect("marker under del/");
        match std::fs::remove_file(workspace.join(rel)) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {} // missing_ok
            Err(e) => return Err(format!("cannot apply deletion of {}: {e}", rel.display())),
        }
    }
    std::fs::remove_dir_all(staging).map_err(|e| format!("cannot clear staging: {e}"))
}

/// Commit held (or interrupted) staging. `Ok(None)` if nothing is staged;
/// `Ok(Some((files, deletions)))` applied otherwise. `Err` on failed-build
/// debris — only a *successful* build's staging may reach the workspace.
/// Mirrors `commit_staging`.
pub fn commit_staging(workspace: &Path, staging: &Path) -> Result<Option<(usize, usize)>, String> {
    if !staging.exists() {
        return Ok(None);
    }
    let held = staging.join(HELD_MARKER).exists();
    let interrupted = staging.join(COMMIT_MARKER).exists();
    if !held && !interrupted {
        return Err(
            "staged outputs are from a failed build; `build` to redo them or `discard`"
                .to_string(),
        );
    }
    let counts = staged_counts(staging);
    touch(&staging.join(COMMIT_MARKER))?;
    apply_commit(workspace, staging)?;
    Ok(Some(counts))
}

/// Throw staging away (held or failed-build debris). `Ok(None)` if nothing
/// is staged; the number of staged files discarded otherwise. `Err` on an
/// interrupted commit — that build already committed and must finish.
/// Mirrors `discard_staging`.
pub fn discard_staging(staging: &Path) -> Result<Option<usize>, String> {
    if !staging.exists() {
        return Ok(None);
    }
    if staging.join(COMMIT_MARKER).exists() {
        return Err(
            "an interrupted commit is pending; it finishes when the next build starts"
                .to_string(),
        );
    }
    let (files, _) = staged_counts(staging);
    std::fs::remove_dir_all(staging).map_err(|e| format!("cannot discard staging: {e}"))?;
    Ok(Some(files))
}

/// Either the whole build lands, or nothing on disk changes. Sits outermost,
/// so process outputs and cache-restored outputs all pass through staging.
pub struct AtomicLayer {
    pub staging_dir: PathBuf,
    pub hold: bool,
}

impl AtomicLayer {
    pub fn new(staging_dir: impl Into<PathBuf>, hold: bool) -> Self {
        AtomicLayer { staging_dir: staging_dir.into(), hold }
    }

    fn staging(&self, ctx: &NodeContext) -> PathBuf {
        ctx.workspace_root.join(&self.staging_dir)
    }
}

impl BuildLayer for AtomicLayer {
    fn on_build_begin(&self, _plan: &ExecutionPlan, ctx: &mut NodeContext) {
        // Python raises on any IO error here and the build dies loudly; a
        // silent failure would break the atomicity promise, so panic too.
        let staging = self.staging(ctx);
        if staging.join(COMMIT_MARKER).exists() {
            // Crashed mid-commit last time; copying is idempotent — finish it.
            apply_commit(&ctx.workspace_root, &staging).expect("cannot recover commit");
        } else if staging.exists() && !staging.join(HELD_MARKER).exists() {
            // Leftovers of a failed/interrupted build; a new build supersedes.
            std::fs::remove_dir_all(&staging).expect("cannot discard stale staging");
        }
        // Held staging survives and is built on top of: the overlay reads it
        // as if committed, so this build composes with the held one. Consume
        // the marker — from here the tree mixes held and new outputs, and
        // only this build's success may re-mark or commit it.
        let _ = std::fs::remove_file(staging.join(HELD_MARKER)); // missing_ok
        std::fs::create_dir_all(staging.join("add")).expect("cannot create staging");
        std::fs::create_dir_all(staging.join("del")).expect("cannot create staging");
        // Build-scoped redirection: all writes stage, all reads overlay.
        ctx.output_root = staging.join("add");
        ctx.deletions_root = Some(staging.join("del"));
    }

    fn around_node(
        &self,
        node: &Node,
        ctx: &NodeContext,
        inner: &crate::layers::NodeRunner,
    ) -> ProcessResult {
        let Action::Delete { paths } = &node.action else {
            return inner(ctx);
        };
        // Deleting is output too: stage markers, apply at commit.
        let label = node.action.label();
        let deletions_root =
            ctx.deletions_root.as_ref().expect("atomic layer set deletions_root");
        let mut stdout = String::new();
        for rel in paths {
            let marker = deletions_root.join(rel);
            if let Some(parent) = marker.parent() {
                std::fs::create_dir_all(parent).expect("cannot create deletion marker dir");
            }
            touch(&marker).expect("cannot stage deletion marker");
            stdout.push_str(&format!("staged deletion of {rel}\n"));
        }
        ProcessResult {
            action_name: label.clone(),
            command: label,
            argv: Vec::new(),
            exit_code: Some(0),
            signal: None,
            stdout,
            stderr: String::new(),
            source: "process".to_string(),
        }
    }

    fn on_build_end(&self, result: &BuildResult, ctx: &NodeContext) {
        if !result.success {
            return; // leave staging inspectable; workspace untouched
        }
        let staging = self.staging(ctx);
        if self.hold {
            touch(&staging.join(HELD_MARKER)).expect("cannot mark held staging");
            return; // held for review; commit_staging applies it later
        }
        touch(&staging.join(COMMIT_MARKER)).expect("cannot journal commit");
        apply_commit(&ctx.workspace_root, &staging).expect("cannot commit staging");
    }
}
