//! Native git-detection vouch logic, mirroring the targeted tests in
//! `tests/test_git_detect.py`. Skips where no `git` is on PATH.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use brix_core::gitdetect::prepare;
use brix_core::metadata::BuildMetadata;

fn have_git() -> bool {
    Command::new("git").arg("--version").output().map(|o| o.status.success()).unwrap_or(false)
}

fn scratch() -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-gitdetect-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    // Canonicalize so paths line up with git's --show-toplevel output.
    std::fs::canonicalize(&dir).unwrap()
}

fn git(root: &Path, args: &[&str]) {
    let ok = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .unwrap()
        .status
        .success();
    assert!(ok, "git {args:?} failed");
}

fn init_repo(root: &Path) {
    git(root, &["init", "-q"]);
    git(root, &["config", "user.email", "t@example.com"]);
    git(root, &["config", "user.name", "Test"]);
}

fn commit(root: &Path) {
    git(root, &["add", "-A"]);
    git(root, &["commit", "-q", "-m", "c"]);
}

fn write(root: &Path, rel: &str, content: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, content).unwrap();
}

/// A detector metadata handle whose baseline pair is already recorded (a prior
/// `prepare`), the precondition for any vouching.
fn with_baseline(root: &Path) -> Arc<BuildMetadata> {
    let md = Arc::new(BuildMetadata::with_directory(root, "build/.brix"));
    md.load();
    let vouch = prepare(root, &md); // first prepare: records baseline, vouches nothing
    assert!(vouch.is_none(), "first prepare must not vouch");
    md
}

#[test]
fn first_build_has_no_baseline_so_vouches_nothing() {
    if !have_git() {
        return;
    }
    let root = scratch();
    write(&root, "src/a.txt", "x");
    init_repo(&root);
    commit(&root);
    let md = Arc::new(BuildMetadata::with_directory(&root, "build/.brix"));
    md.load();
    assert!(prepare(&root, &md).is_none());
}

#[test]
fn unchanged_tracked_file_is_vouched_after_baseline() {
    if !have_git() {
        return;
    }
    let root = scratch();
    write(&root, "src/a.txt", "x");
    init_repo(&root);
    commit(&root);
    let md = with_baseline(&root);

    let vouch = prepare(&root, &md).expect("second prepare vouches");
    assert!(vouch.vouch_unchanged(&root.join("src/a.txt")));

    // An edited tracked file is in the working-tree diff -> not vouched.
    write(&root, "src/a.txt", "CHANGED");
    let vouch = prepare(&root, &md).expect("vouch ready");
    assert!(!vouch.vouch_unchanged(&root.join("src/a.txt")));
}

#[test]
fn untracked_and_out_of_tree_files_are_never_vouched() {
    if !have_git() {
        return;
    }
    let root = scratch();
    write(&root, "src/a.txt", "x");
    init_repo(&root);
    commit(&root);
    let md = with_baseline(&root);
    write(&root, "src/untracked.txt", "u");
    let vouch = prepare(&root, &md).expect("vouch ready");
    assert!(!vouch.vouch_unchanged(&root.join("src/untracked.txt")));
    assert!(!vouch.vouch_unchanged(&root.parent().unwrap().join("elsewhere.txt")));
}

#[test]
fn file_dirty_at_last_build_is_not_vouched_after_revert() {
    if !have_git() {
        return;
    }
    // The revert hole: a file dirty at build N (its dirty content's hash is
    // stored), reverted to HEAD before N+1. Git then calls it clean and
    // baseline..HEAD is empty — but its content is not what the last build
    // hashed, so the previous build's dirty set must block the vouch.
    let root = scratch();
    write(&root, "src/a.txt", "x");
    write(&root, "src/b.txt", "y");
    init_repo(&root);
    commit(&root);
    let md = with_baseline(&root); // build 1: nothing dirty

    write(&root, "src/a.txt", "DIRTY");
    let vouch = prepare(&root, &md).expect("vouch ready"); // build 2: a.txt dirty
    assert!(!vouch.vouch_unchanged(&root.join("src/a.txt")));
    // build 2 recorded {src/a.txt} as its dirty set.

    git(&root, &["checkout", "--", "src/a.txt"]); // revert: clean vs HEAD again
    let vouch = prepare(&root, &md).expect("vouch ready");
    assert!(
        !vouch.vouch_unchanged(&root.join("src/a.txt")),
        "a file dirty at the last build must not be vouched after a revert"
    );
    assert!(
        vouch.vouch_unchanged(&root.join("src/b.txt")),
        "an untouched tracked file is still vouched"
    );
}

#[test]
fn commit_between_builds_is_caught() {
    if !have_git() {
        return;
    }
    let root = scratch();
    write(&root, "src/a.txt", "x");
    init_repo(&root);
    commit(&root);
    let md = with_baseline(&root);

    // Change and COMMIT between builds: baseline..HEAD must mark it changed,
    // so the user never has to leave it dirty for brix to notice.
    write(&root, "src/a.txt", "committed change");
    commit(&root);
    let vouch = prepare(&root, &md).expect("vouch ready");
    assert!(!vouch.vouch_unchanged(&root.join("src/a.txt")));
}

#[test]
fn not_a_git_repo_yields_no_vouch() {
    if !have_git() {
        return;
    }
    let root = scratch(); // no git init
    write(&root, "src/a.txt", "x");
    let md = Arc::new(BuildMetadata::with_directory(&root, "build/.brix"));
    md.load();
    assert!(prepare(&root, &md).is_none());
}
