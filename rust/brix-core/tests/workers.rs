//! Persistent workers, mirrored from `tests/test_workers.py` — either engine
//! must satisfy these. Same cases, same names: a worker-routed action is
//! indistinguishable from a spawned one (results, provenance, caching,
//! liveness), and failures degrade correctly (crash / startup failure ->
//! exit_code=None, never cached, no silent fallback to spawning).
//!
//! The protocol workers are python3 scripts (the JSON line protocol needs a
//! real language; brix is a Python project, so python3 is present on any dev
//! machine) — the same scripts `tests/workers/` uses, embedded per workspace.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use brix_core::cache::{CacheLayer, CacheStore};
use brix_core::content::action_fingerprint;
use brix_core::context::NodeContext;
use brix_core::detector::TimeAndHashChangeDetector;
use brix_core::executor::{execute, BuildResult};
use brix_core::incremental::IncrementalLayer;
use brix_core::metadata::BuildMetadata;
use brix_core::plan::Node;
use brix_core::report::{NullReporter, Reporter};

// -- helpers (shared shape with tests/cache.rs) ---------------------------------

fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-workers-{name}-{}-{}",
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

/// `tests/workers/exec_worker.py`: runs each request's args as a subprocess in
/// the request's cwd and returns the result. Because it just execs the argv, a
/// worker-routed action must produce exactly what spawning would — that is the
/// property these tests assert.
const EXEC_WORKER_PY: &str = r#"import json
import subprocess
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    proc = subprocess.run(req["args"], cwd=req["cwd"], capture_output=True, text=True)
    sys.stdout.write(
        json.dumps(
            {
                "id": req["id"],
                "exitCode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
        + "\n"
    )
    sys.stdout.flush()
"#;

/// `tests/workers/crash_worker.py`: reads one request and dies without
/// answering — the in-flight request must fail with exit_code=None.
const CRASH_WORKER_PY: &str = "import sys\nsys.stdin.readline()\nsys.exit(3)\n";

const UPPER_SH: &str = "echo \"uppercased $1\"\ntr a-z A-Z < \"$1\" > \"$2\"\n";

fn workspace(root: &Path) {
    write(root, "src/a.txt", "hi");
    write(root, "tools/upper.sh", UPPER_SH);
    write(root, "tools/exec_worker.py", EXEC_WORKER_PY);
    write(root, "tools/crash_worker.py", CRASH_WORKER_PY);
}

/// The `upper_graph` from the Python test file, wire-shaped. `worker` names a
/// workspace-relative worker script to route through, or None to spawn.
fn upper_spec(worker: Option<&str>) -> String {
    let mut action = serde_json::json!({
        "kind": "process",
        "name": "upper a",
        "command_line": "{shell} {tool} {src} {dst}",
        "inputs": {
            "shell": [{"kind": "tool", "name": "sh"}],
            "tool": [{"kind": "file", "path": "tools/upper.sh"}],
            "src": [{"kind": "file", "path": "src/a.txt"}]
        },
        "outputs": {"dst": [{"kind": "file", "path": "build/a.txt"}]}
    });
    if let Some(script) = worker {
        action["worker"] = serde_json::json!({
            "name": "exec",
            "command_line": "{py} {w}",
            "inputs": {
                "py": [{"kind": "tool", "name": "python3"}],
                "w": [{"kind": "file", "path": script}]
            }
        });
    }
    serde_json::json!({"version": 1, "actions": [action]}).to_string()
}

/// Records `process_started` — fires on a real spawn *or* a worker dispatch,
/// never on a replay/cache hit (the Python `Started` reporter).
#[derive(Default)]
struct Started {
    labels: Mutex<Vec<String>>,
}

impl Started {
    fn labels(&self) -> Vec<String> {
        self.labels.lock().unwrap().clone()
    }
}

impl Reporter for Started {
    fn process_started(&self, node: &Node) {
        self.labels.lock().unwrap().push(node.label.clone());
    }
}

fn build(root: &Path, spec_json: &str, reporter: &dyn Reporter) -> BuildResult {
    let spec = brix_core::spec::parse(spec_json).unwrap();
    let mut plan =
        brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap();
    execute(&mut plan, Some(2), &[], reporter)
}

/// The `CacheBuilder(IncrementalBuilder(...))` stack, as in tests/cache.rs.
fn build_cached(root: &Path, cache_dir: &Path, spec_json: &str, reporter: &dyn Reporter) -> BuildResult {
    let spec = brix_core::spec::parse(spec_json).unwrap();
    let mut plan =
        brix_core::planner::plan_graph(spec.actions, root.to_str().unwrap(), None).unwrap();
    let metadata = Arc::new(BuildMetadata::new(root));
    metadata.load();
    let incremental = IncrementalLayer::new(metadata);
    let cache =
        CacheLayer::new(Box::new(CacheStore::new(cache_dir)), incremental.detector.clone());
    execute(&mut plan, Some(2), &[&cache, &incremental], reporter)
}

fn read(root: &Path, rel: &str) -> String {
    std::fs::read_to_string(root.join(rel)).unwrap()
}

// -- the contract ---------------------------------------------------------------

#[test]
fn worker_output_identical_to_spawning() {
    let base = scratch("identical");
    let (spawned, worked) = (base.join("spawn"), base.join("work"));
    for root in [&spawned, &worked] {
        workspace(root);
    }

    let spawn_result = build(&spawned, &upper_spec(None), &NullReporter);
    let started = Started::default();
    let worker_result = build(&worked, &upper_spec(Some("tools/exec_worker.py")), &started);

    assert!(spawn_result.success, "errors: {:?}", spawn_result.errors);
    assert!(worker_result.success, "errors: {:?}", worker_result.errors);
    assert_eq!(read(&spawned, "build/a.txt"), "HI");
    assert_eq!(read(&worked, "build/a.txt"), "HI");
    // Indistinguishable: same provenance, and the liveness signal still fires.
    assert!(worker_result.process_results.iter().all(|r| r.source == "process"));
    assert_eq!(started.labels(), vec!["upper a"]);
    // The transported result carries the tool's real output, like a spawn's.
    assert_eq!(worker_result.process_results[0].stdout, spawn_result.process_results[0].stdout);
}

#[test]
fn worker_result_is_cacheable() {
    let base = scratch("cacheable");
    let cache = base.join("cache");
    let (first, second) = (base.join("one"), base.join("two"));
    for root in [&first, &second] {
        workspace(root);
    }

    let spec = upper_spec(Some("tools/exec_worker.py"));
    assert!(build_cached(&first, &cache, &spec, &NullReporter).success);

    let started = Started::default();
    let result = build_cached(&second, &cache, &spec, &started);
    assert!(result.success, "errors: {:?}", result.errors);
    assert!(started.labels().is_empty()); // cache hit: no worker dispatched
    assert!(result.process_results.iter().all(|r| r.source == "cache"));
    assert_eq!(read(&second, "build/a.txt"), "HI");
}

#[test]
fn worker_crash_is_uncacheable_failure() {
    let root = scratch("crash");
    workspace(&root);
    let result = build(&root, &upper_spec(Some("tools/crash_worker.py")), &NullReporter);
    assert!(!result.success);
    assert_eq!(result.process_results.len(), 1);
    let r = &result.process_results[0];
    // A crash is not the action's deterministic answer: exit_code None is what
    // keeps the cache layer from ever storing it (same rule as a signal kill).
    assert_eq!(r.exit_code, None);
    assert!(r.stderr.contains("crashed"), "stderr: {}", r.stderr);
}

#[test]
fn worker_startup_failure_no_spawn_fallback() {
    let root = scratch("startup");
    workspace(&root);
    let spec = serde_json::json!({
        "version": 1,
        "actions": [{
            "kind": "process",
            "name": "x",
            "command_line": "{shell} {tool} {src} {dst}",
            "inputs": {
                "shell": [{"kind": "tool", "name": "sh"}],
                "tool": [{"kind": "file", "path": "tools/upper.sh"}],
                "src": [{"kind": "file", "path": "src/a.txt"}]
            },
            "outputs": {"dst": [{"kind": "file", "path": "build/a.txt"}]},
            "worker": {
                "name": "missing",
                "command_line": "{prog}",
                "inputs": {"prog": [{"kind": "tool", "name": "/nonexistent/brix-no-such-worker"}]}
            }
        }]
    })
    .to_string();
    let result = build(&root, &spec, &NullReporter);
    assert!(!result.success);
    assert_eq!(result.process_results.len(), 1);
    let r = &result.process_results[0];
    assert_eq!(r.exit_code, None);
    assert!(r.stderr.contains("failed to start"), "stderr: {}", r.stderr);
    assert!(!root.join("build/a.txt").exists()); // no quiet fallback to spawning
}

#[test]
fn worker_identity_joins_the_fingerprint() {
    let root = scratch("fingerprint");
    workspace(&root);
    write(&root, "tools/alt_worker.py", "import sys\nsys.stdin.readline()\n");
    let ctx = NodeContext::for_root(root.clone());
    let det = TimeAndHashChangeDetector::new();

    let fp = |spec_json: &str| {
        let spec = brix_core::spec::parse(spec_json).unwrap();
        action_fingerprint(&spec.actions[0], &ctx, &det).unwrap()
    };

    let no_worker = fp(&upper_spec(None));
    let with_worker = fp(&upper_spec(Some("tools/exec_worker.py")));
    assert_ne!(no_worker, with_worker); // the worker section changes the key

    let alt = fp(&upper_spec(Some("tools/alt_worker.py")));
    assert_ne!(with_worker, alt); // a different worker jar invalidates too
}
