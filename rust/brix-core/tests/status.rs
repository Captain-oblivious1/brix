//! `compute_status` + `clean`: the Rust mirror of `tests/test_status.py`'s
//! core cases (same names; the `test_cli_*` cases stay Python — the CLI is a
//! skin, and `clean` is mirrored here at the core level it moved to).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use brix_core::executor::execute;
use brix_core::incremental::IncrementalLayer;
use brix_core::metadata::BuildMetadata;
use brix_core::report::NullReporter;
use brix_core::status::{
    clean, compute_status, StatusReport, FAILED, FRESH, INPUTS_CHANGED, NEVER_BUILT, OUTDATED,
    OUTPUTS_MISSING,
};

fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-status-{name}-{}-{}",
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

fn build(root: &Path) {
    let spec = brix_core::spec::parse(&upper_combine_spec()).unwrap();
    let mut plan =
        brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap();
    let metadata = Arc::new(BuildMetadata::new(root));
    metadata.load();
    let layer = IncrementalLayer::new(metadata);
    execute(&mut plan, Some(2), &[&layer], &NullReporter);
}

fn status(root: &Path) -> StatusReport {
    let spec = brix_core::spec::parse(&upper_combine_spec()).unwrap();
    compute_status(spec.actions, root, "build/.brix", "build/staging", None, None).unwrap()
}

fn by_label(report: &StatusReport) -> HashMap<String, (String, String)> {
    report
        .actions
        .iter()
        .map(|a| (a.label.clone(), (a.state.clone(), a.detail.clone())))
        .collect()
}

fn is_clean(report: &StatusReport) -> bool {
    report.pending_deletions.is_empty()
        && report.staged_files.is_empty()
        && report.staged_deletions.is_empty()
        && !report.interrupted_commit
        && report.actions.iter().all(|a| a.state == FRESH)
}

#[test]
fn never_built_everything_stale() {
    let root = scratch("never-built");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    let report = status(&root);
    assert!(!is_clean(&report));
    assert!(report.actions.iter().all(|a| a.state == NEVER_BUILT));
    let labels: std::collections::BTreeSet<&str> =
        report.actions.iter().map(|a| a.label.as_str()).collect();
    assert_eq!(labels, ["combine", "upper a", "upper b"].into_iter().collect());
}

#[test]
fn after_build_everything_fresh() {
    let root = scratch("all-fresh");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    build(&root);
    let report = status(&root);
    assert!(is_clean(&report), "{report:?}");
}

#[test]
fn status_does_not_build_or_run() {
    let root = scratch("no-run");
    write_workspace(&root, &[("a", "x")]);
    status(&root);
    assert!(!root.join("runs.log").exists()); // no process ran
    assert!(!root.join("build/all.txt").exists());
}

#[test]
fn edited_source_stales_action_and_outdates_downstream() {
    let root = scratch("edited");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    build(&root);
    write(&root, "src/a.txt", "CHANGED");
    let report = status(&root);
    let states = by_label(&report);
    assert_eq!(states["upper a"].0, INPUTS_CHANGED);
    assert_eq!(states["upper b"].0, FRESH);
    // combine's inputs haven't changed *yet*, but upstream will rebuild.
    assert_eq!(states["combine"].0, OUTDATED);
    assert_eq!(states["combine"].1, "upstream will rebuild");
}

#[test]
fn deleted_output_reported() {
    let root = scratch("deleted-output");
    write_workspace(&root, &[("a", "x")]);
    build(&root);
    std::fs::remove_file(root.join("build/upper/a.txt")).unwrap();
    let report = status(&root);
    assert_eq!(by_label(&report)["upper a"].0, OUTPUTS_MISSING);
}

#[test]
fn deleted_source_reports_pending_deletion() {
    let root = scratch("deleted-source");
    write_workspace(&root, &[("a", "x"), ("b", "y")]);
    build(&root);
    std::fs::remove_file(root.join("src/b.txt")).unwrap();
    let report = status(&root);
    assert_eq!(report.pending_deletions.len(), 1);
    let deletion = &report.pending_deletions[0];
    assert_eq!(deletion.member, "b");
    assert_eq!(deletion.paths, vec!["build/upper/b.txt"]);
    assert_eq!(by_label(&report)["combine"].0, OUTDATED);
}

#[test]
fn failed_action_reported_with_result() {
    let root = scratch("failed");
    write_workspace(&root, &[("a", "x")]);
    write(&root, "tools/upper.sh", "echo 'cannot upper' >&2\nexit 4\n");
    build(&root);
    let report = status(&root);
    let failed = report.actions.iter().find(|a| a.label == "upper a").unwrap();
    assert_eq!(failed.state, FAILED);
    let result = failed.last_result.as_ref().unwrap();
    assert_eq!(result.exit_code, Some(4));
    assert!(result.stderr.contains("cannot upper"), "{}", result.stderr);
    // Python: summary().removeprefix("FAILED ") — the CLI-rendered detail.
    assert_eq!(failed.detail, "(exit 4): upper a");
}

// The core half of test_cli_clean_removes_outputs_and_metadata (the CLI's
// printing and exit code stay Python).
#[test]
fn clean_removes_outputs_and_metadata() {
    let root = scratch("clean");
    write_workspace(&root, &[("a", "x")]);
    build(&root);
    assert!(root.join("build/all.txt").exists());
    let spec = brix_core::spec::parse(&upper_combine_spec()).unwrap();
    let removed = clean(spec.actions, &root, "build/.brix").unwrap();
    assert!(removed >= 2, "{removed}");
    assert!(!root.join("build/all.txt").exists());
    assert!(!root.join("build/upper/a.txt").exists());
    assert!(!root.join("build/.brix").exists());
    // After clean, status is back to never-built.
    let report = status(&root);
    assert!(report.actions.iter().all(|a| a.state == NEVER_BUILT));
}
