//! The cache layer: reuse results across workspaces, keyed by fingerprint.
//! Mirrors `src/brix/cache.py`.
//!
//! Incremental answers "did *this workspace* already do it?"; cache answers
//! "did *anyone* already do it?". The key is the same `action_fingerprint`
//! the incremental layer records, so a hit is exactly as trustworthy as a
//! replay. The layer sits innermost: a node the incremental layer proved
//! fresh never reaches the cache, and a cache hit writes its restored
//! outputs through `ctx.output_root`, so with the atomic layer outermost
//! they route through staging like any process output. Restored results
//! carry `source="cache"`. Failures with a real exit code are cached and
//! replayed too (same inputs, same failure); signal-killed or never-started
//! runs are not — those aren't the action's deterministic answer.
//!
//! The store is pluggable (`CacheStoreProtocol`); `CacheStore` is the
//! built-in local one, sharing its on-disk layout with Python — the same
//! `<fp[:2]>/<fp>/entry.json` + `files/<n>` blobs, so the two engines can
//! share one cache directory. Robustness against an unreachable store is
//! the *layer's* job, not each store's: any store error degrades to a cache
//! miss (or a skipped populate), never a failed build, so a leaf store may
//! let its I/O errors propagate as `Err`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::SystemTime;

use serde_json::{json, Map, Value};

use crate::actions::Action;
use crate::content::action_fingerprint;
use crate::context::NodeContext;
use crate::data::DataAtom;
use crate::detector::TimeAndHashChangeDetector;
use crate::layers::BuildLayer;
use crate::patterns::PathPattern;
use crate::plan::Node;
use crate::process::ProcessResult;

const ENTRY: &str = "entry.json";
const FILES: &str = "files";

/// A cache hit refreshes its entry's recency (mtime) so eviction is LRU by
/// *use*, not by creation. Coarsened to this many seconds so repeated hits in
/// one window don't each rewrite the inode. Mirrors `_TOUCH_COARSENING_SECONDS`
/// in `cache.py`; pruning itself lives in Python (engine-agnostic directory
/// maintenance over the shared dir), so this side only keeps the mtimes fresh.
const TOUCH_COARSENING_SECONDS: u64 = 3600;

/// An entry is an opaque JSON value a store returns from `load` and receives
/// back in `restore`; the layer only reads `entry["result"]` from it.
pub type Entry = Value;

/// What `CacheLayer` requires of a store (mirrors `CacheStoreProtocol`).
///
/// `restore` copies the entry's cached outputs to `ctx.output_root` and
/// returns whether it fully succeeded. A store may return `Err` for its I/O
/// failures — the layer treats any `Err` as a miss / skipped populate.
pub trait CacheStoreProtocol: Sync {
    fn load(&self, fingerprint: &str) -> Result<Option<Entry>, String>;
    fn restore(&self, fingerprint: &str, entry: &Entry, ctx: &NodeContext)
        -> Result<bool, String>;
    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String>;
}

/// The action's produced output files as `{logical path: physical path}`
/// (sorted — Python sorts at store time; a BTreeMap keeps them sorted from
/// the start). Directory outputs hold no content of their own and are
/// skipped; the files inside them are declared separately. Mirrors
/// `_output_files`.
fn output_files(action: &Action, ctx: &NodeContext) -> BTreeMap<String, PathBuf> {
    let Action::Process { outputs, .. } = action else {
        return BTreeMap::new();
    };
    let mut files = BTreeMap::new();
    for (_, expr) in outputs {
        for atom in expr {
            match atom {
                DataAtom::File { path, .. } => {
                    let Ok(pattern) = PathPattern::new(path) else { continue };
                    if pattern.is_concrete() {
                        let physical = ctx.read_path(path);
                        if physical.is_file() {
                            files.insert(path.clone(), physical);
                        }
                    } else {
                        for (logical, physical, _) in ctx.find(&pattern) {
                            if physical.is_file() {
                                files.insert(logical, physical);
                            }
                        }
                    }
                }
                DataAtom::Glob { template, exclude, .. } => {
                    let Ok(pattern) = PathPattern::with_exclude(template, exclude) else {
                        continue;
                    };
                    for (logical, physical, _) in ctx.find(&pattern) {
                        if physical.is_file() {
                            files.insert(logical, physical);
                        }
                    }
                }
                _ => {}
            }
        }
    }
    files
}

/// Content-addressed result store under one directory (mirrors `CacheStore`;
/// same on-disk layout, safe for concurrent builds sharing the directory):
///
/// ```text
/// <cache_dir>/<fp[:2]>/<fp>/entry.json   result + logical output paths
/// <cache_dir>/<fp[:2]>/<fp>/files/<n>    output content blobs
/// ```
///
/// Entries are assembled in a temp directory and renamed into place; a lost
/// race just discards the duplicate.
pub struct CacheStore {
    pub directory: PathBuf,
}

impl CacheStore {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        CacheStore { directory: directory.into() }
    }

    fn entry_dir(&self, fingerprint: &str) -> PathBuf {
        self.directory.join(&fingerprint[..2]).join(fingerprint)
    }

    /// Refresh an entry's recency after a hit (best-effort, coarsened). Keeps
    /// the mtime Python's `prune` sorts on current for entries served by this
    /// engine; a stat/utime failure never fails the hit.
    fn touch(&self, fingerprint: &str) {
        let entry = self.entry_dir(fingerprint).join(ENTRY);
        let now = SystemTime::now();
        if let Ok(mtime) = std::fs::metadata(&entry).and_then(|m| m.modified()) {
            if now.duration_since(mtime).map(|d| d.as_secs()).unwrap_or(u64::MAX)
                < TOUCH_COARSENING_SECONDS
            {
                return;
            }
        }
        if let Ok(file) = std::fs::OpenOptions::new().write(true).open(&entry) {
            let _ = file.set_modified(now);
        }
    }
}

impl CacheStoreProtocol for CacheStore {
    /// A missing or unparsable entry is a plain miss (`Ok(None)`), like
    /// Python's `except (OSError, ValueError)`.
    fn load(&self, fingerprint: &str) -> Result<Option<Entry>, String> {
        let text = match std::fs::read_to_string(self.entry_dir(fingerprint).join(ENTRY)) {
            Ok(text) => text,
            Err(_) => return Ok(None),
        };
        Ok(serde_json::from_str(&text).ok())
    }

    /// Copy the entry's outputs to `ctx.output_root`. `Ok(false)` (and no
    /// copies at all) if the entry is corrupt — the caller falls back to
    /// running the process, which overwrites anything partial.
    fn restore(
        &self,
        fingerprint: &str,
        entry: &Entry,
        ctx: &NodeContext,
    ) -> Result<bool, String> {
        let files_dir = self.entry_dir(fingerprint).join(FILES);
        let outputs = match entry.get("outputs") {
            None => {
                self.touch(fingerprint); // a hit with no output files still counts as used
                return Ok(true);
            }
            Some(Value::Object(map)) => map,
            Some(_) => return Err("corrupt cache entry: outputs not a map".to_string()),
        };
        let mut copies: Vec<(PathBuf, PathBuf)> = Vec::new();
        for (logical, blob) in outputs {
            let Some(blob) = blob.as_str() else {
                return Err("corrupt cache entry: blob name not a string".to_string());
            };
            let source = files_dir.join(blob);
            if !source.is_file() {
                return Ok(false);
            }
            copies.push((source, ctx.output_root.join(logical)));
        }
        for (source, dest) in copies {
            if let Some(parent) = dest.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|e| format!("cannot create output dir: {e}"))?;
            }
            // fs::copy carries permission bits (Python uses copy2 so linked
            // binaries keep their mode).
            std::fs::copy(&source, &dest)
                .map_err(|e| format!("cannot restore {}: {e}", dest.display()))?;
        }
        self.touch(fingerprint);
        Ok(true)
    }

    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String> {
        let final_dir = self.entry_dir(fingerprint);
        if final_dir.exists() {
            return Ok(());
        }
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let tmp = self.directory.join(format!(
            ".tmp-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let files_dir = tmp.join(FILES);
        std::fs::create_dir_all(&files_dir).map_err(|e| format!("cannot create tmp: {e}"))?;
        let mut names: Map<String, Value> = Map::new();
        for (index, (logical, physical)) in outputs.iter().enumerate() {
            names.insert(logical.clone(), Value::String(index.to_string()));
            std::fs::copy(physical, files_dir.join(index.to_string()))
                .map_err(|e| format!("cannot copy {}: {e}", physical.display()))?;
        }
        let entry = json!({
            "result": serde_json::to_value(result).expect("result serializes"),
            "outputs": Value::Object(names),
        });
        std::fs::write(tmp.join(ENTRY), serde_json::to_string(&entry).expect("entry serializes"))
            .map_err(|e| format!("cannot write entry: {e}"))?;
        if let Some(parent) = final_dir.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("cannot create entry dir: {e}"))?;
        }
        if std::fs::rename(&tmp, &final_dir).is_err() {
            let _ = std::fs::remove_dir_all(&tmp); // another build won the race
        }
        Ok(())
    }
}

/// Forward to a shared store — lets one store instance back several
/// builds or layers (`Box::new(arc.clone())` per build).
impl<S: CacheStoreProtocol + Send> CacheStoreProtocol for std::sync::Arc<S> {
    fn load(&self, fingerprint: &str) -> Result<Option<Entry>, String> {
        (**self).load(fingerprint)
    }
    fn restore(
        &self,
        fingerprint: &str,
        entry: &Entry,
        ctx: &NodeContext,
    ) -> Result<bool, String> {
        (**self).restore(fingerprint, entry, ctx)
    }
    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String> {
        (**self).store(fingerprint, result, outputs)
    }
}

/// A fast local store in front of a slower shared one (mirrors
/// `TwoTierCacheStore`). A read checks the `front` first; a `back` hit is
/// served and then *promoted* into the front, so the next build on this
/// machine hits locally. A write populates the front and best-effort the
/// back. Both tiers must share the entry shape used here: `entry["result"]`
/// and `entry["outputs"]` (its keys are the logical output paths); promotion
/// never touches a tier's internal blob layout — it re-stores from the files
/// `restore` just wrote.
pub struct TwoTierCacheStore {
    pub front: Box<dyn CacheStoreProtocol>,
    pub back: Box<dyn CacheStoreProtocol>,
}

impl TwoTierCacheStore {
    pub fn new(front: Box<dyn CacheStoreProtocol>, back: Box<dyn CacheStoreProtocol>) -> Self {
        TwoTierCacheStore { front, back }
    }

    /// Copy a back-tier hit into the front, using the files `restore` just
    /// wrote to the output root. Best-effort: a promotion failure must not
    /// turn a valid back hit into a miss.
    fn promote(&self, fingerprint: &str, entry: &Entry, ctx: &NodeContext) {
        let Some(result) = entry
            .get("result")
            .and_then(|r| serde_json::from_value::<ProcessResult>(r.clone()).ok())
        else {
            return;
        };
        let outputs: BTreeMap<String, PathBuf> = entry
            .get("outputs")
            .and_then(|o| o.as_object())
            .map(|map| {
                map.keys().map(|logical| (logical.clone(), ctx.output_root.join(logical))).collect()
            })
            .unwrap_or_default();
        let _ = self.front.store(fingerprint, &result, &outputs);
    }
}

impl CacheStoreProtocol for TwoTierCacheStore {
    fn load(&self, fingerprint: &str) -> Result<Option<Entry>, String> {
        if let Some(mut entry) = self.front.load(fingerprint)? {
            if let Some(map) = entry.as_object_mut() {
                map.insert("_tier".to_string(), json!("front"));
            }
            return Ok(Some(entry));
        }
        let mut entry = self.back.load(fingerprint)?; // may Err; the layer degrades
        if let Some(map) = entry.as_mut().and_then(|e| e.as_object_mut()) {
            map.insert("_tier".to_string(), json!("back"));
        }
        Ok(entry)
    }

    fn restore(
        &self,
        fingerprint: &str,
        entry: &Entry,
        ctx: &NodeContext,
    ) -> Result<bool, String> {
        let is_back = entry.get("_tier").and_then(|v| v.as_str()) == Some("back");
        let tier = if is_back { &self.back } else { &self.front };
        if !tier.restore(fingerprint, entry, ctx)? {
            return Ok(false);
        }
        if is_back {
            self.promote(fingerprint, entry, ctx);
        }
        Ok(true)
    }

    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String> {
        self.front.store(fingerprint, result, outputs)?;
        // A down back must never stop local caching.
        let _ = self.back.store(fingerprint, result, outputs);
        Ok(())
    }
}

/// Answer stale nodes from the store; populate it after real runs.
///
/// All three store operations are guarded: an `Err` from `load`, `restore`,
/// or `store` — an unreachable remote, a permission error, a malformed
/// entry — degrades to a cache miss or a skipped populate, never a failed
/// build. This is why a leaf store may stay dumb and let its I/O errors
/// propagate; the resilience lives here.
pub struct CacheLayer {
    pub store: Box<dyn CacheStoreProtocol>,
    /// Share the incremental layer's detector when stacking (clone it — the
    /// hash memo travels in its `Arc`), so no file is hashed twice per build
    /// and the deps fold reads the same metadata.
    pub detector: TimeAndHashChangeDetector,
}

impl CacheLayer {
    pub fn new(store: Box<dyn CacheStoreProtocol>, detector: TimeAndHashChangeDetector) -> Self {
        CacheLayer { store, detector }
    }

    /// The built-in local store under `cache_dir`.
    pub fn with_dir(cache_dir: impl Into<PathBuf>, detector: TimeAndHashChangeDetector) -> Self {
        CacheLayer::new(Box::new(CacheStore::new(cache_dir)), detector)
    }

    /// A cache hit as a ready ProcessResult, or `None` to run the work. Any
    /// store error (unreachable, corrupt entry) is a miss; a malformed
    /// stored result is too (Python raises constructing `ProcessResult` and
    /// the layer's guard turns it into a miss — same outcome).
    fn try_hit(&self, fingerprint: &str, ctx: &NodeContext) -> Option<ProcessResult> {
        let entry = self.store.load(fingerprint).ok()??;
        if !self.store.restore(fingerprint, &entry, ctx).ok()? {
            return None;
        }
        let mut restored: ProcessResult =
            serde_json::from_value(entry.get("result")?.clone()).ok()?;
        restored.source = "cache".to_string();
        Some(restored)
    }
}

impl BuildLayer for CacheLayer {
    fn around_node(
        &self,
        node: &Node,
        ctx: &NodeContext,
        inner: &crate::layers::NodeRunner,
    ) -> ProcessResult {
        let action = &node.action;
        if !matches!(action, Action::Process { .. }) {
            return inner(ctx);
        }
        let fingerprint = match action_fingerprint(action, ctx, &self.detector) {
            Ok(fp) => fp,
            // Same deliberate divergence as the incremental layer: Python
            // raises (and the build dies); the node fails instead. In the
            // standard stack the incremental layer catches this first.
            Err(exc) => {
                return ProcessResult::did_not_run(&node.label, "", format!("brix: {exc}"))
            }
        };
        if let Some(hit) = self.try_hit(&fingerprint, ctx) {
            return hit;
        }
        let result = inner(ctx);
        let cacheable =
            result.source == "process" && result.signal.is_none() && result.exit_code.is_some();
        if cacheable {
            let outputs =
                if result.ok() { output_files(action, ctx) } else { BTreeMap::new() };
            // Best-effort populate; a store failure never fails the build.
            let _ = self.store.store(&fingerprint, &result, &outputs);
        }
        result
    }
}

/// Route a workspace-relative cache dir under the root (absolute passes
/// through) — mirrors the `CacheBuilder` path handling.
pub fn resolve_cache_dir(root: &Path, cache_dir: &str) -> PathBuf {
    let dir = PathBuf::from(cache_dir);
    if dir.is_absolute() {
        dir
    } else {
        root.join(dir)
    }
}

#[cfg(test)]
mod touch_tests {
    //! The default (Rust) engine must refresh an entry's mtime on a cache hit,
    //! because Python's `prune` sorts on that mtime to pick eviction victims —
    //! a hit that didn't touch would let a hot entry be evicted as "old".

    use super::*;
    use crate::context::NodeContext;
    use std::time::{Duration, SystemTime};

    fn tmpdir(tag: &str) -> PathBuf {
        static N: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "brix-cache-touch-{}-{}-{}",
            std::process::id(),
            tag,
            N.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Hand-build an entry on disk (restore never reads "result") and return
    /// its dir. `fp` must be a valid sharding key (>= 2 chars).
    fn seed(store: &CacheStore, fp: &str, payload: &[u8]) -> PathBuf {
        let entry_dir = store.entry_dir(fp);
        std::fs::create_dir_all(entry_dir.join(FILES)).unwrap();
        std::fs::write(entry_dir.join(FILES).join("0"), payload).unwrap();
        std::fs::write(entry_dir.join(ENTRY), r#"{"outputs":{"out.txt":"0"}}"#).unwrap();
        entry_dir
    }

    fn backdate(entry: &Path, secs: u64) {
        let when = SystemTime::now() - Duration::from_secs(secs);
        let f = std::fs::OpenOptions::new().write(true).open(entry.join(ENTRY)).unwrap();
        f.set_modified(when).unwrap();
    }

    fn mtime(entry: &Path) -> SystemTime {
        std::fs::metadata(entry.join(ENTRY)).unwrap().modified().unwrap()
    }

    #[test]
    fn hit_refreshes_recency_and_restores_bytes() {
        let store = CacheStore::new(tmpdir("cache").join("c"));
        let fp = format!("ab{}", "c".repeat(62));
        let entry = seed(&store, &fp, b"payload");
        backdate(&entry, TOUCH_COARSENING_SECONDS + 10_000);
        let before = mtime(&entry);

        let out = tmpdir("out");
        let value: Value = serde_json::from_str(r#"{"outputs":{"out.txt":"0"}}"#).unwrap();
        assert!(store.restore(&fp, &value, &NodeContext::for_root(out.clone())).unwrap());

        assert!(mtime(&entry) > before, "a hit must refresh recency");
        assert_eq!(std::fs::read(out.join("out.txt")).unwrap(), b"payload");
    }

    #[test]
    fn hit_within_coarsening_window_does_not_churn() {
        let store = CacheStore::new(tmpdir("cache").join("c"));
        let fp = format!("de{}", "f".repeat(62));
        let entry = seed(&store, &fp, b"p");
        let before = mtime(&entry); // just written, well inside the window

        let out = tmpdir("out");
        let value: Value = serde_json::from_str(r#"{"outputs":{"out.txt":"0"}}"#).unwrap();
        store.restore(&fp, &value, &NodeContext::for_root(out)).unwrap();

        assert_eq!(mtime(&entry), before, "a fresh entry is not re-touched");
    }
}
