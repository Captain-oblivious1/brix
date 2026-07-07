//! Logical data atoms and their *structural identity* — what an atom refers
//! to, independent of file content. Mirrors the atom kinds in
//! `src/brix/data.py` and `_atom_identity` in `src/brix/incremental.py`.
//!
//! Structural identity feeds `action_key` (an action's stable identity across
//! builds). Note `Value`'s text is fingerprint *material*, not identity, so it
//! contributes only the tag `["value"]` here — exactly as in Python. Content
//! hashing and content-identity (`jvm-abi`) belong to `action_fingerprint`,
//! which needs file I/O and lands in a later phase.

use crate::fingerprint::Value;

#[derive(Debug, Clone, PartialEq)]
pub enum DataAtom {
    /// A single logical path. `identity` names a content-identity plugin
    /// (fingerprint material only; not part of structural identity).
    File { path: String, identity: Option<String> },
    /// A path set with `{name}` bindings.
    Glob { template: String, identity: Option<String> },
    /// A path set matched by a regular expression (Python `re` pattern text).
    Regex { pattern: String },
    /// A tool identity (name or absolute path).
    Tool { name: String },
    /// Literal data affecting an output.
    Value { text: String },
}

impl DataAtom {
    /// Structural identity as a fingerprint payload value — matches Python's
    /// `_atom_identity`.
    pub fn identity(&self) -> Value {
        let tag = |kind: &str, arg: &str| {
            Value::List(vec![Value::str(kind), Value::str(arg)])
        };
        match self {
            DataAtom::File { path, .. } => tag("file", path),
            DataAtom::Glob { template, .. } => tag("glob", template),
            DataAtom::Regex { pattern } => tag("regex", pattern),
            DataAtom::Tool { name } => tag("tool", name),
            // Text is fingerprint material, not identity.
            DataAtom::Value { .. } => Value::List(vec![Value::str("value")]),
        }
    }
}

/// Structural identity of a union of atoms (`_expr_identity`): the list of
/// each atom's identity.
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
        // identity= does not change structural identity.
        assert_eq!(
            canonical_json(
                &DataAtom::File { path: "a.jar".into(), identity: Some("jvm-abi".into()) }.identity()
            ),
            r#"["file","a.jar"]"#
        );
        assert_eq!(
            canonical_json(&DataAtom::Value { text: "anything".into() }.identity()),
            r#"["value"]"#
        );
    }
}
