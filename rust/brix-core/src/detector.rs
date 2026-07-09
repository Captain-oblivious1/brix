//! The default change detector, mirroring `TimeAndHashChangeDetector` in
//! `src/brix/metadata.py`.
//!
//! With a metadata handle attached (engine use), the `(mtime_ns, size) ->
//! sha256` mapping persists across builds so an unchanged file costs one
//! `stat` — a *performance* cache that never changes the returned value.
//! Without one (golden fixtures), hashing is direct; the byte-for-byte hash
//! is what makes cross-machine cache keys agree.

use std::path::Path;
use std::sync::Arc;

use sha2::{Digest, Sha256};

use crate::metadata::BuildMetadata;

/// Content hashing with a cheap `stat`.
#[derive(Default, Clone)]
pub struct TimeAndHashChangeDetector {
    /// The persistent hash cache (also read by `action_fingerprint` for
    /// discovered deps). `None` = stateless: hash directly every time.
    pub metadata: Option<Arc<BuildMetadata>>,
    /// Skin-registered identity plugins (the `content_identity` extension
    /// trait); consulted only for names the built-in registry doesn't know.
    pub custom_identities: Option<Arc<dyn crate::identity::CustomIdentities>>,
}

impl TimeAndHashChangeDetector {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_metadata(metadata: Arc<BuildMetadata>) -> Self {
        Self { metadata: Some(metadata), custom_identities: None }
    }

    pub fn with_custom_identities(
        mut self,
        custom: Option<Arc<dyn crate::identity::CustomIdentities>>,
    ) -> Self {
        self.custom_identities = custom;
        self
    }

    /// The versioned fingerprint tag for an identity name — built-ins first,
    /// then the skin's resolver; None means the name is unknown everywhere
    /// (a loud error at the caller).
    pub fn identity_tag(&self, name: &str) -> Option<String> {
        crate::identity::tag(name)
            .map(str::to_string)
            .or_else(|| self.custom_identities.as_ref().and_then(|c| c.tag(name)))
    }

    fn compute_identity(&self, name: &str, path: &Path) -> Result<String, String> {
        if crate::identity::tag(name).is_some() {
            crate::identity::digest(name, path)
        } else {
            self.custom_identities
                .as_ref()
                .expect("caller gates on identity_tag")
                .digest(name, path)
        }
    }

    /// `[mtime_ns, size]`, or `None` if the path can't be stat'd. This carries
    /// an mtime, so it is machine-specific and never golden-pinned — it feeds
    /// tool-atom identity and the hash cache's validation, where a
    /// disagreement can miss but never make a stale hit.
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
    /// `None`). Mirrors `hash_file`, cache included.
    pub fn hash_file(&self, path: &Path) -> Option<String> {
        if !path.is_file() {
            return None;
        }
        // Python returns None when the stat fails (deleted between checks).
        let sig = self.stat_signature(path)?;
        if let Some(meta) = &self.metadata {
            let key = path.to_string_lossy();
            if let Some(cached) = meta.cached_hash(&key, &sig) {
                return Some(cached);
            }
            let digest = hash_bytes(path)?;
            meta.store_hash(&key, &sig, &digest);
            return Some(digest);
        }
        hash_bytes(path)
    }

    /// The content-identity digest of a file, cached like `hash_file` and
    /// validated by the same stat signature (mirrors `identity_digest`).
    /// `Ok(None)` if missing or not a regular file; `Err` propagates the
    /// plugin's error on a file it cannot interpret — the caller decides the
    /// fallback (`:raw`). `name` must resolve via [`Self::identity_tag`]
    /// (built-in or skin-registered); custom digests cache under their
    /// versioned tag exactly like built-ins.
    pub fn identity_digest(&self, path: &Path, name: &str) -> Result<Option<String>, String> {
        if !path.is_file() {
            return Ok(None);
        }
        let Some(sig) = self.stat_signature(path) else {
            return Ok(None);
        };
        let tag = self.identity_tag(name).expect("caller gates on identity_tag");
        if let Some(meta) = &self.metadata {
            let key = path.to_string_lossy();
            if let Some(cached) = meta.cached_identity(&key, &sig, &tag) {
                return Ok(Some(cached));
            }
            let digest = self.compute_identity(name, path)?; // caller falls back to :raw
            meta.store_identity(&key, &sig, &tag, &digest);
            return Ok(Some(digest));
        }
        self.compute_identity(name, path).map(Some)
    }
}

fn hash_bytes(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    let mut hex = String::with_capacity(64);
    for b in hasher.finalize() {
        hex.push_str(&format!("{:02x}", b));
    }
    Some(hex)
}
