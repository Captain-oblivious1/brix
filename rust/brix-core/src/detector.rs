//! The default change detector, mirroring `TimeAndHashChangeDetector` in
//! `src/brix/metadata.py`.
//!
//! The Python version memoizes `(mtime, size) -> hash` in the persistent
//! metadata so an unchanged file costs one `stat` instead of a re-hash. That
//! cache is a *performance* optimization that never changes the returned value,
//! so this port computes the sha256 directly — the byte-for-byte hash is what
//! makes cross-machine cache keys agree, and it is what the golden fixtures pin.

use std::path::Path;

use sha2::{Digest, Sha256};

/// Content hashing with a cheap `stat`. Stateless here (no metadata cache):
/// `hash_file` always reflects the file's current bytes.
#[derive(Debug, Default, Clone)]
pub struct TimeAndHashChangeDetector;

impl TimeAndHashChangeDetector {
    pub fn new() -> Self {
        Self
    }

    /// `[mtime_ns, size]`, or `None` if the path can't be stat'd. This carries
    /// an mtime, so it is machine-specific and never golden-pinned — it feeds
    /// only tool-atom identity, where a disagreement can miss but never make a
    /// stale hit.
    pub fn stat_signature(&self, path: &Path) -> Option<Vec<i64>> {
        let md = std::fs::metadata(path).ok()?;
        let mtime_ns = md
            .modified()
            .ok()?
            .duration_since(std::time::UNIX_EPOCH)
            .ok()?
            .as_nanos() as i64;
        Some(vec![mtime_ns, md.len() as i64])
    }

    /// sha256 hex of the file's bytes; `None` for a missing path or a
    /// non-regular file (a directory declared as an input/output hashes as
    /// `None`). Mirrors `hash_file`.
    pub fn hash_file(&self, path: &Path) -> Option<String> {
        if !path.is_file() {
            return None;
        }
        let bytes = std::fs::read(path).ok()?;
        let mut hasher = Sha256::new();
        hasher.update(&bytes);
        let mut hex = String::with_capacity(64);
        for b in hasher.finalize() {
            hex.push_str(&format!("{:02x}", b));
        }
        Some(hex)
    }
}
