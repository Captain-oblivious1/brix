//! brix-workerd: the cross-build worker holder (docs/java-fast-builds.md v2).
//!
//! The build process exits; this small daemon is what keeps a persistent
//! worker's JVM warm between builds. It detaches, spawns the real worker on
//! stdin/stdout pipes (stderr to a log file), listens on a Unix socket, and
//! bridges **one client at a time** to the worker's pipes. The JSON-line
//! protocol passes through untouched — the holder is transport plumbing, not
//! a participant.
//!
//! Session contract with the engine (`brix-core/src/workers.rs`):
//! - On accept, the holder sends one greeting line
//!   `{"brix_workerd": "ready"}`. A connect that never sees the greeting is
//!   a busy or dead holder — the engine moves on. (A queued connection on a
//!   busy listener would otherwise look accepted.)
//! - A client line `{"brix_workerd": "release"}` ends the session cleanly:
//!   the worker stays alive and the holder returns to accepting.
//! - A client that disconnects *without* the release line (engine crash,
//!   panic, discarded-after-garbage worker) poisons the worker: mid-request
//!   state is unknowable, so the holder kills the worker and exits.
//! - The worker exiting (or EOF on its stdout) at any point ends everything:
//!   close the client, remove the registry entry, exit.
//! - No client for `--idle-timeout` seconds (counted only while
//!   unconnected; a long build holding a session never expires) kills the
//!   worker and exits. 0 never expires — but the engine never spawns a
//!   holder with 0; that value means "no daemon at all".
//!
//! Usage:
//!   brix-workerd --socket <path> --idle-timeout <secs> --stderr-log <path>
//!                --cwd <dir> -- <worker argv...>

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const GREETING: &str = "{\"brix_workerd\": \"ready\"}\n";
const RELEASE: &str = "{\"brix_workerd\": \"release\"}";

struct Args {
    socket: PathBuf,
    idle_timeout: u64,
    stderr_log: PathBuf,
    cwd: PathBuf,
    worker_argv: Vec<String>,
}

fn parse_args(mut argv: impl Iterator<Item = String>) -> Result<Args, String> {
    let (mut socket, mut idle, mut log, mut cwd) = (None, None, None, None);
    let mut worker_argv = Vec::new();
    while let Some(arg) = argv.next() {
        let mut value = |name: &str| argv.next().ok_or(format!("{name} needs a value"));
        match arg.as_str() {
            "--socket" => socket = Some(PathBuf::from(value("--socket")?)),
            "--idle-timeout" => {
                idle = Some(value("--idle-timeout")?.parse().map_err(|e| format!("{e}"))?)
            }
            "--stderr-log" => log = Some(PathBuf::from(value("--stderr-log")?)),
            "--cwd" => cwd = Some(PathBuf::from(value("--cwd")?)),
            "--" => {
                worker_argv = argv.collect();
                break;
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    if worker_argv.is_empty() {
        return Err("no worker argv after --".to_string());
    }
    Ok(Args {
        socket: socket.ok_or("--socket is required")?,
        idle_timeout: idle.ok_or("--idle-timeout is required")?,
        stderr_log: log.ok_or("--stderr-log is required")?,
        cwd: cwd.ok_or("--cwd is required")?,
        worker_argv,
    })
}

pub fn run() {
    run_args(std::env::args().skip(1).collect());
}

/// Run with an explicit argv tail. The installed Python console script uses
/// this because the OS argv begins with the Python interpreter and wrapper;
/// the standalone Rust binary uses [`run`].
pub fn run_args(argv: Vec<String>) {
    let args = match parse_args(argv.into_iter()) {
        Ok(args) => args,
        Err(err) => {
            eprintln!("brix-workerd: {err}");
            std::process::exit(2);
        }
    };
    // Bind before detaching so the spawning engine can connect as soon as
    // our parent exits — no readiness race.
    let listener = match UnixListener::bind(&args.socket) {
        Ok(listener) => listener,
        Err(err) => {
            eprintln!("brix-workerd: cannot bind {}: {err}", args.socket.display());
            std::process::exit(2);
        }
    };
    detach();
    write_pidfile(&args.socket);
    let outcome = serve(&listener, &args);
    cleanup(&args.socket);
    if let Err(err) = outcome {
        let _ = std::fs::write(args.socket.with_extension("err"), err);
    }
}

/// Double-fork + setsid: survive the spawning build process and its session.
fn detach() {
    unsafe {
        if libc::fork() > 0 {
            libc::_exit(0); // original parent: the engine reaps this
        }
        libc::setsid();
        if libc::fork() > 0 {
            libc::_exit(0);
        }
        // Detach stdio; our diagnostics go to the .err file.
        let devnull = libc::open(c"/dev/null".as_ptr(), libc::O_RDWR);
        for fd in 0..3 {
            libc::dup2(devnull, fd);
        }
        if devnull > 2 {
            libc::close(devnull);
        }
    }
}

fn write_pidfile(socket: &Path) {
    let _ = std::fs::write(socket.with_extension("pid"), std::process::id().to_string());
}

fn cleanup(socket: &Path) {
    let _ = std::fs::remove_file(socket);
    let _ = std::fs::remove_file(socket.with_extension("pid"));
}

fn serve(listener: &UnixListener, args: &Args) -> Result<(), String> {
    let log = std::fs::File::create(&args.stderr_log)
        .map_err(|e| format!("cannot create stderr log: {e}"))?;
    let mut worker = Command::new(&args.worker_argv[0])
        .args(&args.worker_argv[1..])
        .current_dir(&args.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|e| format!("cannot spawn worker: {e}"))?;
    let mut worker_stdin = worker.stdin.take().expect("piped stdin");
    let mut worker_stdout = BufReader::new(worker.stdout.take().expect("piped stdout"));

    listener
        .set_nonblocking(true)
        .map_err(|e| format!("cannot set nonblocking: {e}"))?;
    let mut idle_since = Instant::now();
    loop {
        // Wait for a client, watching the worker and the idle clock.
        let client = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                    if worker_exited(&mut worker) {
                        return Ok(()); // worker died while idle: entry gone, we go too
                    }
                    if args.idle_timeout > 0
                        && idle_since.elapsed() >= Duration::from_secs(args.idle_timeout)
                    {
                        terminate(&mut worker, worker_stdin);
                        return Ok(());
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                Err(err) => return Err(format!("accept failed: {err}")),
            }
        };
        match session(client, &mut worker_stdin, &mut worker_stdout) {
            SessionEnd::Released => {
                idle_since = Instant::now();
            }
            SessionEnd::ClientVanished => {
                // No release line: the engine crashed or discarded this
                // worker mid-session; its state is unknowable. Kill it.
                terminate(&mut worker, worker_stdin);
                return Ok(());
            }
            SessionEnd::WorkerDied => {
                let _ = worker.wait();
                return Ok(());
            }
        }
    }
}

enum SessionEnd {
    Released,
    ClientVanished,
    WorkerDied,
}

/// Bridge one client to the worker's pipes, one request/response at a time
/// (the v1 protocol is strictly serial, so a serial bridge is faithful).
fn session(
    mut client: UnixStream,
    worker_stdin: &mut std::process::ChildStdin,
    worker_stdout: &mut BufReader<std::process::ChildStdout>,
) -> SessionEnd {
    if client.set_nonblocking(false).is_err() || client.write_all(GREETING.as_bytes()).is_err()
    {
        return SessionEnd::Released; // greeting failed: client already gone
    }
    let mut reader = BufReader::new(match client.try_clone() {
        Ok(clone) => clone,
        Err(_) => return SessionEnd::Released,
    });
    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) | Err(_) => return SessionEnd::ClientVanished,
            Ok(_) => {}
        }
        if line.trim() == RELEASE {
            return SessionEnd::Released;
        }
        if worker_stdin.write_all(line.as_bytes()).is_err() || worker_stdin.flush().is_err() {
            return SessionEnd::WorkerDied;
        }
        let mut response = String::new();
        match worker_stdout.read_line(&mut response) {
            Ok(0) | Err(_) => return SessionEnd::WorkerDied,
            Ok(_) => {}
        }
        if client.write_all(response.as_bytes()).is_err() {
            return SessionEnd::ClientVanished;
        }
    }
}

fn worker_exited(worker: &mut Child) -> bool {
    matches!(worker.try_wait(), Ok(Some(_)))
}

/// Close stdin (a protocol worker's natural stop), then SIGTERM, then
/// SIGKILL after a grace period — the same sequence the engine uses.
fn terminate(worker: &mut Child, stdin: std::process::ChildStdin) {
    drop(stdin);
    if worker_exited(worker) {
        return;
    }
    unsafe {
        libc::kill(worker.id() as libc::pid_t, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if worker_exited(worker) {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let _ = worker.kill();
    let _ = worker.wait();
}
