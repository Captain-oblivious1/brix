//! Persistent workers: a long-lived process that executes many actions.
//! Mirrors `src/brix/workers.py` — see that module's docs for the protocol
//! (JSON, one object per line, over the worker's stdin/stdout) and the v1
//! rules (one request in flight per worker process; stateless between
//! requests; the scheduler owns lifecycles).
//!
//! Do-not-reinterpret notes:
//! - A worker crash or protocol garbage fails the in-flight request with
//!   `exit_code: None` and a `brix:` stderr — like a signal kill, it is not
//!   the action's deterministic answer, so it is never cached; the worker is
//!   discarded, not reused.
//! - A worker that fails to start fails every request the same way. There is
//!   **no** silent fallback to spawning — a misconfigured worker must be
//!   loud, not quietly slow.
//! - Pools are keyed by the spec's *content* (Python keys by object identity;
//!   across the JSON boundary every node carries a copy of its spec, so
//!   structural identity is the faithful translation — one lowered spec, one
//!   pool).

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::actions::{Action, WorkerSpec};
use crate::context::NodeContext;
use crate::process::ProcessResult;
use crate::render::render_command;

/// One live worker process. Not thread-safe: a pool lends it to one thread
/// at a time (one request in flight).
pub struct Worker {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl Worker {
    fn new(argv: &[String], cwd: &Path) -> Result<Worker, String> {
        if argv.is_empty() {
            return Err("empty worker command line".to_string());
        }
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|exc| exc.to_string())?;
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = BufReader::new(child.stdout.take().expect("piped stdout"));
        Ok(Worker { child, stdin: Some(stdin), stdout, next_id: 0 })
    }

    /// One protocol round-trip. `Err` means the worker died or spoke garbage
    /// mid-request (Python's `WorkerCrashed`).
    fn request(&mut self, args: &[String], cwd: &str) -> Result<Value, String> {
        self.next_id += 1;
        let rid = self.next_id;
        let line = json!({"id": rid, "args": args, "cwd": cwd}).to_string();
        let round_trip = (|| {
            let stdin = self.stdin.as_mut().expect("stdin open until terminate");
            stdin.write_all(line.as_bytes())?;
            stdin.write_all(b"\n")?;
            stdin.flush()?;
            let mut response = String::new();
            self.stdout.read_line(&mut response)?;
            Ok::<String, std::io::Error>(response)
        })();
        let response = round_trip.map_err(|exc| format!("write/read failed: {exc}"))?;
        if response.is_empty() {
            // EOF — the worker exited without answering.
            return Err("worker exited without responding".to_string());
        }
        let message: Value = serde_json::from_str(&response)
            .map_err(|exc| format!("non-JSON protocol line {response:?}: {exc}"))?;
        if message.get("id").and_then(Value::as_u64) != Some(rid) {
            return Err(format!("response id {:?} != request {rid}", message.get("id")));
        }
        Ok(message)
    }

    /// Best-effort stderr of a crashed worker; the process is killed first so
    /// reading its stderr pipe cannot block.
    fn crash_detail(&mut self) -> String {
        self.terminate();
        let mut detail = String::new();
        if let Some(stderr) = self.child.stderr.take() {
            let mut reader = BufReader::new(stderr);
            let _ = reader.read_to_string(&mut detail);
        }
        detail
    }

    fn terminate(&mut self) {
        if matches!(self.child.try_wait(), Ok(Some(_))) {
            return;
        }
        // Close stdin (a protocol worker's natural stop signal), then
        // SIGTERM, then SIGKILL after a grace period — Python's
        // close/terminate/wait(5)/kill sequence.
        drop(self.stdin.take());
        #[cfg(unix)]
        unsafe {
            libc::kill(self.child.id() as libc::pid_t, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                _ => break,
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        // Safety net: a worker dropped without an explicit terminate (e.g. a
        // panicking layer unwound past a checked-out worker) must not leak a
        // live process past the build.
        self.terminate();
    }
}

/// Live workers of one spec. Lends an idle worker or starts a new one; a
/// crashed worker is discarded, never returned to the idle set.
struct WorkerPool {
    spec: WorkerSpec,
    idle: Mutex<Vec<Worker>>,
}

impl WorkerPool {
    fn new(spec: WorkerSpec) -> WorkerPool {
        WorkerPool { spec, idle: Mutex::new(Vec::new()) }
    }

    /// Render the launch argv exactly as a spawned action's command renders
    /// (tool resolution and overlay-resolved input paths included — the
    /// worker jar may be a staged build product).
    fn launch_argv(&self, ctx: &NodeContext) -> Result<Vec<String>, String> {
        let launcher = Action::Process {
            name: Some(self.spec.name.clone()),
            command_line: self.spec.command_line.clone(),
            inputs: self.spec.inputs.clone(),
            outputs: Vec::new(),
            env: Vec::new(),
            depfile: None,
            worker: None,
        };
        crate::shlex::split(&render_command(&launcher, ctx)?)
    }

    /// `Err` is a startup failure (Python's `WorkerStartupError`).
    fn check_out(&self, ctx: &NodeContext) -> Result<Worker, String> {
        if let Some(worker) = self.idle.lock().unwrap().pop() {
            return Ok(worker);
        }
        Worker::new(&self.launch_argv(ctx)?, &ctx.workspace_root)
    }

    fn check_in(&self, worker: Worker) {
        self.idle.lock().unwrap().push(worker);
    }

    fn discard(&self, mut worker: Worker) {
        worker.terminate();
    }

    fn terminate_all(&self) {
        let mut workers = std::mem::take(&mut *self.idle.lock().unwrap());
        for worker in &mut workers {
            worker.terminate();
        }
    }
}

/// Build-scoped: one pool per worker spec, all torn down at build end. Owned
/// by the scheduler; layers never see it (workers are pure transport).
pub struct WorkerRegistry {
    pools: Mutex<HashMap<String, Arc<WorkerPool>>>,
}

impl Default for WorkerRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl WorkerRegistry {
    pub fn new() -> WorkerRegistry {
        WorkerRegistry { pools: Mutex::new(HashMap::new()) }
    }

    fn pool(&self, spec: &WorkerSpec) -> Arc<WorkerPool> {
        let key = format!("{spec:?}");
        Arc::clone(
            self.pools
                .lock()
                .unwrap()
                .entry(key)
                .or_insert_with(|| Arc::new(WorkerPool::new(spec.clone()))),
        )
    }

    /// Dispatch one rendered request to the spec's pool. Mirrors
    /// `WorkerRegistry.run`: failure results carry `exit_code: None` (never
    /// cached) and there is no fallback to spawning.
    pub fn run(
        &self,
        spec: &WorkerSpec,
        action_label: &str,
        args: Vec<String>,
        ctx: &NodeContext,
    ) -> ProcessResult {
        let pool = self.pool(spec);
        let command = args.join(" ");
        let mut worker = match pool.check_out(ctx) {
            Ok(worker) => worker,
            Err(exc) => {
                return ProcessResult {
                    action_name: action_label.to_string(),
                    command,
                    argv: args,
                    stderr: format!("brix: worker '{}' failed to start: {exc}", spec.name),
                    source: "process".to_string(),
                    ..Default::default()
                }
            }
        };
        let message = match worker.request(&args, &ctx.workspace_root.to_string_lossy()) {
            Ok(message) => message,
            Err(exc) => {
                let detail = worker.crash_detail();
                pool.discard(worker);
                let mut stderr = format!("brix: worker '{}' crashed: {exc}", spec.name);
                if !detail.is_empty() {
                    stderr.push('\n');
                    stderr.push_str(&detail);
                }
                return ProcessResult {
                    action_name: action_label.to_string(),
                    command,
                    argv: args,
                    stderr,
                    source: "process".to_string(),
                    ..Default::default()
                };
            }
        };
        pool.check_in(worker);
        let text = |key: &str| {
            message.get(key).and_then(Value::as_str).unwrap_or_default().to_string()
        };
        ProcessResult {
            action_name: action_label.to_string(),
            command,
            argv: args,
            exit_code: message.get("exitCode").and_then(Value::as_i64).map(|c| c as i32),
            signal: None,
            stdout: text("stdout"),
            stderr: text("stderr"),
            source: "process".to_string(),
        }
    }

    pub fn terminate_all(&self) {
        let pools = std::mem::take(&mut *self.pools.lock().unwrap());
        for pool in pools.values() {
            pool.terminate_all();
        }
    }
}
