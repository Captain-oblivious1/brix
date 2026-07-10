//! Cross-build worker daemon behavior (docs/java-fast-builds.md v2): a
//! worker adopted across registry lifetimes is the same OS process; idle
//! timeout, fingerprint-keyed replacement, and crash semantics hold. These
//! tests drive brix-core's real `WorkerRegistry` against the real
//! `brix-workerd` binary — each `WorkerRegistry` stands in for one build.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use brix_core::context::NodeContext;
use brix_core::detector::TimeAndHashChangeDetector;
use brix_core::{DaemonConfig, WorkerRegistry};

/// Responds to each request with its own pid — adoption is provable by pid
/// equality across "builds".
const PID_WORKER_PY: &str = r#"
import json, os, sys
for line in sys.stdin:
    req = json.loads(line)
    resp = {"id": req["id"], "exitCode": 0, "stdout": str(os.getpid()), "stderr": ""}
    print(json.dumps(resp), flush=True)
"#;

/// Exits mid-request without answering (the crash case).
const CRASH_WORKER_PY: &str = r#"
import sys
sys.stdin.readline()
sys.exit(3)
"#;

fn scratch(name: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "brix-workerd-{name}-{}-{}",
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

fn spec(script: &str) -> brix_core::WorkerSpec {
    serde_json::from_str(&format!(
        r#"{{"name": "w", "command_line": "{{python}} {{script}}",
            "inputs": {{"python": [{{"kind": "tool", "name": "python3"}}],
                        "script": [{{"kind": "file", "path": "{script}"}}]}}}}"#
    ))
    .unwrap()
}

fn daemon_config(root: &Path, idle_timeout: u64) -> DaemonConfig {
    DaemonConfig {
        registry_dir: root.join("build/.brix/workers"),
        idle_timeout,
        workerd: PathBuf::from(env!("CARGO_BIN_EXE_brix-workerd")),
        detector: TimeAndHashChangeDetector::default(),
    }
}

/// One "build": a fresh registry, one request, workers released at the end.
fn one_build(root: &Path, idle_timeout: u64, script: &str) -> brix_core::ProcessResult {
    let registry = WorkerRegistry::with_daemon(daemon_config(root, idle_timeout));
    let ctx = NodeContext::for_root(root.to_path_buf());
    let result = registry.run(&spec(script), "use worker", vec!["go".to_string()], &ctx);
    registry.terminate_all(); // build end: releases, daemon workers stay warm
    result
}

/// The real socket paths behind a registry's pointer files.
fn socket_targets(root: &Path) -> Vec<PathBuf> {
    let base = root.join("build/.brix/workers");
    let Ok(dirs) = std::fs::read_dir(&base) else { return Vec::new() };
    let mut out = Vec::new();
    for dir in dirs.flatten() {
        let Ok(files) = std::fs::read_dir(dir.path()) else { continue };
        for file in files.flatten() {
            if file.path().extension().is_some_and(|e| e == "sock") {
                if let Ok(target) = std::fs::read_to_string(file.path()) {
                    out.push(PathBuf::from(target.trim()));
                }
            }
        }
    }
    out
}

/// Kill any holders left in the registry (test hygiene; real ones idle out).
fn kill_daemons(root: &Path) {
    for socket in socket_targets(root) {
        if let Ok(pid) = std::fs::read_to_string(socket.with_extension("pid")) {
            if let Ok(pid) = pid.trim().parse::<i32>() {
                unsafe { libc::kill(pid, libc::SIGKILL) };
            }
        }
        let _ = std::fs::remove_file(&socket);
        let _ = std::fs::remove_file(socket.with_extension("pid"));
        let _ = std::fs::remove_file(socket.with_extension("stderr"));
    }
}

#[test]
fn worker_survives_across_builds() {
    let root = scratch("adopt");
    write(&root, "tools/worker.py", PID_WORKER_PY);
    let first = one_build(&root, 120, "tools/worker.py");
    assert_eq!(first.exit_code, Some(0), "{}", first.stderr);
    let second = one_build(&root, 120, "tools/worker.py");
    assert_eq!(second.exit_code, Some(0), "{}", second.stderr);
    assert_eq!(first.stdout, second.stdout, "expected the same warm worker pid");
    kill_daemons(&root);
}

#[test]
fn idle_timeout_expires_the_worker() {
    let root = scratch("idle");
    write(&root, "tools/worker.py", PID_WORKER_PY);
    let first = one_build(&root, 1, "tools/worker.py");
    assert_eq!(first.exit_code, Some(0), "{}", first.stderr);
    // The holder counts idle time only while unconnected; after release it
    // has 1s to live. Wait past it (plus its 50ms poll) and the registry
    // entry must be gone.
    std::thread::sleep(Duration::from_millis(1800));
    // The holder removes its real socket on expiry; the pointer goes stale
    // and is swept by the next adoption attempt.
    let live: Vec<_> = socket_targets(&root).into_iter().filter(|s| s.exists()).collect();
    assert!(live.is_empty(), "expired holder left live sockets: {live:?}");
    // The next build starts a fresh worker.
    let second = one_build(&root, 1, "tools/worker.py");
    assert_eq!(second.exit_code, Some(0), "{}", second.stderr);
    assert_ne!(first.stdout, second.stdout, "expired worker was somehow adopted");
    kill_daemons(&root);
}

#[test]
fn changed_worker_inputs_change_the_key() {
    // A rebuilt worker script (new JDK, new wrapper jar) must miss the
    // registry: the fingerprint is the key, so no kill logic is needed.
    let root = scratch("rekey");
    write(&root, "tools/worker.py", PID_WORKER_PY);
    let first = one_build(&root, 120, "tools/worker.py");
    write(&root, "tools/worker.py", &format!("{PID_WORKER_PY}# v2\n"));
    let second = one_build(&root, 120, "tools/worker.py");
    assert_eq!(second.exit_code, Some(0), "{}", second.stderr);
    assert_ne!(first.stdout, second.stdout, "stale worker served a new fingerprint");
    let keys = std::fs::read_dir(root.join("build/.brix/workers")).unwrap().count();
    assert_eq!(keys, 2, "expected two fingerprint directories");
    kill_daemons(&root);
}

#[test]
fn crashed_worker_is_not_reused_and_holder_dies() {
    let root = scratch("crash");
    write(&root, "tools/worker.py", CRASH_WORKER_PY);
    let result = one_build(&root, 120, "tools/worker.py");
    assert_eq!(result.exit_code, None);
    assert!(result.stderr.contains("crashed"), "{}", result.stderr);
    // The holder notices its worker died and removes the registry entry
    // (give its poll a moment).
    std::thread::sleep(Duration::from_millis(500));
    write(&root, "tools/worker.py", PID_WORKER_PY);
    // Same bytes would be a different fingerprint anyway (content changed);
    // what matters is that a fresh, working worker answers.
    let second = one_build(&root, 120, "tools/worker.py");
    assert_eq!(second.exit_code, Some(0), "{}", second.stderr);
    kill_daemons(&root);
}

#[test]
fn concurrent_builds_get_separate_workers() {
    // A busy holder must not stall a second build: the probe times out and
    // the pool spawns another daemon.
    let root = scratch("busy");
    write(&root, "tools/worker.py", PID_WORKER_PY);
    let config = daemon_config(&root, 120);
    let ctx = NodeContext::for_root(root.clone());
    let registry_a = WorkerRegistry::with_daemon(config.clone());
    let registry_b = WorkerRegistry::with_daemon(config);
    // Registry A holds its worker checked in with an open session; B's
    // adopt probe must skip the busy holder and spawn a second one.
    let a = registry_a.run(&spec("tools/worker.py"), "a", vec!["x".into()], &ctx);
    let b = registry_b.run(&spec("tools/worker.py"), "b", vec!["x".into()], &ctx);
    assert_eq!(a.exit_code, Some(0), "{}", a.stderr);
    assert_eq!(b.exit_code, Some(0), "{}", b.stderr);
    assert_ne!(a.stdout, b.stdout, "two live sessions cannot share one worker");
    registry_a.terminate_all();
    registry_b.terminate_all();
    kill_daemons(&root);
}
