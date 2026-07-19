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
//!
//! Cross-build daemon (docs/java-fast-builds.md v2, Rust engine only): with
//! a [`DaemonConfig`], workers run behind `brix-workerd` holders that
//! outlive the build. A worker here is then a Unix-socket session with its
//! holder: `check_out` adopts an idle holder for the spec's *worker
//! fingerprint* (or spawns a fresh one), and build end *releases* sessions
//! instead of terminating. The holder enforces the idle timeout and the
//! poison rule (a session dropped without a clean release kills the worker —
//! mid-request state is unknowable), so crash semantics stay v1's exactly.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::actions::{Action, WorkerSpec};
use crate::context::NodeContext;
use crate::detector::TimeAndHashChangeDetector;
use crate::process::ProcessResult;
use crate::render::render_command;

/// Must match brix-workerd's greeting/release lines byte-for-byte.
const GREETING: &str = "{\"brix_workerd\": \"ready\"}";
const RELEASE: &str = "{\"brix_workerd\": \"release\"}\n";

/// How workers persist across builds. Absent = v1: direct pipes, all
/// terminated at build end.
#[derive(Clone)]
pub struct DaemonConfig {
    /// Registry root; holders live under `<dir>/<worker-fingerprint>/`.
    pub registry_dir: PathBuf,
    /// Seconds a holder survives with no connected build. The engine never
    /// constructs a `DaemonConfig` with 0 — that value means "no daemon".
    pub idle_timeout: u64,
    /// The brix-workerd binary.
    pub workerd: PathBuf,
    /// Fingerprints the worker's launch identity (registry key).
    pub detector: TimeAndHashChangeDetector,
}

/// One live worker: either a direct child (v1) or a socket session with a
/// brix-workerd holder. Not thread-safe: a pool lends it to one thread at a
/// time (one request in flight).
enum Transport {
    Child {
        child: Child,
        stdin: Option<ChildStdin>,
        stdout: BufReader<ChildStdout>,
    },
    Daemon {
        stream: UnixStream,
        reader: BufReader<UnixStream>,
        stderr_log: PathBuf,
    },
}

pub struct Worker {
    transport: Transport,
    next_id: u64,
}

enum Connect {
    Adopted(Worker),
    Busy,
    Stale,
}

impl Worker {
    fn spawn(argv: &[String], cwd: &Path) -> Result<Worker, String> {
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
        Ok(Worker {
            transport: Transport::Child { child, stdin: Some(stdin), stdout },
            next_id: 0,
        })
    }

    /// A session with the holder at `socket`: `Stale` cleans up the holder's
    /// files (dead after a reboot or crash); `Busy` leaves it alone. A probe
    /// that times out sends the release line before hanging up, so the
    /// holder reads it as a clean no-op session instead of a poisoning
    /// disconnect.
    fn connect(socket: &Path) -> Connect {
        let mut stream = match UnixStream::connect(socket) {
            Ok(stream) => stream,
            Err(_) => {
                let _ = std::fs::remove_file(socket);
                let _ = std::fs::remove_file(socket.with_extension("pid"));
                let _ = std::fs::remove_file(socket.with_extension("stderr"));
                return Connect::Stale;
            }
        };
        let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
        let Ok(clone) = stream.try_clone() else { return Connect::Busy };
        let mut reader = BufReader::new(clone);
        let mut greeting = String::new();
        let greeted =
            reader.read_line(&mut greeting).is_ok() && greeting.trim() == GREETING;
        if !greeted {
            let _ = stream.write_all(RELEASE.as_bytes()); // busy: defuse the probe
            return Connect::Busy;
        }
        let _ = stream.set_read_timeout(None);
        Connect::Adopted(Worker {
            transport: Transport::Daemon {
                stream,
                reader,
                stderr_log: socket.with_extension("stderr"),
            },
            next_id: 0,
        })
    }

    /// One protocol round-trip. `Err` means the worker died or spoke garbage
    /// mid-request (Python's `WorkerCrashed`).
    fn request(&mut self, args: &[String], cwd: &str) -> Result<Value, String> {
        self.next_id += 1;
        let rid = self.next_id;
        let line = json!({"id": rid, "args": args, "cwd": cwd}).to_string();
        let round_trip = (|| {
            let mut response = String::new();
            match &mut self.transport {
                Transport::Child { stdin, stdout, .. } => {
                    let stdin = stdin.as_mut().expect("stdin open until terminate");
                    stdin.write_all(line.as_bytes())?;
                    stdin.write_all(b"\n")?;
                    stdin.flush()?;
                    stdout.read_line(&mut response)?;
                }
                Transport::Daemon { stream, reader, .. } => {
                    stream.write_all(line.as_bytes())?;
                    stream.write_all(b"\n")?;
                    stream.flush()?;
                    reader.read_line(&mut response)?;
                }
            }
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

    /// Best-effort stderr of a crashed worker. Direct child: kill first so
    /// reading the pipe cannot block. Daemon: the holder logs the worker's
    /// stderr to a file; read what is there.
    fn crash_detail(&mut self) -> String {
        match &mut self.transport {
            Transport::Child { .. } => {
                self.terminate();
                let Transport::Child { child, .. } = &mut self.transport else {
                    unreachable!()
                };
                let mut detail = String::new();
                if let Some(stderr) = child.stderr.take() {
                    let mut reader = BufReader::new(stderr);
                    let _ = reader.read_to_string(&mut detail);
                }
                detail
            }
            Transport::Daemon { stderr_log, .. } => {
                std::fs::read_to_string(&*stderr_log).unwrap_or_default()
            }
        }
    }

    /// Stop this worker for good. Child: close stdin (a protocol worker's
    /// natural stop), SIGTERM, SIGKILL after a grace period. Daemon: hang up
    /// *without* the release line — the holder's poison rule kills the
    /// worker and cleans the registry entry.
    fn terminate(&mut self) {
        match &mut self.transport {
            Transport::Child { child, stdin, .. } => {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    return;
                }
                drop(stdin.take());
                #[cfg(unix)]
                unsafe {
                    libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
                }
                let deadline = Instant::now() + Duration::from_secs(5);
                loop {
                    match child.try_wait() {
                        Ok(Some(_)) => return,
                        Ok(None) if Instant::now() < deadline => {
                            std::thread::sleep(Duration::from_millis(20));
                        }
                        _ => break,
                    }
                }
                let _ = child.kill();
                let _ = child.wait();
            }
            Transport::Daemon { .. } => {
                // Nothing to do here: dropping the socket without the
                // release line IS the kill (the holder's poison rule).
            }
        }
    }

    /// End the session keeping the worker warm (daemon), or terminate (v1
    /// child — with nothing to persist into, release means stop).
    fn release(&mut self) {
        match &mut self.transport {
            Transport::Child { .. } => self.terminate(),
            Transport::Daemon { stream, .. } => {
                let _ = stream.write_all(RELEASE.as_bytes());
                let _ = stream.flush();
            }
        }
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        // Safety net: a worker dropped without an explicit terminate/release
        // (e.g. a panicking layer unwound past a checked-out worker) must not
        // leak a live process past the build. For a daemon session the plain
        // socket drop is the safe default: the holder treats it as poison
        // and kills the worker.
        if let Transport::Child { .. } = self.transport {
            self.terminate();
        }
    }
}

/// Live workers of one spec. Lends an idle worker or starts a new one; a
/// crashed worker is discarded, never returned to the idle set.
struct WorkerPool {
    spec: WorkerSpec,
    idle: Mutex<Vec<Worker>>,
    daemon: Option<DaemonConfig>,
    /// The spec's registry directory, computed on first checkout (worker
    /// inputs exist by then — they are dependency edges).
    fingerprint_dir: OnceLock<PathBuf>,
}

static SOCKET_COUNTER: AtomicU64 = AtomicU64::new(0);

impl WorkerPool {
    fn new(spec: WorkerSpec, daemon: Option<DaemonConfig>) -> WorkerPool {
        WorkerPool { spec, idle: Mutex::new(Vec::new()), daemon, fingerprint_dir: OnceLock::new() }
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
            env_unset: Vec::new(),
            depfile: None,
            worker: None,
        };
        crate::shlex::split(&render_command(&launcher, ctx)?)
    }

    /// The spec's registry directory. Unix socket paths are length-limited
    /// (~108 bytes), so the directory carries a 16-hex prefix of the worker
    /// fingerprint with the full value in a `fingerprint` file — verified on
    /// use, so a (astronomically unlikely) prefix collision or a foreign
    /// directory can never hand out another spec's worker.
    fn registry_dir(&self, daemon: &DaemonConfig, ctx: &NodeContext) -> Result<PathBuf, String> {
        if let Some(dir) = self.fingerprint_dir.get() {
            return Ok(dir.clone());
        }
        let fp = crate::content::worker_fingerprint(&self.spec, ctx, &daemon.detector)?;
        let dir = daemon.registry_dir.join(&fp[..16]);
        let marker = dir.join("fingerprint");
        match std::fs::read_to_string(&marker) {
            Ok(existing) if existing.trim() == fp => {}
            Ok(_) => {
                // Prefix collision or leftovers from another spec: clear it.
                let _ = std::fs::remove_dir_all(&dir);
            }
            Err(_) => {}
        }
        std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {dir:?}: {e}"))?;
        std::fs::write(&marker, &fp).map_err(|e| format!("cannot write {marker:?}: {e}"))?;
        let _ = self.fingerprint_dir.set(dir.clone());
        Ok(dir)
    }

    /// Adopt an idle holder from the registry, if any answers. Registry
    /// entries are *pointer files*: `<n>.sock` under the registry dir holds
    /// the real socket path, which lives in the temp dir — Unix socket paths
    /// are length-limited (~108 bytes) and workspaces can be arbitrarily
    /// deep, so the socket itself must stay short.
    fn adopt(dir: &Path) -> Option<Worker> {
        let entries = std::fs::read_dir(dir).ok()?;
        let mut pointers: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|ext| ext == "sock"))
            .collect();
        pointers.sort();
        for pointer in pointers {
            let Ok(socket) = std::fs::read_to_string(&pointer) else { continue };
            match Worker::connect(Path::new(socket.trim())) {
                Connect::Adopted(worker) => return Some(worker),
                Connect::Busy => {}
                Connect::Stale => {
                    let _ = std::fs::remove_file(&pointer);
                }
            }
        }
        None
    }

    /// Spawn a fresh brix-workerd holder and connect to it.
    fn spawn_daemon(
        &self,
        daemon: &DaemonConfig,
        dir: &Path,
        ctx: &NodeContext,
    ) -> Result<Worker, String> {
        let argv = self.launch_argv(ctx)?;
        if argv.is_empty() {
            return Err("empty worker command line".to_string());
        }
        let n = SOCKET_COUNTER.fetch_add(1, Ordering::Relaxed);
        // Short real socket path (SUN_LEN limits), pointed to from the
        // registry so any build can find it.
        let socket = std::env::temp_dir().join(format!(
            "brix-wd-{}-{n}.sock",
            std::process::id()
        ));
        let pointer = dir.join(format!("{}-{n}.sock", std::process::id()));
        let output = Command::new(&daemon.workerd)
            .arg("--socket")
            .arg(&socket)
            .arg("--idle-timeout")
            .arg(daemon.idle_timeout.to_string())
            .arg("--stderr-log")
            .arg(socket.with_extension("stderr"))
            .arg("--cwd")
            .arg(&ctx.workspace_root)
            .arg("--")
            .args(&argv)
            .output()
            .map_err(|e| format!("cannot spawn {}: {e}", daemon.workerd.display()))?;
        if !output.status.success() {
            let mut err = String::from_utf8_lossy(&output.stderr).trim().to_string();
            if err.is_empty() {
                err = std::fs::read_to_string(socket.with_extension("err")).unwrap_or_default();
            }
            return Err(format!("brix-workerd failed to start: {err}"));
        }
        std::fs::write(&pointer, socket.to_string_lossy().as_bytes())
            .map_err(|e| format!("cannot write {pointer:?}: {e}"))?;
        // The holder binds before detaching, so the socket is live the
        // moment its foreground parent exits; one connect suffices, with a
        // short retry for filesystem visibility.
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match Worker::connect(&socket) {
                Connect::Adopted(worker) => return Ok(worker),
                Connect::Busy | Connect::Stale => {}
            }
            if Instant::now() >= deadline {
                let _ = std::fs::remove_file(&pointer);
                return Err(format!(
                    "worker '{}' daemon did not answer at {}",
                    self.spec.name,
                    socket.display()
                ));
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    /// `Err` is a startup failure (Python's `WorkerStartupError`).
    fn check_out(&self, ctx: &NodeContext) -> Result<Worker, String> {
        if let Some(worker) = self.idle.lock().unwrap().pop() {
            return Ok(worker);
        }
        match &self.daemon {
            None => Worker::spawn(&self.launch_argv(ctx)?, &ctx.workspace_root),
            Some(daemon) => {
                let dir = self.registry_dir(daemon, ctx)?;
                if let Some(worker) = Self::adopt(&dir) {
                    return Ok(worker);
                }
                self.spawn_daemon(daemon, &dir, ctx)
            }
        }
    }

    fn check_in(&self, worker: Worker) {
        self.idle.lock().unwrap().push(worker);
    }

    fn discard(&self, mut worker: Worker) {
        worker.terminate();
    }

    /// Build end: release sessions (daemon workers stay warm; v1 children
    /// terminate — release degrades to terminate for them).
    fn release_all(&self) {
        let mut workers = std::mem::take(&mut *self.idle.lock().unwrap());
        for worker in &mut workers {
            worker.release();
        }
    }
}

/// Build-scoped: one pool per worker spec. Owned by the scheduler; layers
/// never see it (workers are pure transport). Without a daemon config all
/// workers die at build end (v1); with one, build end releases them to
/// their brix-workerd holders.
pub struct WorkerRegistry {
    pools: Mutex<HashMap<String, Arc<WorkerPool>>>,
    daemon: Option<DaemonConfig>,
}

impl Default for WorkerRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl WorkerRegistry {
    pub fn new() -> WorkerRegistry {
        WorkerRegistry { pools: Mutex::new(HashMap::new()), daemon: None }
    }

    pub fn with_daemon(config: DaemonConfig) -> WorkerRegistry {
        WorkerRegistry { pools: Mutex::new(HashMap::new()), daemon: Some(config) }
    }

    fn pool(&self, spec: &WorkerSpec) -> Arc<WorkerPool> {
        let key = format!("{spec:?}");
        Arc::clone(
            self.pools
                .lock()
                .unwrap()
                .entry(key)
                .or_insert_with(|| Arc::new(WorkerPool::new(spec.clone(), self.daemon.clone()))),
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

    /// Build end. The name is historical (v1 terminated everything); with a
    /// daemon config this *releases* workers to their holders instead.
    pub fn terminate_all(&self) {
        let pools = std::mem::take(&mut *self.pools.lock().unwrap());
        for pool in pools.values() {
            pool.release_all();
        }
    }
}
