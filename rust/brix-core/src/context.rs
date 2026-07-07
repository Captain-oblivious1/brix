//! `NodeContext`: the overlay through which an action reads its inputs.
//! Mirrors `NodeContext` in `src/brix/layers.py`.
//!
//! Declared paths on actions are *logical* names; the context maps them to
//! physical places. With no layers both roots are the workspace. The atomic
//! layer points `output_root` at its staging tree and `deletions_root` at its
//! deletion markers, so reads see the workspace *as it will look after commit*:
//! staged outputs shadow workspace files and marked deletions hide them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::fsutil::walk_files;
use crate::patterns::PathPattern;

#[derive(Debug, Clone)]
pub struct NodeContext {
    pub workspace_root: PathBuf,
    pub output_root: PathBuf,
    pub deletions_root: Option<PathBuf>,
}

impl NodeContext {
    /// The no-layers context: both roots are the workspace, no deletions.
    pub fn for_root(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        Self {
            workspace_root: root.clone(),
            output_root: root,
            deletions_root: None,
        }
    }

    fn read_roots(&self) -> Vec<&Path> {
        if self.output_root == self.workspace_root {
            vec![&self.workspace_root]
        } else {
            vec![&self.output_root, &self.workspace_root]
        }
    }

    /// Deleted this build (marker present) and not re-created since.
    fn hidden(&self, logical: &str) -> bool {
        match &self.deletions_root {
            Some(dr) => dr.join(logical).exists() && !self.output_root.join(logical).exists(),
            None => false,
        }
    }

    /// The physical location a logical path currently reads from.
    pub fn read_path(&self, logical: &str) -> PathBuf {
        if self.hidden(logical) {
            return self.output_root.join(logical); // resolves to a missing file
        }
        for root in self.read_roots() {
            let candidate = root.join(logical);
            if candidate.exists() {
                return candidate;
            }
        }
        self.workspace_root.join(logical)
    }

    /// Realize a `PathPattern` across the overlay: `(logical, physical,
    /// bindings)` sorted by logical, staged copies shadowing workspace copies,
    /// marked deletions excluded.
    pub fn find(&self, pattern: &PathPattern) -> Vec<(String, PathBuf, Vec<(String, String)>)> {
        let mut merged: BTreeMap<String, (PathBuf, Vec<(String, String)>)> = BTreeMap::new();
        for root in self.read_roots() {
            for (logical, bindings) in pattern.find(root) {
                if !merged.contains_key(&logical) && !self.hidden(&logical) {
                    merged.insert(logical.clone(), (root.join(&logical), bindings));
                }
            }
        }
        merged.into_iter().map(|(k, (p, b))| (k, p, b)).collect()
    }

    /// Every `(logical, physical)` file visible through the overlay, sorted by
    /// logical, excluding brix's own storage (staging tree, `.brix`-prefixed
    /// dirs).
    pub fn iter_files(&self) -> Vec<(String, PathBuf)> {
        let mut exclude: Vec<PathBuf> = Vec::new();
        if self.output_root != self.workspace_root {
            if let Some(parent) = self.output_root.parent() {
                exclude.push(parent.to_path_buf()); // the staging tree
            }
        }
        let mut merged: BTreeMap<String, PathBuf> = BTreeMap::new();
        for root in self.read_roots() {
            for path in walk_files(root) {
                // ".brix"-prefixed components are brix's own storage.
                if path
                    .components()
                    .any(|c| c.as_os_str().to_string_lossy().starts_with(".brix"))
                {
                    continue;
                }
                if exclude.iter().any(|x| path.starts_with(x)) {
                    continue;
                }
                let logical = match path.strip_prefix(root) {
                    Ok(rel) => rel.to_string_lossy().replace('\\', "/"),
                    Err(_) => continue,
                };
                if !merged.contains_key(&logical) && !self.hidden(&logical) {
                    merged.insert(logical, path);
                }
            }
        }
        merged.into_iter().collect()
    }
}
