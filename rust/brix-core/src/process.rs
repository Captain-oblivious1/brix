//! Process execution and structured results. Mirrors `src/brix/process.py`.
//!
//! Do-not-reinterpret notes (docs/rust-port.md):
//! - A `RenderError`/bad-command result has `exit_code: None` (and is
//!   therefore never "ok" and never cached).
//! - Negative return code means killed-by-signal: `signal = -rc`,
//!   `exit_code = None`.
//! - `env` entries extend and shadow the inherited environment.

use std::path::Path;
use std::process::Command;

/// The captured result of one process invocation (or a failure to invoke).
#[derive(Debug, Clone, Default)]
pub struct ProcessResult {
    pub action_name: String,
    pub command: String,
    pub argv: Vec<String>,
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    /// Provenance: how this result was obtained ("process", "replayed",
    /// "cache"). Stamped by whatever produced the result. Scheduling never
    /// reads it; reporting may.
    pub source: String,
}

impl ProcessResult {
    /// A result that never ran (render error, bad command line): no exit
    /// code, only a diagnostic on stderr.
    pub fn did_not_run(action_name: &str, command: &str, stderr: String) -> Self {
        ProcessResult {
            action_name: action_name.to_string(),
            command: command.to_string(),
            stderr,
            source: "process".to_string(),
            ..Default::default()
        }
    }

    pub fn ok(&self) -> bool {
        self.exit_code == Some(0)
    }

    pub fn summary(&self) -> String {
        if self.ok() {
            return format!("ok: {}", self.action_name);
        }
        if let Some(sig) = self.signal {
            return format!("FAILED (signal {sig}): {}", self.action_name);
        }
        if let Some(code) = self.exit_code {
            return format!("FAILED (exit {code}): {}", self.action_name);
        }
        format!("FAILED (did not run): {}", self.action_name)
    }
}

/// Run a rendered command line, capturing stdout/stderr/exit status.
///
/// `env` entries extend (and shadow) the inherited environment. Output is
/// decoded as UTF-8 with replacement (Python uses strict locale decoding and
/// would crash the build on invalid bytes; lossy is the deliberate, gentler
/// divergence — invalid output bytes become U+FFFD instead of an engine
/// error).
pub fn run_process(
    action_name: &str,
    command: &str,
    cwd: &Path,
    env: &[(String, String)],
) -> ProcessResult {
    let argv = match crate::shlex::split(command) {
        Ok(argv) => argv,
        Err(exc) => {
            return ProcessResult::did_not_run(
                action_name,
                command,
                format!("brix: bad command line: {exc}"),
            )
        }
    };
    if argv.is_empty() {
        return ProcessResult::did_not_run(
            action_name,
            command,
            "brix: empty command line".to_string(),
        );
    }
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..]).current_dir(cwd);
    for (k, v) in env {
        cmd.env(k, v);
    }
    let output = match cmd.output() {
        Ok(o) => o,
        Err(exc) => {
            return ProcessResult {
                action_name: action_name.to_string(),
                command: command.to_string(),
                argv,
                stderr: format!("brix: cannot execute: {exc}"),
                source: "process".to_string(),
                ..Default::default()
            }
        }
    };
    // Python: rc >= 0 -> exit_code, rc < 0 -> signal = -rc. On Unix, an
    // absent status code means killed-by-signal.
    let (exit_code, signal) = decode_status(&output.status);
    ProcessResult {
        action_name: action_name.to_string(),
        command: command.to_string(),
        argv,
        exit_code,
        signal,
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        source: "process".to_string(),
    }
}

#[cfg(unix)]
fn decode_status(status: &std::process::ExitStatus) -> (Option<i32>, Option<i32>) {
    use std::os::unix::process::ExitStatusExt;
    match status.code() {
        Some(code) => (Some(code), None),
        None => (None, status.signal()),
    }
}

#[cfg(not(unix))]
fn decode_status(status: &std::process::ExitStatus) -> (Option<i32>, Option<i32>) {
    (status.code(), None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn cwd() -> PathBuf {
        std::env::current_dir().unwrap()
    }

    #[test]
    fn ok_run_captures_stdout() {
        let r = run_process("echo", "echo hello", &cwd(), &[]);
        assert_eq!(r.exit_code, Some(0));
        assert!(r.ok());
        assert_eq!(r.stdout, "hello\n");
        assert_eq!(r.summary(), "ok: echo");
    }

    #[test]
    fn nonzero_exit_is_failure() {
        let r = run_process("false", "false", &cwd(), &[]);
        assert_eq!(r.exit_code, Some(1));
        assert!(!r.ok());
        assert_eq!(r.summary(), "FAILED (exit 1): false");
    }

    #[test]
    fn empty_and_unsplittable_commands_never_ran() {
        let r = run_process("x", "", &cwd(), &[]);
        assert_eq!(r.exit_code, None);
        assert_eq!(r.stderr, "brix: empty command line");
        let r = run_process("x", "'unterminated", &cwd(), &[]);
        assert_eq!(r.exit_code, None);
        assert!(r.stderr.starts_with("brix: bad command line:"));
        assert_eq!(r.summary(), "FAILED (did not run): x");
    }

    #[test]
    fn missing_binary_cannot_execute() {
        let r = run_process("x", "/no/such/binary-brix-test", &cwd(), &[]);
        assert_eq!(r.exit_code, None);
        assert!(r.stderr.starts_with("brix: cannot execute:"));
    }

    #[test]
    fn env_extends_and_shadows() {
        let r = run_process(
            "env",
            "sh -c 'echo $BRIX_TEST_VAR'",
            &cwd(),
            &[("BRIX_TEST_VAR".to_string(), "shadowed".to_string())],
        );
        assert_eq!(r.stdout, "shadowed\n");
    }
}
