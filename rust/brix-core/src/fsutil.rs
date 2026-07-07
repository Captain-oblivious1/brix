//! Small filesystem helpers shared by `PathPattern::find` and
//! `NodeContext::iter_files`. Mirrors the effect of Python's `Path.rglob("*")`
//! filtered to regular files.

use std::path::{Path, PathBuf};

/// Every regular file under `dir`, recursively (the file half of
/// `dir.rglob("*")`). Order is unspecified — every caller sorts — so the walk
/// order never reaches a golden value. Unreadable directories are skipped
/// rather than erroring, matching `rglob`'s best-effort traversal.
///
/// Symlinked *directories* are not descended into (avoids cycles; `rglob`'s
/// default is likewise not to follow them), but a symlink pointing at a file is
/// collected — `path.is_file()` follows the link, like Python's `is_file()`.
pub(crate) fn walk_files(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let entries = match std::fs::read_dir(&d) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let ft = match entry.file_type() {
                Ok(ft) => ft,
                Err(_) => continue,
            };
            if ft.is_dir() {
                stack.push(path);
            } else if ft.is_file() || (ft.is_symlink() && path.is_file()) {
                out.push(path);
            }
        }
    }
    out
}
