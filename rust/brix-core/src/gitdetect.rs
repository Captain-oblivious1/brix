//! Git-accelerated change detection, mirroring `src/brix/gitdetect.py`.
//!
//! For a **tracked** file git reports unchanged since the last build, the
//! detector trusts the stored hash without stat-ing it. Everything git can't
//! vouch for — untracked/ignored files (the build's own intermediates),
//! out-of-repo inputs — falls through to the ordinary stat/hash path. It is a
//! pure performance layer: it never changes a fingerprint. Its one correctness
//! obligation is the vouch, and it degrades to the full scan whenever git is
//! unavailable, there is no baseline yet, or any git call fails.
//!
//! "Since last build", not "since last commit": the changed set unions the
//! working-tree diff with `git diff <baseline>..HEAD`, so commits between
//! builds are caught and the user never has to commit. The baseline is the HEAD
//! sha recorded at the previous build, **paired with that build's working-tree
//! dirty set** — a file dirty then was hashed with content git never committed,
//! so if it is reverted to HEAD before this build git reports it clean while
//! the cached hash is stale; such files are never vouched.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;

use crate::metadata::BuildMetadata;

/// Run a git command under `root`; `None` on any failure (missing git, not a
/// repo, nonzero exit) so every caller degrades to the stat scan.
fn git(root: &Path, args: &[&str]) -> Option<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8(output.stdout).ok()
}

fn repo_root(root: &Path) -> Option<PathBuf> {
    git(root, &["rev-parse", "--show-toplevel"]).map(|s| PathBuf::from(s.trim()))
}

fn head(root: &Path) -> Option<String> {
    git(root, &["rev-parse", "HEAD"]).map(|s| s.trim().to_string())
}

/// Repo-relative POSIX paths of every tracked file (`git ls-files`).
fn tracked(repo_root: &Path) -> Option<HashSet<String>> {
    let out = git(repo_root, &["ls-files", "-z"])?;
    Some(out.split('\0').filter(|p| !p.is_empty()).map(str::to_string).collect())
}

/// Repo-relative paths dirty in the working tree now (unstaged + staged +
/// untracked), from `git status --porcelain`.
fn worktree_dirty(repo_root: &Path) -> Option<HashSet<String>> {
    let status = git(
        repo_root,
        &["status", "--porcelain", "-z", "--untracked-files=all"],
    )?;
    let mut dirty = HashSet::new();
    // Porcelain -z: each entry is "XY <path>", NUL-separated; a rename/copy adds
    // a second NUL-separated path (the origin). Parse defensively — any path
    // mentioned counts, so both sides of a rename are changed.
    let tokens: Vec<&str> = status.split('\0').filter(|t| !t.is_empty()).collect();
    let mut i = 0;
    while i < tokens.len() {
        let entry = tokens[i];
        let code = &entry[..entry.len().min(2)];
        if entry.len() > 3 {
            dirty.insert(entry[3..].to_string());
        }
        if code.starts_with('R') || code.starts_with('C') {
            i += 1; // the following token is the rename/copy source path
            if i < tokens.len() {
                dirty.insert(tokens[i].to_string());
            }
        }
        i += 1;
    }
    Some(dirty)
}

/// Repo-relative paths touched by commits in `baseline..HEAD`.
fn committed_since(repo_root: &Path, baseline: &str) -> Option<HashSet<String>> {
    let out = git(
        repo_root,
        &["diff", "--name-only", "-z", &format!("{baseline}..HEAD")],
    )?;
    Some(out.split('\0').filter(|p| !p.is_empty()).map(str::to_string).collect())
}

/// The prepared vouch for one build: which tracked files git guarantees are
/// unchanged since the recorded baseline. Attached to the detector, which
/// consults it before stat-ing.
pub struct GitVouch {
    repo_root: PathBuf,
    tracked: HashSet<String>,
    changed: HashSet<String>,
    previous_dirty: HashSet<String>,
}

impl GitVouch {
    /// True iff git guarantees this file's content equals what the last build
    /// hashed: tracked, not changed since the baseline, and not dirty *at* the
    /// last build (a then-dirty file was hashed with content git never saw —
    /// reverting it makes git report clean while the cached hash is stale).
    pub fn vouch_unchanged(&self, path: &Path) -> bool {
        let Ok(abs) = std::fs::canonicalize(path) else {
            // A path that can't be canonicalized isn't a present tracked file.
            return false;
        };
        let Ok(rel) = abs.strip_prefix(&self.repo_root) else {
            return false; // outside the repo -> the ordinary path owns it
        };
        let rel = rel.to_string_lossy().replace('\\', "/");
        self.tracked.contains(&rel)
            && !self.changed.contains(&rel)
            && !self.previous_dirty.contains(&rel)
    }
}

/// Prepare git vouching for this build against `root`, recording the new
/// (HEAD, dirty-set) baseline pair into `metadata` for the next build. Returns
/// `Some` only when a prior baseline pair exists and every git call succeeds;
/// otherwise `None` (vouch nothing this build — the safe stat scan) while still
/// setting the baseline so the *next* build can accelerate. Mirrors
/// `GitChangeDetector._prepare`.
pub fn prepare(root: &Path, metadata: &Arc<BuildMetadata>) -> Option<GitVouch> {
    let repo_root = repo_root(root)?; // not a git repo (or no git) -> no vouch
    // Canonicalize so it matches `canonicalize(path)` in the vouch: temp dirs
    // and worktrees can be reached through symlinks git resolves and we don't.
    let repo_root = std::fs::canonicalize(&repo_root).unwrap_or(repo_root);
    let baseline = metadata.git_baseline();
    let previous_dirty = metadata.git_dirty();
    let tracked = tracked(&repo_root);
    let dirty_now = worktree_dirty(&repo_root);
    let head = head(&repo_root);

    // Record this build's (HEAD, dirty set) as the next build's baseline pair;
    // they must be recorded together (the dirty set is what makes a later
    // "clean vs HEAD" answer trustworthy). If either half is unknowable, clear
    // both so the next build vouches nothing.
    match (&head, &dirty_now) {
        (Some(sha), Some(dirty)) => {
            metadata.set_git_baseline(Some(sha));
            metadata.set_git_dirty(Some(dirty.iter().cloned().collect()));
        }
        _ => {
            metadata.set_git_baseline(None);
            metadata.set_git_dirty(None);
            return None;
        }
    }
    let dirty_now = dirty_now.unwrap();

    // No baseline/dirty pair yet (first git build, or pre-dirty-set metadata) or
    // ls-files failed: vouch nothing, but the pair is now set for next time.
    let (baseline, previous_dirty, tracked) = match (baseline, previous_dirty, tracked) {
        (Some(b), Some(pd), Some(t)) => (b, pd, t),
        _ => return None,
    };
    let committed = committed_since(&repo_root, &baseline)?; // unresolvable -> full scan

    let mut changed = dirty_now;
    changed.extend(committed);
    Some(GitVouch {
        repo_root,
        tracked,
        changed,
        previous_dirty: previous_dirty.into_iter().collect(),
    })
}
