//! `PathPattern`: a workspace-relative path template whose `{name}`
//! placeholders each match one path segment (`[^/]+`, no `/`).
//!
//! Mirrors `src/brix/patterns.py`. Python compiles the template to a regex
//! (`{name}` -> `(?P<name>[^/]+)`, a repeated name -> a backreference); this
//! port matches by hand instead — the `regex` crate has no backreferences,
//! and hand-matching this small language avoids any regex-engine drift from
//! Python's `re`. Behavior is pinned by golden fixtures.

use std::path::Path;

use crate::fsutil::walk_files;

/// One piece of a parsed template.
#[derive(Debug, Clone, PartialEq)]
enum Token {
    Literal(String),
    Binding(String),
}

#[derive(Debug, Clone)]
pub struct PathPattern {
    pub template: String,
    tokens: Vec<Token>,
    /// Binding names in order of appearance, duplicates included (matching
    /// Python's `tuple(_BINDING_RE.findall(template))`).
    pub binding_names: Vec<String>,
}

/// A `{name}` binding where `name` is `\w+` (ASCII letters, digits, `_`),
/// matching Python's `_BINDING_RE = re.compile(r"\{(\w+)\}")`.
fn is_word(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

fn parse(template: &str) -> (Vec<Token>, Vec<String>) {
    let mut tokens = Vec::new();
    let mut names = Vec::new();
    let mut literal = String::new();
    let chars: Vec<char> = template.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '{' {
            // Try to read {word}.
            let mut j = i + 1;
            while j < chars.len() && is_word(chars[j]) {
                j += 1;
            }
            if j > i + 1 && j < chars.len() && chars[j] == '}' {
                let name: String = chars[i + 1..j].iter().collect();
                if !literal.is_empty() {
                    tokens.push(Token::Literal(std::mem::take(&mut literal)));
                }
                names.push(name.clone());
                tokens.push(Token::Binding(name));
                i = j + 1;
                continue;
            }
        }
        literal.push(chars[i]);
        i += 1;
    }
    if !literal.is_empty() {
        tokens.push(Token::Literal(literal));
    }
    (tokens, names)
}

impl PathPattern {
    pub fn new(template: &str) -> Result<PathPattern, String> {
        if template.is_empty() {
            return Err("empty path pattern".to_string());
        }
        let (tokens, binding_names) = parse(template);
        Ok(PathPattern {
            template: template.to_string(),
            tokens,
            binding_names,
        })
    }

    pub fn is_concrete(&self) -> bool {
        self.binding_names.is_empty()
    }

    /// The deepest directory prefix that is literal (no bindings). May be "".
    /// Mirrors Python: text before the first `{`, then drop the last segment.
    pub fn literal_prefix_dir(&self) -> String {
        let prefix = match self.template.find('{') {
            Some(idx) => &self.template[..idx],
            None => &self.template[..],
        };
        match prefix.rfind('/') {
            Some(idx) => prefix[..idx].to_string(),
            None => String::new(),
        }
    }

    /// Full-match `path`, returning the captured bindings, or `None`.
    pub fn matches(&self, path: &str) -> Option<Vec<(String, String)>> {
        let pchars: Vec<char> = path.chars().collect();
        let mut bindings: Vec<(String, String)> = Vec::new();
        if match_tokens(&self.tokens, 0, &pchars, 0, &mut bindings) {
            Some(bindings)
        } else {
            None
        }
    }

    /// Realize this pattern against the filesystem under `root`, mirroring
    /// `PathPattern.find`. Returns sorted `(root-relative path, bindings)`
    /// pairs. A concrete template is a single `is_file` probe; a templated one
    /// walks its literal prefix directory and full-matches every regular file.
    pub fn find(&self, root: &Path) -> Vec<(String, Vec<(String, String)>)> {
        if self.is_concrete() {
            return if root.join(&self.template).is_file() {
                vec![(self.template.clone(), Vec::new())]
            } else {
                Vec::new()
            };
        }
        let prefix = self.literal_prefix_dir();
        let base = if prefix.is_empty() {
            root.to_path_buf()
        } else {
            root.join(&prefix)
        };
        if !base.is_dir() {
            return Vec::new();
        }
        // Python sorts `base.rglob("*")` (full paths) then relativizes; since
        // all entries share the `root` prefix, sorting the relative paths gives
        // the identical order.
        let mut rels: Vec<String> = walk_files(&base)
            .iter()
            .filter_map(|p| p.strip_prefix(root).ok())
            .map(|rel| rel.to_string_lossy().replace('\\', "/"))
            .collect();
        rels.sort();
        rels.into_iter()
            .filter_map(|rel| self.matches(&rel).map(|b| (rel, b)))
            .collect()
    }

    /// Replace known bindings; unknown `{name}` placeholders are left intact.
    pub fn substitute(&self, bindings: &[(String, String)]) -> String {
        let lookup = |name: &str| bindings.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone());
        let mut out = String::new();
        let chars: Vec<char> = self.template.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            if chars[i] == '{' {
                let mut j = i + 1;
                while j < chars.len() && is_word(chars[j]) {
                    j += 1;
                }
                if j > i + 1 && j < chars.len() && chars[j] == '}' {
                    let name: String = chars[i + 1..j].iter().collect();
                    if let Some(value) = lookup(&name) {
                        out.push_str(&value);
                    } else {
                        out.push('{');
                        out.push_str(&name);
                        out.push('}');
                    }
                    i = j + 1;
                    continue;
                }
            }
            out.push(chars[i]);
            i += 1;
        }
        out
    }
}

/// Recursive backtracking match, replicating `^literal(?P<a>[^/]+)...$`:
/// a binding consumes one or more non-`/` chars, greedy (longest first) with
/// backtracking; a repeated binding must match the same text again.
fn match_tokens(
    tokens: &[Token],
    ti: usize,
    path: &[char],
    pi: usize,
    bindings: &mut Vec<(String, String)>,
) -> bool {
    if ti == tokens.len() {
        return pi == path.len(); // full match only
    }
    match &tokens[ti] {
        Token::Literal(lit) => {
            let lit_chars: Vec<char> = lit.chars().collect();
            if pi + lit_chars.len() > path.len() {
                return false;
            }
            for (k, lc) in lit_chars.iter().enumerate() {
                if path[pi + k] != *lc {
                    return false;
                }
            }
            match_tokens(tokens, ti + 1, path, pi + lit_chars.len(), bindings)
        }
        Token::Binding(name) => {
            if let Some((_, bound)) = bindings.iter().find(|(k, _)| k == name) {
                // Repeated binding: the same text must appear here.
                let bound_chars: Vec<char> = bound.chars().collect();
                if pi + bound_chars.len() > path.len() {
                    return false;
                }
                for (k, bc) in bound_chars.iter().enumerate() {
                    if path[pi + k] != *bc {
                        return false;
                    }
                }
                return match_tokens(tokens, ti + 1, path, pi + bound_chars.len(), bindings);
            }
            // [^/]+ : the maximal run of non-slash chars from pi, then shrink.
            let mut max = pi;
            while max < path.len() && path[max] != '/' {
                max += 1;
            }
            let mut end = max;
            while end > pi {
                let value: String = path[pi..end].iter().collect();
                bindings.push((name.clone(), value));
                if match_tokens(tokens, ti + 1, path, end, bindings) {
                    return true;
                }
                bindings.pop();
                end -= 1;
            }
            false
        }
    }
}

const PROBE: &str = "\u{0}brix\u{0}";

/// Conservatively decide whether two patterns can name the same path.
/// Mirrors `patterns.overlaps`.
pub fn overlaps(a: &PathPattern, b: &PathPattern) -> bool {
    match (a.is_concrete(), b.is_concrete()) {
        (true, true) => a.template == b.template,
        (true, false) => b.matches(&a.template).is_some(),
        (false, true) => a.matches(&b.template).is_some(),
        (false, false) => {
            let probe_a = a.substitute(&probe_bindings(a));
            let probe_b = b.substitute(&probe_bindings(b));
            b.matches(&probe_a).is_some() || a.matches(&probe_b).is_some()
        }
    }
}

fn probe_bindings(p: &PathPattern) -> Vec<(String, String)> {
    let mut seen = Vec::new();
    for name in &p.binding_names {
        if !seen.iter().any(|(k, _): &(String, String)| k == name) {
            seen.push((name.clone(), PROBE.to_string()));
        }
    }
    seen
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(pat: &str, path: &str) -> Option<Vec<(String, String)>> {
        PathPattern::new(pat).unwrap().matches(path)
    }

    #[test]
    fn basic_binding() {
        assert_eq!(
            m("src/{name}.txt", "src/foo.txt"),
            Some(vec![("name".to_string(), "foo".to_string())])
        );
        assert_eq!(m("src/{name}.txt", "src/a/b.txt"), None); // {name} is one segment
    }

    #[test]
    fn repeated_binding_must_match() {
        assert!(m("{a}/{a}.txt", "x/x.txt").is_some());
        assert!(m("{a}/{a}.txt", "x/y.txt").is_none());
    }

    #[test]
    fn adjacent_bindings_backtrack() {
        assert_eq!(
            m("{a}-{b}.txt", "x-y.txt"),
            Some(vec![
                ("a".to_string(), "x".to_string()),
                ("b".to_string(), "y".to_string())
            ])
        );
    }

    #[test]
    fn concrete_and_prefix() {
        let p = PathPattern::new("build/upper/{name}.txt").unwrap();
        assert!(!p.is_concrete());
        assert_eq!(p.literal_prefix_dir(), "build/upper");
        assert_eq!(PathPattern::new("a/b.txt").unwrap().literal_prefix_dir(), "a");
    }
}
