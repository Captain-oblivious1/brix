//! The cache layer's behavioral contract, mirrored from `tests/test_cache.py`
//! and `tests/test_cache_store.py` — either engine must satisfy these. The
//! build stack is the real one: cache innermost, incremental around it, both
//! over the real scheduler. `runs.log` counts real tool runs; a `Spawns`
//! reporter counts real process spawns — the thing caching is meant to avoid.
//!
//! Includes the interop pins: the local store's on-disk layout is shared
//! with Python (`<fp[:2]>/<fp>/entry.json` + `files/<n>`), verified in both
//! directions — Rust restores from a hand-written Python-format entry, and a
//! Rust-written entry has the exact shape Python's `json.dumps` produces.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use brix_core::actions::Action;
use brix_core::cache::{CacheLayer, CacheStore, CacheStoreProtocol, Entry, TwoTierCacheStore};
use brix_core::content::action_fingerprint;
use brix_core::context::NodeContext;
use brix_core::detector::TimeAndHashChangeDetector;
use brix_core::executor::{execute, BuildResult};
use brix_core::incremental::IncrementalLayer;
use brix_core::layers::BuildLayer;
use brix_core::metadata::BuildMetadata;
use brix_core::plan::{ExecutionPlan, Node};
use brix_core::process::ProcessResult;
use brix_core::report::Reporter;

// -- helpers (shared shape with tests/incremental.rs) --------------------------

fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-cache-{name}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write(root: &Path, rel: &str, content: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, content).unwrap();
}

const UPPER_SH: &str = "echo \"upper:$1\" >> runs.log\n\
                        echo \"uppercased $1\"\n\
                        tr a-z A-Z < \"$1\" > \"$2\"\n";

const CONCAT_SH: &str = "echo combine >> runs.log\n\
                         out=\"$1\"\n\
                         shift\n\
                         cat \"$@\" > \"$out\"\n";

fn write_workspace(root: &Path, sources: &[(&str, &str)]) {
    for (name, text) in sources {
        write(root, &format!("src/{name}.txt"), text);
    }
    write(root, "tools/upper.sh", UPPER_SH);
    write(root, "tools/concat.sh", CONCAT_SH);
}

fn upper_combine_spec() -> String {
    r#"{
      "version": 1,
      "actions": [
        {
          "kind": "expand",
          "element": "name",
          "collection": [{"kind": "glob", "template": "src/{name}.txt"}],
          "child": {
            "kind": "process",
            "name": "upper {name}",
            "command_line": "{shell} {tool} {source} {dest}",
            "inputs": {
              "shell": [{"kind": "tool", "name": "sh"}],
              "tool": [{"kind": "file", "path": "tools/upper.sh"}],
              "source": [{"kind": "file", "path": "src/{name}.txt"}]
            },
            "outputs": {"dest": [{"kind": "file", "path": "build/upper/{name}.txt"}]}
          }
        },
        {
          "kind": "process",
          "name": "combine",
          "command_line": "{shell} {tool} {combined} {parts}",
          "inputs": {
            "shell": [{"kind": "tool", "name": "sh"}],
            "tool": [{"kind": "file", "path": "tools/concat.sh"}],
            "parts": [{"kind": "glob", "template": "build/upper/{name}.txt"}]
          },
          "outputs": {"combined": [{"kind": "file", "path": "build/all.txt"}]}
        }
      ]
    }"#
    .to_string()
}

/// Counts real process spawns — the thing caching is meant to avoid.
#[derive(Default)]
struct Spawns {
    labels: Mutex<Vec<String>>,
}

impl Spawns {
    fn labels(&self) -> Vec<String> {
        self.labels.lock().unwrap().clone()
    }
}

impl Reporter for Spawns {
    fn process_started(&self, node: &Node) {
        self.labels.lock().unwrap().push(node.label.clone());
    }
}

/// One build with the real stack: cache innermost, incremental around it,
/// the cache layer sharing the incremental layer's detector (hash memo and
/// deps fold travel in its Arc) — exactly the Python `CacheBuilder(
/// IncrementalBuilder(...))` wiring.
fn build_with_store(
    root: &Path,
    store: Box<dyn CacheStoreProtocol>,
    spec_json: &str,
    reporter: &dyn Reporter,
) -> (BuildResult, ExecutionPlan) {
    let spec = brix_core::spec::parse(spec_json).unwrap();
    let mut plan =
        brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap();
    let metadata = Arc::new(BuildMetadata::new(root));
    metadata.load();
    let incremental = IncrementalLayer::new(metadata);
    let cache = CacheLayer::new(store, incremental.detector.clone());
    let result = execute(&mut plan, Some(2), &[&cache, &incremental], reporter);
    (result, plan)
}

fn build_cached(
    root: &Path,
    cache_dir: &Path,
    spec_json: &str,
    reporter: &dyn Reporter,
) -> (BuildResult, ExecutionPlan) {
    build_with_store(root, Box::new(CacheStore::new(cache_dir)), spec_json, reporter)
}

fn runs(root: &Path) -> HashMap<String, usize> {
    let mut counts = HashMap::new();
    if let Ok(text) = std::fs::read_to_string(root.join("runs.log")) {
        for line in text.lines() {
            *counts.entry(line.to_string()).or_insert(0) += 1;
        }
    }
    counts
}

fn read(root: &Path, rel: &str) -> String {
    std::fs::read_to_string(root.join(rel)).unwrap()
}

// -- the classic payoffs --------------------------------------------------------

#[test]
fn fresh_checkout_restores_from_cache() {
    let base = scratch("fresh-checkout");
    let cache = base.join("shared-cache");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write_workspace(root, &[("a", "x"), ("b", "y")]);
    }

    let spawns = Spawns::default();
    assert!(build_cached(&first, &cache, &upper_combine_spec(), &spawns).0.success);
    assert!(!runs(&first).is_empty()); // real processes populated the cache

    let spawns = Spawns::default();
    let (result, _) = build_cached(&second, &cache, &upper_combine_spec(), &spawns);
    assert!(result.success, "errors: {:?}", result.errors);
    assert!(spawns.labels().is_empty()); // nothing ran
    assert!(runs(&second).is_empty());
    assert_eq!(read(&second, "build/all.txt"), "XY");
    assert!(result.process_results.iter().all(|r| r.source == "cache"));
}

#[test]
fn clean_rebuild_restores_from_cache() {
    // The classic payoff: rm -rf outputs + metadata, rebuild instantly.
    let root = scratch("clean-rebuild");
    let cache = root.join("build/.brix-cache");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    assert!(build_cached(&root, &cache, &upper_combine_spec(), &Spawns::default()).0.success);
    let ran_before = runs(&root);

    std::fs::remove_dir_all(root.join("build/upper")).unwrap();
    std::fs::remove_file(root.join("build/all.txt")).unwrap();
    std::fs::remove_dir_all(root.join("build/.brix")).unwrap();

    let (result, _) = build_cached(&root, &cache, &upper_combine_spec(), &Spawns::default());
    assert!(result.success);
    assert_eq!(runs(&root), ran_before); // no new processes
    assert_eq!(read(&root, "build/all.txt"), "XY");
    assert!(result.process_results.iter().all(|r| r.source == "cache"));
}

#[test]
fn incremental_freshness_wins_over_cache() {
    let root = scratch("freshness-wins");
    let cache = root.join("cache");
    write_workspace(&root, &[("a", "x")]);
    assert!(build_cached(&root, &cache, &upper_combine_spec(), &Spawns::default()).0.success);
    let (second, _) = build_cached(&root, &cache, &upper_combine_spec(), &Spawns::default());
    assert!(second.success);
    // Fresh nodes replay from local metadata; the cache is never consulted.
    assert!(second.process_results.iter().all(|r| r.source == "replayed"));
}

#[test]
fn changed_input_misses_then_repopulates() {
    let base = scratch("changed-input");
    let cache = base.join("cache");
    let root = base.join("ws");
    write_workspace(&root, &[("a", "x")]);
    assert!(build_cached(&root, &cache, &upper_combine_spec(), &Spawns::default()).0.success);

    write(&root, "src/a.txt", "z");
    let spawns = Spawns::default();
    assert!(build_cached(&root, &cache, &upper_combine_spec(), &spawns).0.success);
    assert!(!spawns.labels().is_empty()); // miss: the changed content really ran
    assert_eq!(read(&root, "build/all.txt"), "Z");

    // A fresh checkout with the *new* content now hits the new entry.
    let other = base.join("other");
    write_workspace(&other, &[("a", "z")]);
    let spawns = Spawns::default();
    let (result, _) = build_cached(&other, &cache, &upper_combine_spec(), &spawns);
    assert!(result.success && spawns.labels().is_empty());
    assert_eq!(read(&other, "build/all.txt"), "Z");
}

#[test]
fn cached_failure_replays_without_running() {
    let base = scratch("cached-failure");
    let cache = base.join("cache");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write(root, "tools/boom.sh", "echo kaboom >&2\nexit 7\n");
    }
    let spec = r#"{
      "version": 1,
      "actions": [{
        "kind": "process",
        "name": "boom",
        "command_line": "{shell} {tool}",
        "inputs": {
          "shell": [{"kind": "tool", "name": "sh"}],
          "tool": [{"kind": "file", "path": "tools/boom.sh"}]
        }
      }]
    }"#;

    assert!(!build_cached(&first, &cache, spec, &Spawns::default()).0.success);

    let spawns = Spawns::default();
    let (result, _) = build_cached(&second, &cache, spec, &spawns);
    assert!(!result.success);
    assert!(spawns.labels().is_empty()); // the failure replayed from cache
    let [proc] = &result.process_results[..] else {
        panic!("expected one result, got {:?}", result.process_results)
    };
    assert_eq!(proc.source, "cache");
    assert_eq!(proc.exit_code, Some(7));
    assert!(proc.stderr.contains("kaboom"));
}

#[test]
fn cache_hit_replays_stdout() {
    let base = scratch("stdout-replay");
    let cache = base.join("cache");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write_workspace(root, &[("a", "hi")]);
    }
    assert!(build_cached(&first, &cache, &upper_combine_spec(), &Spawns::default()).0.success);

    let (result, _) = build_cached(&second, &cache, &upper_combine_spec(), &Spawns::default());
    assert!(result.success);
    let upper = result.process_results.iter().find(|r| r.stdout.contains("uppercased")).unwrap();
    assert_eq!(upper.source, "cache"); // stdout replayed from the entry
}

// -- restored outputs route through the output root -----------------------------

/// Stand-in for the atomic layer (slice 5): points writes at a staging tree.
struct RedirectToStaging {
    root: PathBuf,
}

impl BuildLayer for RedirectToStaging {
    fn on_build_begin(&self, _plan: &ExecutionPlan, ctx: &mut NodeContext) {
        ctx.output_root = self.root.join("build/staging/add");
    }
}

#[test]
fn cache_hit_writes_through_redirected_output_root() {
    // With an output-redirecting layer outermost, restored outputs land in
    // the staging tree, not the workspace — the property that lets the
    // atomic layer commit cache hits like any process output.
    let base = scratch("redirected-restore");
    let cache = base.join("cache");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write_workspace(root, &[("a", "x")]);
    }
    assert!(build_cached(&first, &cache, &upper_combine_spec(), &Spawns::default()).0.success);

    let spec = brix_core::spec::parse(&upper_combine_spec()).unwrap();
    let mut plan =
        brix_core::planner::plan_graph(spec.actions, second.to_str().unwrap(), None).unwrap();
    let metadata = Arc::new(BuildMetadata::new(&second));
    metadata.load();
    let incremental = IncrementalLayer::new(metadata);
    let cache_layer =
        CacheLayer::with_dir(&cache, incremental.detector.clone());
    let redirect = RedirectToStaging { root: second.clone() };
    let spawns = Spawns::default();
    let result =
        execute(&mut plan, Some(2), &[&cache_layer, &incremental, &redirect], &spawns);
    assert!(result.success, "errors: {:?}", result.errors);
    assert!(spawns.labels().is_empty()); // all cache hits
    // Restored outputs went to staging, and the workspace copy was never made.
    assert_eq!(read(&second, "build/staging/add/build/all.txt"), "X");
    assert!(!second.join("build/all.txt").exists());
}

// -- pluggable stores and layer-level degradation --------------------------------

/// A store that keeps entries and blob *content* in memory — proves the
/// protocol works for a non-filesystem backend. Its entry keeps `outputs`
/// as a *list* (unlike the local store's map) to prove entries are opaque
/// to the layer.
#[derive(Default)]
struct MemoryStore {
    #[allow(clippy::type_complexity)]
    entries: Mutex<HashMap<String, (Value, BTreeMap<String, Vec<u8>>)>>,
    stores: AtomicUsize,
}

impl CacheStoreProtocol for MemoryStore {
    fn load(&self, fingerprint: &str) -> Result<Option<Entry>, String> {
        Ok(self.entries.lock().unwrap().get(fingerprint).map(|(result, files)| {
            json!({"result": result, "outputs": files.keys().collect::<Vec<_>>()})
        }))
    }

    fn restore(
        &self,
        fingerprint: &str,
        _entry: &Entry,
        ctx: &NodeContext,
    ) -> Result<bool, String> {
        let entries = self.entries.lock().unwrap();
        let Some((_, files)) = entries.get(fingerprint) else {
            return Ok(false);
        };
        for (logical, data) in files {
            let dest = ctx.output_root.join(logical);
            std::fs::create_dir_all(dest.parent().unwrap()).map_err(|e| e.to_string())?;
            std::fs::write(dest, data).map_err(|e| e.to_string())?;
        }
        Ok(true)
    }

    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String> {
        self.stores.fetch_add(1, Ordering::Relaxed);
        let mut files = BTreeMap::new();
        for (logical, physical) in outputs {
            files.insert(logical.clone(), std::fs::read(physical).map_err(|e| e.to_string())?);
        }
        self.entries
            .lock()
            .unwrap()
            .insert(fingerprint.to_string(), (serde_json::to_value(result).unwrap(), files));
        Ok(())
    }
}

/// A store that fails every operation, as an unreachable remote would.
struct Broken;

impl CacheStoreProtocol for Broken {
    fn load(&self, _fingerprint: &str) -> Result<Option<Entry>, String> {
        Err("cache unreachable".to_string())
    }
    fn restore(&self, _fingerprint: &str, _entry: &Entry, _ctx: &NodeContext)
        -> Result<bool, String> {
        Err("cache unreachable".to_string())
    }
    fn store(
        &self,
        _fingerprint: &str,
        _result: &ProcessResult,
        _outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String> {
        Err("cache unreachable".to_string())
    }
}

#[test]
fn custom_store_used_end_to_end() {
    // A pluggable store caches across workspaces just like cache_dir.
    let base = scratch("custom-store");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write_workspace(root, &[("a", "x"), ("b", "y")]);
    }
    let mem = Arc::new(MemoryStore::default());

    let (result, _) = build_with_store(
        &first,
        Box::new(mem.clone()),
        &upper_combine_spec(),
        &Spawns::default(),
    );
    assert!(result.success);
    assert!(mem.stores.load(Ordering::Relaxed) > 0); // real runs populated the store
    assert!(!runs(&first).is_empty());

    let spawns = Spawns::default();
    let (result, _) =
        build_with_store(&second, Box::new(mem.clone()), &upper_combine_spec(), &spawns);
    assert!(result.success);
    assert!(spawns.labels().is_empty()); // everything came from the store
    assert!(runs(&second).is_empty());
    assert_eq!(read(&second, "build/all.txt"), "XY");
    assert!(result.process_results.iter().all(|r| r.source == "cache"));
}

#[test]
fn broken_store_never_fails_build() {
    // Every store call errors; the build still succeeds by running the work.
    let root = scratch("broken-store");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    let spawns = Spawns::default();
    let (result, _) = build_with_store(&root, Box::new(Broken), &upper_combine_spec(), &spawns);
    assert!(result.success, "errors: {:?}", result.errors);
    assert!(!spawns.labels().is_empty()); // a store miss means the work really ran
    assert_eq!(read(&root, "build/all.txt"), "XY");
}

#[test]
fn store_that_fails_only_on_write() {
    // A populate failure is swallowed; the build's outputs are correct.
    struct WriteFails;
    impl CacheStoreProtocol for WriteFails {
        fn load(&self, _fp: &str) -> Result<Option<Entry>, String> {
            Ok(None)
        }
        fn restore(&self, _fp: &str, _e: &Entry, _ctx: &NodeContext) -> Result<bool, String> {
            Ok(false)
        }
        fn store(
            &self,
            _fp: &str,
            _r: &ProcessResult,
            _o: &BTreeMap<String, PathBuf>,
        ) -> Result<(), String> {
            Err("disk full".to_string())
        }
    }

    let root = scratch("write-fails");
    write_workspace(&root, &[("a", "x")]);
    let (result, _) =
        build_with_store(&root, Box::new(WriteFails), &upper_combine_spec(), &Spawns::default());
    assert!(result.success);
    assert_eq!(read(&root, "build/all.txt"), "X");
}

// -- two-tier ---------------------------------------------------------------------

/// Wraps a store, counting loads — to prove front-first avoids back hits.
struct Counting {
    inner: CacheStore,
    loads: Arc<AtomicUsize>,
}

impl CacheStoreProtocol for Counting {
    fn load(&self, fingerprint: &str) -> Result<Option<Entry>, String> {
        self.loads.fetch_add(1, Ordering::Relaxed);
        self.inner.load(fingerprint)
    }
    fn restore(&self, fingerprint: &str, entry: &Entry, ctx: &NodeContext)
        -> Result<bool, String> {
        self.inner.restore(fingerprint, entry, ctx)
    }
    fn store(
        &self,
        fingerprint: &str,
        result: &ProcessResult,
        outputs: &BTreeMap<String, PathBuf>,
    ) -> Result<(), String> {
        self.inner.store(fingerprint, result, outputs)
    }
}

/// Populate a back store by building once in a throwaway workspace.
fn seed_back(base: &Path, back_dir: &Path) {
    let seed = base.join("seed");
    write_workspace(&seed, &[("a", "x"), ("b", "y")]);
    let (result, _) = build_with_store(
        &seed,
        Box::new(CacheStore::new(back_dir)),
        &upper_combine_spec(),
        &Spawns::default(),
    );
    assert!(result.success);
}

fn has_entry(dir: &Path) -> bool {
    fn walk(dir: &Path) -> bool {
        let Ok(entries) = std::fs::read_dir(dir) else { return false };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() && walk(&path) {
                return true;
            }
            if path.file_name().is_some_and(|n| n == "entry.json") {
                return true;
            }
        }
        false
    }
    walk(dir)
}

#[test]
fn two_tier_back_hit_promotes_to_front() {
    // A back hit serves the build and is copied into the front, so the next
    // machine-local build needs no back round-trip.
    let base = scratch("two-tier-promote");
    let (back_dir, front_dir) = (base.join("back"), base.join("front"));
    seed_back(&base, &back_dir);

    // First build: front empty, so the entries come from the back tier.
    let b = base.join("b");
    write_workspace(&b, &[("a", "x"), ("b", "y")]);
    let back_loads = Arc::new(AtomicUsize::new(0));
    let store = TwoTierCacheStore::new(
        Box::new(CacheStore::new(&front_dir)),
        Box::new(Counting { inner: CacheStore::new(&back_dir), loads: back_loads.clone() }),
    );
    let spawns = Spawns::default();
    let (result, _) = build_with_store(&b, Box::new(store), &upper_combine_spec(), &spawns);
    assert!(result.success);
    assert!(spawns.labels().is_empty()); // served entirely from cache (back tier)
    assert!(back_loads.load(Ordering::Relaxed) > 0); // the back was consulted
    assert_eq!(read(&b, "build/all.txt"), "XY");
    assert!(has_entry(&front_dir)); // promoted into the front

    // Second build: front is now warm, the back must not be touched at all.
    let c = base.join("c");
    write_workspace(&c, &[("a", "x"), ("b", "y")]);
    let back_loads = Arc::new(AtomicUsize::new(0));
    let store = TwoTierCacheStore::new(
        Box::new(CacheStore::new(&front_dir)),
        Box::new(Counting { inner: CacheStore::new(&back_dir), loads: back_loads.clone() }),
    );
    let spawns = Spawns::default();
    let (result, _) = build_with_store(&c, Box::new(store), &upper_combine_spec(), &spawns);
    assert!(result.success);
    assert!(spawns.labels().is_empty());
    assert_eq!(back_loads.load(Ordering::Relaxed), 0); // front served everything
    assert_eq!(read(&c, "build/all.txt"), "XY");
}

#[test]
fn two_tier_store_writes_both_tiers() {
    // A real run populates front and back together.
    let root = scratch("two-tier-writes");
    let (back_dir, front_dir) = (root.join("back"), root.join("front"));
    write_workspace(&root, &[("a", "x")]);
    let store = TwoTierCacheStore::new(
        Box::new(CacheStore::new(&front_dir)),
        Box::new(CacheStore::new(&back_dir)),
    );
    let (result, _) =
        build_with_store(&root, Box::new(store), &upper_combine_spec(), &Spawns::default());
    assert!(result.success);
    assert!(has_entry(&front_dir));
    assert!(has_entry(&back_dir));
}

#[test]
fn two_tier_back_down_still_caches_front() {
    // With the back unreachable, the front is still populated and served.
    let base = scratch("two-tier-back-down");
    let front_dir = base.join("front");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write_workspace(root, &[("a", "x")]);
    }

    // Build with a broken back: front should still get the entries.
    let store =
        TwoTierCacheStore::new(Box::new(CacheStore::new(&front_dir)), Box::new(Broken));
    let (result, _) =
        build_with_store(&first, Box::new(store), &upper_combine_spec(), &Spawns::default());
    assert!(result.success);
    assert!(has_entry(&front_dir));

    // A second workspace hits the warm front despite the back being down.
    let store =
        TwoTierCacheStore::new(Box::new(CacheStore::new(&front_dir)), Box::new(Broken));
    let spawns = Spawns::default();
    let (result, _) =
        build_with_store(&second, Box::new(store), &upper_combine_spec(), &spawns);
    assert!(result.success);
    assert!(spawns.labels().is_empty());
    assert_eq!(read(&second, "build/all.txt"), "X");
}

// -- Python interop: shared on-disk layout ---------------------------------------

const GEN_SPEC: &str = r#"{
  "version": 1,
  "actions": [{
    "kind": "process",
    "name": "gen",
    "command_line": "{shell} {tool} {out}",
    "inputs": {
      "shell": [{"kind": "tool", "name": "sh"}],
      "tool": [{"kind": "file", "path": "tools/gen.sh"}]
    },
    "outputs": {"out": [{"kind": "file", "path": "build/out.txt"}]}
  }]
}"#;

fn gen_action_fingerprint(root: &Path) -> String {
    let spec = brix_core::spec::parse(GEN_SPEC).unwrap();
    let Action::Process { .. } = &spec.actions[0] else { panic!("expected process") };
    let ctx = NodeContext::for_root(root);
    action_fingerprint(&spec.actions[0], &ctx, &TimeAndHashChangeDetector::new()).unwrap()
}

#[test]
fn restores_from_python_written_entry() {
    // A hand-written entry in exactly the shape Python's CacheStore.store
    // produces (json.dumps(sort_keys=True) of {"outputs", "result"}); the
    // Rust layer must hit it, restore its blob, and replay its result.
    let root = scratch("python-entry");
    let cache = root.join("cache");
    write(&root, "tools/gen.sh", "echo run >> runs.log\necho generated > \"$1\"\n");
    let fp = gen_action_fingerprint(&root);
    let entry_dir = cache.join(&fp[..2]).join(&fp);
    std::fs::create_dir_all(entry_dir.join("files")).unwrap();
    std::fs::write(entry_dir.join("files/0"), "FROM PYTHON\n").unwrap();
    std::fs::write(
        entry_dir.join("entry.json"),
        r#"{"outputs": {"build/out.txt": "0"}, "result": {"action_name": "gen", "argv": ["sh"], "command": "sh tools/gen.sh build/out.txt", "exit_code": 0, "signal": null, "source": "process", "stderr": "", "stdout": "hello from python"}}"#,
    )
    .unwrap();

    let spawns = Spawns::default();
    let (result, _) = build_cached(&root, &cache, GEN_SPEC, &spawns);
    assert!(result.success, "errors: {:?}", result.errors);
    assert!(spawns.labels().is_empty()); // hit — the tool never ran
    assert!(runs(&root).is_empty());
    assert_eq!(read(&root, "build/out.txt"), "FROM PYTHON\n");
    let [proc] = &result.process_results[..] else { panic!("one result expected") };
    assert_eq!(proc.source, "cache");
    assert_eq!(proc.stdout, "hello from python");
}

#[test]
fn store_layout_matches_python() {
    // A Rust-written entry sits at <fp[:2]>/<fp>/entry.json with files/<n>
    // blobs and the exact key shape Python reads back.
    let root = scratch("layout");
    let cache = root.join("cache");
    write(&root, "tools/gen.sh", "echo run >> runs.log\necho made-by-rust > \"$1\"\n");
    assert!(build_cached(&root, &cache, GEN_SPEC, &Spawns::default()).0.success);

    let fp = gen_action_fingerprint(&root);
    let entry_dir = cache.join(&fp[..2]).join(&fp);
    let entry: Value =
        serde_json::from_str(&read(&root, entry_dir.join("entry.json").to_str().unwrap()))
            .unwrap();
    assert_eq!(entry["outputs"], json!({"build/out.txt": "0"}));
    assert_eq!(read(&root, entry_dir.join("files/0").to_str().unwrap()), "made-by-rust\n");
    // The result dict carries every field Python's ProcessResult(**d) needs.
    let result = entry["result"].as_object().unwrap();
    for field in
        ["action_name", "argv", "command", "exit_code", "signal", "source", "stderr", "stdout"]
    {
        assert!(result.contains_key(field), "missing {field}: {result:?}");
    }
    assert_eq!(result["exit_code"], json!(0));
    assert_eq!(result["source"], json!("process"));
}
