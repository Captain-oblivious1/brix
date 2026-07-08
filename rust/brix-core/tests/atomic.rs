//! The atomic layer's behavioral contract, mirrored from
//! `tests/test_atomic.py` and `tests/test_held_staging.py` (minus the
//! status/CLI cases — those stay Python until the boundary slice). The
//! stack is the real one: incremental inside, atomic outermost, over the
//! real scheduler.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use brix_core::atomic::{commit_staging, discard_staging, AtomicLayer, COMMIT_MARKER, HELD_MARKER};
use brix_core::executor::{execute, BuildResult};
use brix_core::incremental::IncrementalLayer;
use brix_core::metadata::BuildMetadata;
use brix_core::plan::ExecutionPlan;
use brix_core::report::NullReporter;

const STAGING: &str = "build/staging";

// -- helpers (shared shape with tests/incremental.rs) --------------------------

fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-atomic-{name}-{}-{}",
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

fn plan_for(root: &Path, spec_json: &str) -> ExecutionPlan {
    let spec = brix_core::spec::parse(spec_json).unwrap();
    brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap()
}

/// `AtomicBuilder(CleanBuilder(...))`: atomic alone.
fn build_atomic_clean(root: &Path, spec_json: &str) -> BuildResult {
    let mut plan = plan_for(root, spec_json);
    let atomic = AtomicLayer::new(STAGING, false);
    execute(&mut plan, Some(2), &[&atomic], &NullReporter)
}

/// `AtomicBuilder(IncrementalBuilder(...))`: the full stack, optionally held.
fn build_atomic_incremental(root: &Path, spec_json: &str, hold: bool) -> BuildResult {
    let mut plan = plan_for(root, spec_json);
    let metadata = Arc::new(BuildMetadata::new(root));
    metadata.load();
    let incremental = IncrementalLayer::new(metadata);
    let atomic = AtomicLayer::new(STAGING, hold);
    execute(&mut plan, Some(2), &[&incremental, &atomic], &NullReporter)
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

fn count(counts: &HashMap<String, usize>, key: &str) -> usize {
    counts.get(key).copied().unwrap_or(0)
}

fn read(root: &Path, rel: &str) -> String {
    std::fs::read_to_string(root.join(rel)).unwrap()
}

// -- atomic commit ---------------------------------------------------------------

#[test]
fn success_commits_and_clears_staging() {
    let root = scratch("success-commits");
    write_workspace(&root, &[("a", "hi "), ("b", "there")]);
    let result = build_atomic_clean(&root, &upper_combine_spec());
    assert!(result.success, "errors: {:?}", result.errors);
    assert_eq!(read(&root, "build/all.txt"), "HI THERE");
    assert_eq!(read(&root, "build/upper/a.txt"), "HI ");
    assert!(!root.join(STAGING).exists());
}

#[test]
fn downstream_reads_staged_upstream_outputs() {
    // combine consumes upper's outputs, which exist only in staging until
    // commit — the overlay must make it see them.
    let root = scratch("staged-reads");
    write_workspace(&root, &[("a", "one "), ("b", "two")]);
    let result = build_atomic_clean(&root, &upper_combine_spec());
    assert!(result.success);
    assert_eq!(read(&root, "build/all.txt"), "ONE TWO");
}

#[test]
fn failure_leaves_workspace_untouched() {
    let root = scratch("failure-untouched");
    write_workspace(&root, &[("a", "good"), ("b", "bad")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    let committed = read(&root, "build/all.txt");

    // Change both sources, but make the tool fail on the second.
    write(&root, "src/a.txt", "new a");
    write(&root, "src/b.txt", "new b");
    write(
        &root,
        "tools/upper.sh",
        "echo \"upper:$1\" >> runs.log\n\
         case \"$1\" in *b.txt) exit 2;; esac\n\
         tr a-z A-Z < \"$1\" > \"$2\"\n",
    );
    let result = build_atomic_incremental(&root, &upper_combine_spec(), false);
    assert!(!result.success);
    // Nothing changed in the workspace — not even upper a's successful output.
    assert_eq!(read(&root, "build/all.txt"), committed);
    assert_eq!(read(&root, "build/upper/a.txt"), "GOOD");
    // The successful part of the failed build is staged, inspectable.
    assert_eq!(read(&root, &format!("{STAGING}/add/build/upper/a.txt")), "NEW A");
}

#[test]
fn atomic_composes_with_incremental() {
    let root = scratch("composes");
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    let after_first = runs(&root);

    // Nothing changed: no processes, nothing staged, still committed.
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    assert_eq!(runs(&root), after_first);
    assert!(!root.join(STAGING).exists());
    assert_eq!(read(&root, "build/all.txt"), "AABB");

    // One edit: only its chain reruns; result commits.
    write(&root, "src/a.txt", "changed ");
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    let counts = runs(&root);
    assert_eq!(count(&counts, "upper:src/a.txt"), 2);
    assert_eq!(count(&counts, "upper:src/b.txt"), 1);
    assert_eq!(read(&root, "build/all.txt"), "CHANGED BB");
}

#[test]
fn deleted_source_deletion_is_staged_then_committed() {
    let root = scratch("staged-deletion");
    write_workspace(&root, &[("a", "aa"), ("b", "bb")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    std::fs::remove_file(root.join("src/b.txt")).unwrap();
    let result = build_atomic_incremental(&root, &upper_combine_spec(), false);
    assert!(result.success, "errors: {:?}", result.errors);
    // The stale output is gone from the workspace only via commit,
    // and combine never saw it through the overlay.
    assert!(!root.join("build/upper/b.txt").exists());
    assert_eq!(read(&root, "build/all.txt"), "AA");
    assert!(!root.join(STAGING).exists());
}

#[test]
fn generated_collection_realizes_through_staging() {
    // Generator writes into staging; the expansion must see the staged files.
    let root = scratch("generated");
    write(&root, "tools/upper.sh", UPPER_SH);
    write(
        &root,
        "tools/generate.sh",
        "out=\"$1\"\n\
         shift\n\
         mkdir -p \"$out\"\n\
         for name in \"$@\"; do printf '%s' \"$name\" > \"$out/$name.txt\"; done\n",
    );
    let spec = r#"{
      "version": 1,
      "actions": [
        {
          "kind": "process",
          "name": "generate",
          "command_line": "{shell} {tool} {out_dir} alpha beta",
          "inputs": {
            "shell": [{"kind": "tool", "name": "sh"}],
            "tool": [{"kind": "file", "path": "tools/generate.sh"}]
          },
          "outputs": {
            "out_dir": [{"kind": "file", "path": "build/generated"}],
            "generated": [{"kind": "glob", "template": "build/generated/{name}.txt"}]
          }
        },
        {
          "kind": "expand",
          "element": "name",
          "collection": [{"kind": "glob", "template": "build/generated/{name}.txt"}],
          "child": {
            "kind": "process",
            "name": "upper {name}",
            "command_line": "{shell} {tool} {source} {dest}",
            "inputs": {
              "shell": [{"kind": "tool", "name": "sh"}],
              "tool": [{"kind": "file", "path": "tools/upper.sh"}],
              "source": [{"kind": "file", "path": "build/generated/{name}.txt"}]
            },
            "outputs": {"dest": [{"kind": "file", "path": "build/upper/{name}.txt"}]}
          }
        }
      ]
    }"#;
    let result = build_atomic_clean(&root, spec);
    assert!(result.success, "errors: {:?}", result.errors);
    assert_eq!(read(&root, "build/upper/alpha.txt"), "ALPHA");
    assert_eq!(read(&root, "build/generated/beta.txt"), "beta");
    assert!(!root.join(STAGING).exists());
}

#[test]
fn interrupted_commit_is_completed_on_next_build() {
    let root = scratch("interrupted-commit");
    write_workspace(&root, &[("a", "x")]);
    // Simulate a crash mid-commit: staged output + COMMIT marker, half-committed.
    write(&root, &format!("{STAGING}/add/build/half.txt"), "recovered");
    write(&root, &format!("{STAGING}/{COMMIT_MARKER}"), "");

    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    assert_eq!(read(&root, "build/half.txt"), "recovered");
    assert!(!root.join(STAGING).exists());
}

#[test]
fn stale_staging_without_marker_is_discarded() {
    let root = scratch("stale-staging");
    write_workspace(&root, &[("a", "x")]);
    write(&root, &format!("{STAGING}/add/build/garbage.txt"), "should never land");

    assert!(build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    assert!(!root.join("build/garbage.txt").exists());
    assert!(!root.join(STAGING).exists());
}

// -- held staging -----------------------------------------------------------------

#[test]
fn hold_leaves_workspace_untouched() {
    let root = scratch("hold-untouched");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    let result = build_atomic_incremental(&root, &upper_combine_spec(), true);
    assert!(result.success);
    assert!(!root.join("build/all.txt").exists());
    assert!(!root.join("build/upper/a.txt").exists());
    let staging = root.join(STAGING);
    assert!(staging.join(HELD_MARKER).exists());
    assert_eq!(read(&root, &format!("{STAGING}/add/build/all.txt")), "XY");
}

#[test]
fn commit_applies_and_clears() {
    let root = scratch("commit-applies");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), true).success);

    let staging = root.join(STAGING);
    let (files, deletions) = commit_staging(&root, &staging).unwrap().unwrap();
    assert_eq!((files, deletions), (3, 0)); // upper/a, upper/b, all.txt
    assert_eq!(read(&root, "build/all.txt"), "XY");
    assert!(!staging.exists());
    assert_eq!(commit_staging(&root, &staging).unwrap(), None); // nothing left
}

#[test]
fn plain_build_on_top_of_held_commits_everything() {
    let root = scratch("plain-on-held");
    write_workspace(&root, &[("a", "x")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), true).success);
    let ran_before = runs(&root);

    // No hold: replay on top, then commit all.
    let result = build_atomic_incremental(&root, &upper_combine_spec(), false);
    assert!(result.success);
    assert_eq!(runs(&root), ran_before); // nothing re-ran
    assert_eq!(read(&root, "build/all.txt"), "X");
    assert!(!root.join(STAGING).exists());
}

#[test]
fn discard_throws_staging_away() {
    let root = scratch("discard");
    write_workspace(&root, &[("a", "x")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), true).success);

    let staging = root.join(STAGING);
    assert_eq!(discard_staging(&staging).unwrap(), Some(2)); // upper/a.txt + all.txt
    assert!(!staging.exists());
    assert!(!root.join("build/all.txt").exists()); // workspace untouched
    assert_eq!(discard_staging(&staging).unwrap(), None);
}

#[test]
fn failure_on_top_of_held_never_commits() {
    // A failed build mixes held outputs with its own leavings; that tree
    // must never reach the workspace — commit refuses, next build discards.
    let root = scratch("failure-on-held");
    write_workspace(&root, &[("a", "x")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), true).success);

    write(&root, "tools/upper.sh", "exit 3\n");
    assert!(!build_atomic_incremental(&root, &upper_combine_spec(), false).success);
    let staging = root.join(STAGING);
    assert!(!staging.join(HELD_MARKER).exists());
    let err = commit_staging(&root, &staging).unwrap_err();
    assert!(err.contains("failed build"), "{err}");

    write(&root, "tools/upper.sh", UPPER_SH);
    // Discards the debris, rebuilds, commits.
    let result = build_atomic_incremental(&root, &upper_combine_spec(), false);
    assert!(result.success);
    assert_eq!(read(&root, "build/all.txt"), "X");
}

// -- the full stack ---------------------------------------------------------------

#[test]
fn cache_under_atomic_routes_through_staging() {
    // AtomicBuilder(CacheBuilder(IncrementalBuilder(...))): restored outputs
    // land in staging and only commit with the build.
    use brix_core::cache::{CacheLayer, CacheStore};
    use brix_core::report::Reporter;

    let base = scratch("full-stack");
    let cache_dir = base.join("cache");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        write_workspace(root, &[("a", "x")]);
    }

    let full_stack = |root: &Path, reporter: &dyn Reporter| {
        let mut plan = plan_for(root, &upper_combine_spec());
        let metadata = Arc::new(BuildMetadata::new(root));
        metadata.load();
        let incremental = IncrementalLayer::new(metadata);
        let cache = CacheLayer::new(
            Box::new(CacheStore::new(&cache_dir)),
            incremental.detector.clone(),
        );
        let atomic = AtomicLayer::new(STAGING, false);
        execute(&mut plan, Some(2), &[&cache, &incremental, &atomic], reporter)
    };

    assert!(full_stack(&first, &NullReporter).success);

    /// Spots restored outputs sitting in staging mid-build, and counts spawns.
    #[derive(Default)]
    struct StagingWatcher {
        root: PathBuf,
        spawns: AtomicUsize,
        seen_in_staging: std::sync::atomic::AtomicBool,
    }
    impl Reporter for StagingWatcher {
        fn process_started(&self, _node: &brix_core::plan::Node) {
            self.spawns.fetch_add(1, Ordering::Relaxed);
        }
        fn node_completed(
            &self,
            _node: &brix_core::plan::Node,
            _status: brix_core::plan::NodeState,
            _result: Option<&brix_core::process::ProcessResult>,
            _error: Option<&str>,
        ) {
            let staged = self.root.join(STAGING).join("add/build");
            if std::fs::read_dir(&staged).map(|mut d| d.next().is_some()).unwrap_or(false) {
                self.seen_in_staging.store(true, Ordering::Relaxed);
            }
        }
    }

    let watcher =
        StagingWatcher { root: second.clone(), ..Default::default() };
    let result = full_stack(&second, &watcher);
    assert!(result.success, "errors: {:?}", result.errors);
    assert_eq!(watcher.spawns.load(Ordering::Relaxed), 0); // all cache hits
    assert!(watcher.seen_in_staging.load(Ordering::Relaxed)); // restored via staging
    assert_eq!(read(&second, "build/all.txt"), "X"); // then committed
    assert!(!second.join(STAGING).exists());
}

#[test]
fn discard_refuses_an_interrupted_commit() {
    let root = scratch("discard-refuses");
    write_workspace(&root, &[("a", "x")]);
    assert!(build_atomic_incremental(&root, &upper_combine_spec(), true).success);
    let staging = root.join(STAGING);
    write(&root, &format!("{STAGING}/{COMMIT_MARKER}"), ""); // simulate crash
    let err = discard_staging(&staging).unwrap_err();
    assert!(err.contains("interrupted commit"), "{err}");
}
