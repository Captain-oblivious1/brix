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
        /// The content lens name (`jvm-abi`, `ts-dts`, …); absent = byte hash.
        /// Set by the skin's typed atoms (e.g. `brix.java`'s `AbiFile`), never
        /// typed by the author. Fingerprinting dispatches it through
        /// [`crate::content_key`]; it is not part of structural identity.
        #[serde(default)]
        content_key: Option<String>,
    },
    Glob {
        template: String,
        #[serde(default)]
        content_key: Option<String>,
        /// fnmatch patterns filtering membership (`data.glob(...) - "pat"`).
        /// Additive wire field: absent and `[]` are identical.
        #[serde(default)]
        exclude: Vec<String>,
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
    /// A file brix itself ships, named by a stable logical `name` (POSIX,
    /// brix-relative). The engine owns the bytes ([`crate::assets`]); its
    /// fingerprint is `["asset", name, content-digest]` — never the resolved
    /// path — so it is identical across machines and frontends. Not a path atom
    /// (it drives no workspace dependency inference).
    Asset {
        name: String,
    },
}

impl DataAtom {
    /// Structural identity as a fingerprint payload value (`_atom_identity`).
    /// `content_key` and `Value` text are fingerprint material, not identity.
    pub fn identity(&self) -> Value {
        let tag = |kind: &str, arg: &str| Value::List(vec![Value::str(kind), Value::str(arg)]);
        match self {
            DataAtom::File { path, .. } => tag("file", path),
            // The exclude list joins identity only when non-empty so that
            // pre-exclude identities stay byte-identical.
            DataAtom::Glob { template, exclude, .. } if !exclude.is_empty() => {
                let mut excl = vec![Value::str("exclude")];
                excl.extend(exclude.iter().map(|p| Value::str(p)));
                Value::List(vec![Value::str("glob"), Value::str(template), Value::List(excl)])
            }
            DataAtom::Glob { template, .. } => tag("glob", template),
            DataAtom::Regex { pattern } => tag("regex", pattern),
            DataAtom::Tool { name } => tag("tool", name),
            DataAtom::Value { .. } => Value::List(vec![Value::str("value")]),
            // The name is identity; the content digest is fingerprint material
            // (mirrors File: identity ["file", path], fingerprint adds the hash).
            DataAtom::Asset { name } => tag("asset", name),
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
            DataAtom::Glob { template, exclude, .. } => {
                PathPattern::with_exclude(template, exclude).ok()
            }
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
            DataAtom::Asset { name } => name,
        }
    }

    /// A stable key for de-duplicating needs during target selection.
    pub fn dedup_key(&self) -> String {
        match self {
            DataAtom::File { path, content_key } => format!("file:{path}:{content_key:?}"),
            DataAtom::Glob { template, content_key, exclude } if exclude.is_empty() => {
                format!("glob:{template}:{content_key:?}")
            }
            DataAtom::Glob { template, content_key, exclude } => {
                format!("glob:{template}:{content_key:?}:{exclude:?}")
            }
            DataAtom::Regex { pattern } => format!("regex:{pattern}"),
            DataAtom::Tool { name } => format!("tool:{name}"),
            DataAtom::Value { text } => format!("value:{text}"),
            DataAtom::Asset { name } => format!("asset:{name}"),
        }
    }

    /// Apply `{name}` bindings where meaningful (File/Glob path, Value text).
    /// A Regex atom narrows: each named group whose name is bound is replaced
    /// by the escaped binding value (a narrower regex — mirrors
    /// `RegexData.substitute`). Tool is unchanged.
    pub fn substitute(&self, bindings: &[(String, String)]) -> DataAtom {
        match self {
            DataAtom::File { path, content_key } => DataAtom::File {
                path: subst(path, bindings),
                content_key: content_key.clone(),
            },
            DataAtom::Glob { template, content_key, exclude } => DataAtom::Glob {
                template: subst(template, bindings),
                content_key: content_key.clone(),
                exclude: exclude.clone(),
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
        // fancy-regex (not the `regex` crate) so author collections may use
        // lookaround, e.g. a `(?!container/)` guard distinguishing a container
        // directory from a real member. It falls back to the linear `regex`
        // engine for patterns without fancy features, so the common case keeps
        // its guarantees; `captures` returns a Result because a fancy pattern
        // can hit the backtrack limit — a match error is treated as no match.
        let re = fancy_regex::Regex::new(&format!(r"\A(?:{pattern})\z")).ok()?;
        let caps = re.captures(path).ok()??;
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

/// If `chars[i..]` opens a `(?P<name>` group, return `(name, index_after_'>')`.
fn parse_group_head(chars: &[char], i: usize) -> Option<(String, usize)> {
    let head: [char; 4] = ['(', '?', 'P', '<'];
    if i + head.len() > chars.len() || chars[i..i + head.len()] != head {
        return None;
    }
    let mut j = i + head.len();
    let mut name = String::new();
    while j < chars.len() && chars[j] != '>' {
        name.push(chars[j]);
        j += 1;
    }
    if j >= chars.len() || name.is_empty() {
        return None;
    }
    Some((name, j + 1)) // skip the '>'
}

/// Replace each `(?P<name>...)` whose name is bound with the escaped value,
/// leaving groups whose names are not in `values` intact. Scans the body with a
/// paren-depth counter (and stays out of `[...]` character classes), so a body
/// that itself contains groups — `(?:a)|(?!b)c` — is handled; the naive
/// group-shaped regex silently declines a paren-bearing body, leaving the
/// pattern unnarrowed. Returns `(filled, all_pinned)`; `None` on an unbalanced
/// group. Shared by `regex_substitute` and target selection's membership check;
/// mirrors `_fill_named_groups` in `src/brix/data.py`. Uses `re_escape` (not
/// `regex::escape`) because the result is fingerprint material and must be
/// byte-identical to the Python engine.
pub(crate) fn fill_named_groups(
    pattern: &str,
    values: &[(String, String)],
) -> Option<(String, bool)> {
    let chars: Vec<char> = pattern.chars().collect();
    let n = chars.len();
    let mut out = String::new();
    let mut all_pinned = true;
    let mut i = 0;
    while i < n {
        if let Some((name, body_start)) = parse_group_head(&chars, i) {
            let (mut depth, mut k, mut in_class) = (1i32, body_start, false);
            while k < n && depth > 0 {
                let c = chars[k];
                if c == '\\' {
                    k += 2;
                    continue;
                }
                if in_class {
                    in_class = c != ']';
                    k += 1;
                    continue;
                }
                if c == '[' {
                    in_class = true;
                    k += 1;
                    if k < n && chars[k] == '^' {
                        k += 1;
                    }
                    if k < n && chars[k] == ']' {
                        // a leading ] is a literal class member
                        k += 1;
                    }
                    continue;
                }
                depth += if c == '(' {
                    1
                } else if c == ')' {
                    -1
                } else {
                    0
                };
                k += 1;
            }
            if depth != 0 {
                return None; // unbalanced — do not guess
            }
            match values.iter().find(|(nm, _)| nm == &name) {
                Some((_, v)) => out.push_str(&re_escape(v)),
                None => {
                    all_pinned = false;
                    out.extend(chars[i..k].iter());
                }
            }
            i = k;
            continue;
        }
        if chars[i] == '\\' {
            out.push(chars[i]);
            if i + 1 < n {
                out.push(chars[i + 1]);
            }
            i += 2;
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    Some((out, all_pinned))
}

/// Replace each `(?P<name>...)` group whose name is bound with the escaped
/// value — mirrors `RegexData.substitute`. Names not present as groups are
/// ignored (bindings are commonly a superset).
fn regex_substitute(pattern: &str, bindings: &[(String, String)]) -> String {
    // `None` is only reached on an unbalanced group, which the `Regex::new`
    // in `regex_match`/construction already rejects — unreachable for a real
    // pattern. Degrade to unchanged (as the Python engine does) rather than
    // diverge the two engines.
    match fill_named_groups(pattern, bindings) {
        Some((filled, _)) => filled,
        None => pattern.to_string(),
    }
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
            canonical_json(&DataAtom::File { path: "a.txt".into(), content_key: None }.identity()),
            r#"["file","a.txt"]"#
        );
        assert_eq!(
            canonical_json(
                &DataAtom::File { path: "a.jar".into(), content_key: Some("jvm-abi".into()) }
                    .identity()
            ),
            r#"["file","a.jar"]"#
        );
        assert_eq!(
            canonical_json(&DataAtom::Value { text: "x".into() }.identity()),
            r#"["value"]"#
        );
        // Exclude joins identity only when non-empty; the empty case is
        // byte-identical to the pre-exclude shape.
        let glob = |exclude: &[&str]| DataAtom::Glob {
            template: "src/{n}.cc".into(),
            content_key: None,
            exclude: exclude.iter().map(|s| s.to_string()).collect(),
        };
        assert_eq!(canonical_json(&glob(&[]).identity()), r#"["glob","src/{n}.cc"]"#);
        assert_eq!(
            canonical_json(&glob(&["*_test.cc"]).identity()),
            r#"["glob","src/{n}.cc",["exclude","*_test.cc"]]"#
        );
    }

    #[test]
    fn glob_exclude_carries_through_substitute_and_pattern() {
        let atom = DataAtom::Glob {
            template: "src/{n}.cc".into(),
            content_key: None,
            exclude: vec!["*_test.cc".into()],
        };
        let sub = atom.substitute(&[("n".into(), "foo".into())]);
        let DataAtom::Glob { template, exclude, .. } = &sub else {
            panic!("substitute changed the atom kind");
        };
        assert_eq!(template, "src/foo.cc");
        assert_eq!(exclude, &["*_test.cc".to_string()]);
        assert_eq!(atom.path_pattern().unwrap().exclude, vec!["*_test.cc".to_string()]);
        // Wire field is additive: absent deserializes as empty.
        let parsed: DataAtom =
            serde_json::from_str(r#"{"kind":"glob","template":"a/{n}.cc"}"#).unwrap();
        let DataAtom::Glob { exclude, .. } = &parsed else { panic!() };
        assert!(exclude.is_empty());
    }

    fn regex(pattern: &str) -> DataAtom {
        DataAtom::Regex { pattern: pattern.to_string() }
    }

    #[test]
    fn substitute_narrows_a_group_whose_body_has_parens() {
        // Bug 0.1: a bound {video} must narrow the collection even when its
        // group body is a lookahead alternation (parentheses). The old
        // group-shaped matcher silently declined it, leaving the collection
        // project-wide so a nested video expanded over every scene.
        let scenes = regex(
            r"src/main/videos/(?P<video>(?:rights)/[0-9][^/]*|(?!(?:rights)/)[^/]+)/(?P<scene>[0-9][^/]*)\.py",
        );
        let narrowed = scenes.substitute(&[("video".into(), "rights/02_islandEmperor".into())]);
        let DataAtom::Regex { pattern } = &narrowed else { panic!() };
        assert_eq!(
            pattern,
            r"src/main/videos/rights/02_islandEmperor/(?P<scene>[0-9][^/]*)\.py"
        );
        // The narrowed collection binds a scene only for that one video...
        assert_eq!(
            narrowed.regex_match("src/main/videos/rights/02_islandEmperor/00_title.py"),
            Some(vec![("scene".to_string(), "00_title".to_string())]),
        );
        // ...and rejects a scene path belonging to another video.
        assert_eq!(narrowed.regex_match("src/main/videos/wealth/03_x.py"), None);
    }

    #[test]
    fn substitute_ignores_parens_inside_a_character_class() {
        // A ')' inside [...] must not fool the paren-depth counter.
        let atom = regex(r"(?P<v>[()]+)/(?P<s>[0-9]+)\.py");
        let DataAtom::Regex { pattern } = &atom.substitute(&[("v".into(), "AB".into())]) else {
            panic!()
        };
        assert_eq!(pattern, r"AB/(?P<s>[0-9]+)\.py");
    }

    #[test]
    fn substitute_matches_legacy_escaping_on_plain_groups() {
        // Byte-for-byte parity with the previous regex-based substitution for
        // the simple (paren-free body) cases it handled — fingerprint material.
        let legacy = |pattern: &str, binds: &[(&str, &str)]| {
            let mut p = pattern.to_string();
            for (name, value) in binds {
                let group = format!(r"\(\?P<{}>(?:[^()\\]|\\.)*\)", regex::escape(name));
                if let Ok(re) = regex::Regex::new(&group) {
                    let replacement = re_escape(value);
                    p = re.replace_all(&p, |_: &regex::Captures| replacement.clone()).into_owned();
                }
            }
            p
        };
        for (pattern, binds) in [
            (r"(?P<video>[^/]+)/(?P<scene>[0-9]\S*)", vec![("video", "intro")]),
            (r"a/(?P<n>[^/]+)\.cc", vec![("n", "a.b-c+d")]),
            (r"(?P<x>[^/]+)", vec![("y", "unrelated")]),
        ] {
            let owned: Vec<(String, String)> =
                binds.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
            assert_eq!(regex_substitute(pattern, &owned), legacy(pattern, &binds), "{pattern}");
        }
    }
}
