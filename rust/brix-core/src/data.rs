//! Logical data atoms — the leaves of the graph spec — and their structural
//! identity. Mirrors the atom kinds in `src/brix/data.py` and `_atom_identity`
//! in `src/brix/incremental.py`, and the wire `Atom` shape in
//! `docs/boundary-schema.md`.

use serde::Deserialize;

use crate::fingerprint::Value;
use crate::patterns::PathPattern;

/// A union of atoms (`DataExpr` on the wire is a JSON array of atoms).
pub type DataExpr = Vec<DataAtom>;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum DataAtom {
    File {
        path: String,
        #[serde(default)]
        identity: Option<String>,
    },
    Glob {
        template: String,
        #[serde(default)]
        identity: Option<String>,
    },
    Regex {
        pattern: String,
    },
    Tool {
        name: String,
    },
    Value {
        text: String,
    },
}

impl DataAtom {
    /// Structural identity as a fingerprint payload value (`_atom_identity`).
    /// `identity=` and `Value` text are fingerprint material, not identity.
    pub fn identity(&self) -> Value {
        let tag = |kind: &str, arg: &str| Value::List(vec![Value::str(kind), Value::str(arg)]);
        match self {
            DataAtom::File { path, .. } => tag("file", path),
            DataAtom::Glob { template, .. } => tag("glob", template),
            DataAtom::Regex { pattern } => tag("regex", pattern),
            DataAtom::Tool { name } => tag("tool", name),
            DataAtom::Value { .. } => Value::List(vec![Value::str("value")]),
        }
    }

    /// File/Glob/Regex are the path atoms that drive dependency inference and
    /// target selection; Tool/Value do not.
    pub fn is_path_atom(&self) -> bool {
        matches!(
            self,
            DataAtom::File { .. } | DataAtom::Glob { .. } | DataAtom::Regex { .. }
        )
    }

    /// The `PathPattern` for a File (concrete) or Glob (templated); None for
    /// Regex/Tool/Value.
    pub fn path_pattern(&self) -> Option<PathPattern> {
        match self {
            DataAtom::File { path, .. } => PathPattern::new(path).ok(),
            DataAtom::Glob { template, .. } => PathPattern::new(template).ok(),
            _ => None,
        }
    }

    /// The text used in an edge reason (`_atom_text`): the path/template, or
    /// the regex pattern.
    pub fn text(&self) -> &str {
        match self {
            DataAtom::File { path, .. } => path,
            DataAtom::Glob { template, .. } => template,
            DataAtom::Regex { pattern } => pattern,
            DataAtom::Tool { name } => name,
            DataAtom::Value { text } => text,
        }
    }

    /// A stable key for de-duplicating needs during target selection.
    pub fn dedup_key(&self) -> String {
        match self {
            DataAtom::File { path, identity } => format!("file:{path}:{identity:?}"),
            DataAtom::Glob { template, identity } => format!("glob:{template}:{identity:?}"),
            DataAtom::Regex { pattern } => format!("regex:{pattern}"),
            DataAtom::Tool { name } => format!("tool:{name}"),
            DataAtom::Value { text } => format!("value:{text}"),
        }
    }

    /// Apply `{name}` bindings where meaningful (File/Glob path, Value text).
    /// A Regex atom narrows: each named group whose name is bound is replaced
    /// by the escaped binding value (a narrower regex — mirrors
    /// `RegexData.substitute`). Tool is unchanged.
    pub fn substitute(&self, bindings: &[(String, String)]) -> DataAtom {
        match self {
            DataAtom::File { path, identity } => DataAtom::File {
                path: subst(path, bindings),
                identity: identity.clone(),
            },
            DataAtom::Glob { template, identity } => DataAtom::Glob {
                template: subst(template, bindings),
                identity: identity.clone(),
            },
            DataAtom::Value { text } if !text.is_empty() => DataAtom::Value {
                text: subst(text, bindings),
            },
            DataAtom::Regex { pattern } => DataAtom::Regex {
                pattern: regex_substitute(pattern, bindings),
            },
            other => other.clone(),
        }
    }

    /// Full-match a path against a Regex atom's pattern, returning its named
    /// captures. Mirrors `RegexData.match` (Python `re.fullmatch`). `None` for
    /// non-Regex atoms or no match. Only participating named groups are
    /// returned (brix's regex collections bind all groups).
    pub fn regex_match(&self, path: &str) -> Option<Vec<(String, String)>> {
        let DataAtom::Regex { pattern } = self else {
            return None;
        };
        // \A..\z anchors both ends -> full match, matching Python re.fullmatch.
        let re = regex::Regex::new(&format!(r"\A(?:{pattern})\z")).ok()?;
        let caps = re.captures(path)?;
        let mut out = Vec::new();
        for name in re.capture_names().flatten() {
            if let Some(m) = caps.name(name) {
                out.push((name.to_string(), m.as_str().to_string()));
            }
        }
        Some(out)
    }
}

/// Python's `re.escape` (3.7+ semantics): backslash-escape exactly the special
/// characters in `_special_chars_map`. Must be byte-exact because the escaped
/// text ends up in a regex pattern that is fingerprint material.
fn re_escape(s: &str) -> String {
    // ()[]{}?*+-|^$\.&~# and the whitespace set \t \n \r \v \f and space.
    const SPECIAL: &str = "()[]{}?*+-|^$\\.&~# \t\n\r\u{0b}\u{0c}";
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if SPECIAL.contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// Replace each `(?P<name>...)` group whose name is bound with the escaped
/// value — mirrors `RegexData.substitute`.
fn regex_substitute(pattern: &str, bindings: &[(String, String)]) -> String {
    let mut p = pattern.to_string();
    for (name, value) in bindings {
        let group = format!(r"\(\?P<{}>(?:[^()\\]|\\.)*\)", regex::escape(name));
        if let Ok(re) = regex::Regex::new(&group) {
            let replacement = re_escape(value);
            p = re.replace_all(&p, |_: &regex::Captures| replacement.clone()).into_owned();
        }
    }
    p
}

fn subst(template: &str, bindings: &[(String, String)]) -> String {
    match PathPattern::new(template) {
        Ok(p) => p.substitute(bindings),
        Err(_) => template.to_string(),
    }
}

/// Structural identity of a union (`_expr_identity`).
pub fn expr_identity(atoms: &[DataAtom]) -> Value {
    Value::List(atoms.iter().map(|a| a.identity()).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fingerprint::canonical_json;

    #[test]
    fn atom_identity_matches_python_shapes() {
        assert_eq!(
            canonical_json(&DataAtom::File { path: "a.txt".into(), identity: None }.identity()),
            r#"["file","a.txt"]"#
        );
        assert_eq!(
            canonical_json(
                &DataAtom::File { path: "a.jar".into(), identity: Some("jvm-abi".into()) }.identity()
            ),
            r#"["file","a.jar"]"#
        );
        assert_eq!(
            canonical_json(&DataAtom::Value { text: "x".into() }.identity()),
            r#"["value"]"#
        );
    }
}
