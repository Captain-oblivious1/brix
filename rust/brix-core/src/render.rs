//! Command-line placeholder rendering for realized actions. Mirrors
//! `src/brix/render.py`.
//!
//! Rendering is where logical names become physical places: inputs are read
//! and collections realized against `ctx.workspace_root` (through the
//! overlay), while declared outputs are mapped through `ctx.output_root`
//! (the two are the same unless a layer, such as atomic staging, redirects
//! outputs). Actions themselves are never rewritten.

use std::path::{Component, Path, PathBuf};

use crate::actions::Action;
use crate::context::NodeContext;
use crate::data::DataAtom;

/// Rendering failures carry a message; the scheduler turns them into a
/// did-not-run `ProcessResult` (mirrors Python's `RenderError`).
pub type RenderResult<T> = Result<T, String>;

/// Resolve a tool name to an invocable path (mirrors `resolve_tool`):
/// a name containing `/` is used as-is; otherwise PATH is searched
/// (`shutil.which` semantics: first regular, executable file wins).
pub fn resolve_tool(name: &str) -> RenderResult<String> {
    if name.contains('/') {
        return Ok(name.to_string());
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            let candidate = dir.join(name);
            if is_executable_file(&candidate) {
                return Ok(candidate.to_string_lossy().into_owned());
            }
        }
    }
    Err(format!("tool not found: {name}"))
}

#[cfg(unix)]
fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    match path.metadata() {
        Ok(md) => md.is_file() && md.permissions().mode() & 0o111 != 0,
        Err(_) => false,
    }
}

#[cfg(not(unix))]
fn is_executable_file(path: &Path) -> bool {
    path.is_file()
}

/// `os.path.relpath(path, start)` for the paths brix renders (both absolute,
/// no `..` segments of their own): walk off the common prefix, then `..` out
/// of what remains of `start` and descend into what remains of `path`.
pub fn relpath(path: &Path, start: &Path) -> String {
    let path_parts: Vec<Component> = path.components().collect();
    let start_parts: Vec<Component> = start.components().collect();
    let common = path_parts
        .iter()
        .zip(start_parts.iter())
        .take_while(|(a, b)| a == b)
        .count();
    let mut out = PathBuf::new();
    for _ in common..start_parts.len() {
        out.push("..");
    }
    for part in &path_parts[common..] {
        out.push(part);
    }
    if out.as_os_str().is_empty() {
        return ".".to_string();
    }
    out.to_string_lossy().replace('\\', "/")
}

/// Map a logical output path to the physical path used in the command.
/// Commands run with `cwd = workspace_root`, so the physical path is
/// expressed relative to the workspace. With `output_root == workspace_root`
/// this is the logical path unchanged.
fn output_path(ctx: &NodeContext, logical: &str) -> String {
    if ctx.output_root == ctx.workspace_root {
        return logical.to_string();
    }
    relpath(&ctx.output_root.join(logical), &ctx.workspace_root)
}

/// Express a physical path relative to the command's cwd (the workspace).
fn relative(ctx: &NodeContext, physical: &Path) -> String {
    relpath(physical, &ctx.workspace_root)
}

fn input_path(ctx: &NodeContext, logical: &str) -> String {
    let physical = ctx.read_path(logical);
    if physical == ctx.workspace_root.join(logical) {
        return logical.to_string();
    }
    relative(ctx, &physical)
}

/// Render one atom (mirrors `_render_expr` on a single expression part).
fn render_atom(name: &str, atom: &DataAtom, ctx: &NodeContext, is_output: bool) -> RenderResult<String> {
    match atom {
        DataAtom::Value { text } => Ok(text.clone()),
        DataAtom::Tool { name } => resolve_tool(name),
        DataAtom::File { path, .. } => {
            let pattern = crate::patterns::PathPattern::new(path)
                .map_err(|e| format!("{name}: {e}"))?;
            if !pattern.is_concrete() {
                return Err(format!("{name}: unresolved bindings in path '{path}'"));
            }
            if is_output {
                Ok(output_path(ctx, path))
            } else {
                Ok(input_path(ctx, path))
            }
        }
        DataAtom::Glob { template, .. } => {
            if is_output {
                return Err(format!(
                    "{name}: output collection '{template}' cannot be rendered into \
                     a command line; pass a directory value instead"
                ));
            }
            let pattern = crate::patterns::PathPattern::new(template)
                .map_err(|e| format!("{name}: {e}"))?;
            Ok(ctx
                .find(&pattern)
                .iter()
                .map(|(_, physical, _)| relative(ctx, physical))
                .collect::<Vec<_>>()
                .join(" "))
        }
        DataAtom::Regex { .. } => {
            if is_output {
                return Err(format!("{name}: output regex collection cannot be rendered"));
            }
            Ok(ctx
                .iter_files()
                .iter()
                .filter(|(logical, _)| atom.regex_match(logical).is_some())
                .map(|(_, physical)| relative(ctx, physical))
                .collect::<Vec<_>>()
                .join(" "))
        }
    }
}

/// Render a full expression: a union renders each part and joins the
/// non-empty ones with spaces (mirrors `_render_expr` over `DataUnion`; on
/// the wire an expression is always a list of atoms).
fn render_expr(
    name: &str,
    atoms: &[DataAtom],
    ctx: &NodeContext,
    is_output: bool,
) -> RenderResult<String> {
    let mut parts = Vec::new();
    for atom in atoms {
        let part = render_atom(name, atom, ctx, is_output)?;
        if !part.is_empty() {
            parts.push(part);
        }
    }
    Ok(parts.join(" "))
}

/// Substitute input/output placeholders in the action's command line.
/// Only placeholders the command line references are rendered: declared
/// outputs such as unrealized collections need not appear in the command.
pub fn render_command(action: &Action, ctx: &NodeContext) -> RenderResult<String> {
    let Action::Process { command_line, inputs, outputs, .. } = action else {
        return Err("render_command: not a process action".to_string());
    };
    let label = action.label();
    for (name, _) in inputs {
        if outputs.iter().any(|(o, _)| o == name) {
            return Err(format!("'{name}' is both an input and an output of '{label}'"));
        }
    }
    // Command placeholders are `{word}` names — the input/output keys, not
    // path patterns (no `:*` constraints here).
    let placeholder = regex::Regex::new(r"\{(\w+)\}").unwrap();
    let mut out = String::new();
    let mut pos = 0;
    for m in placeholder.captures_iter(command_line) {
        let whole = m.get(0).unwrap();
        let key = m.get(1).unwrap().as_str();
        out.push_str(&command_line[pos..whole.start()]);
        if let Some((_, expr)) = inputs.iter().find(|(k, _)| k == key) {
            out.push_str(&render_expr(key, expr, ctx, false)?);
        } else if let Some((_, expr)) = outputs.iter().find(|(k, _)| k == key) {
            out.push_str(&render_expr(key, expr, ctx, true)?);
        } else {
            return Err(format!("'{label}': unknown placeholder {{{key}}}"));
        }
        pos = whole.end();
    }
    out.push_str(&command_line[pos..]);
    Ok(out)
}

/// Render the action's declared environment entries (inputs: overlay-resolved).
pub fn render_env(action: &Action, ctx: &NodeContext) -> RenderResult<Vec<(String, String)>> {
    let Action::Process { env, .. } = action else {
        return Ok(Vec::new());
    };
    env.iter()
        .map(|(key, expr)| Ok((key.clone(), render_expr(&format!("env {key}"), expr, ctx, false)?)))
        .collect()
}

/// Create parent directories for declared outputs under the output root.
pub fn prepare_output_dirs(action: &Action, ctx: &NodeContext) -> RenderResult<()> {
    let Action::Process { outputs, .. } = action else {
        return Ok(());
    };
    for (_, expr) in outputs {
        for atom in expr {
            match atom {
                DataAtom::File { path, .. } => {
                    if let Ok(p) = crate::patterns::PathPattern::new(path) {
                        if p.is_concrete() {
                            if let Some(parent) = ctx.output_root.join(path).parent() {
                                std::fs::create_dir_all(parent)
                                    .map_err(|e| format!("cannot create output dir: {e}"))?;
                            }
                        }
                    }
                }
                DataAtom::Glob { template, .. } => {
                    if let Ok(p) = crate::patterns::PathPattern::new(template) {
                        let prefix = p.literal_prefix_dir();
                        if !prefix.is_empty() {
                            std::fs::create_dir_all(ctx.output_root.join(prefix))
                                .map_err(|e| format!("cannot create output dir: {e}"))?;
                        }
                    }
                }
                _ => {}
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relpath_matches_os_path_relpath() {
        let p = Path::new("/a/b/c/d.txt");
        assert_eq!(relpath(p, Path::new("/a/b")), "c/d.txt");
        assert_eq!(relpath(p, Path::new("/a/x")), "../b/c/d.txt");
        assert_eq!(relpath(Path::new("/a/b"), Path::new("/a/b")), ".");
        assert_eq!(relpath(Path::new("/a"), Path::new("/a/b/c")), "../..");
    }

    #[test]
    fn resolve_tool_pathlike_and_missing() {
        assert_eq!(resolve_tool("./bin/x").unwrap(), "./bin/x");
        assert!(resolve_tool("no-such-brix-tool").is_err());
        // `sh` exists on any dev machine this suite runs on.
        assert!(resolve_tool("sh").unwrap().ends_with("/sh"));
    }
}
