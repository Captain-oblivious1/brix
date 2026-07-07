//! The fingerprint digest — a cross-machine cache key, so byte-exact with
//! Python's `_digest`:
//!
//! ```python
//! sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"))
//!        .encode()).hexdigest()
//! ```
//!
//! with `ensure_ascii=True` (Python's default): non-ASCII is escaped as
//! `\uXXXX`, astral code points as a surrogate pair. serde_json will not
//! reproduce that escaping, so the canonical serializer here is hand-rolled
//! and pinned by golden fixtures. Payload values are only strings, ints,
//! bools, null, lists, and maps — no floats (so no float-formatting hazard),
//! matching the Python payloads.

use std::collections::BTreeMap;

use sha2::{Digest, Sha256};

/// A JSON-shaped fingerprint payload. Maps are kept in a `BTreeMap` so keys
/// serialize in sorted order — Rust's `String` ordering is by UTF-8 bytes,
/// which equals Unicode code-point order, matching Python's `sort_keys`.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Str(String),
    List(Vec<Value>),
    Map(BTreeMap<String, Value>),
}

impl Value {
    pub fn str(s: impl Into<String>) -> Value {
        Value::Str(s.into())
    }
}

/// Serialize to the exact bytes Python's `json.dumps(..., sort_keys=True,
/// separators=(",", ":"))` produces with `ensure_ascii=True`.
pub fn canonical_json(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value);
    out
}

/// `sha256(canonical_json(value)).hexdigest()` — the fingerprint.
pub fn digest(value: &Value) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical_json(value).as_bytes());
    let bytes = hasher.finalize();
    let mut hex = String::with_capacity(64);
    for b in bytes {
        hex.push_str(&format!("{:02x}", b));
    }
    hex
}

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(n) => out.push_str(&n.to_string()),
        Value::Str(s) => write_string(out, s),
        Value::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Map(map) => {
            out.push('{');
            for (i, (key, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, key);
                out.push(':');
                write_value(out, val);
            }
            out.push('}');
        }
    }
}

/// Escape a string exactly as Python's json encoder does with
/// `ensure_ascii=True`.
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{09}' => out.push_str("\\t"),
            '\u{0a}' => out.push_str("\\n"),
            '\u{0c}' => out.push_str("\\f"),
            '\u{0d}' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            // Printable ASCII 0x20..=0x7e passes through. Python's json escapes
            // everything outside that range (its `[^\ -~]` rule), so 0x7f (DEL)
            // and up are escaped below.
            c if (c as u32) < 0x7f => out.push(c),
            // 0x7f and non-ASCII: \uXXXX, astral code points as a surrogate pair.
            c => {
                let cp = c as u32;
                if cp > 0xFFFF {
                    let v = cp - 0x10000;
                    let hi = 0xD800 + (v >> 10);
                    let lo = 0xDC00 + (v & 0x3FF);
                    out.push_str(&format!("\\u{:04x}\\u{:04x}", hi, lo));
                } else {
                    out.push_str(&format!("\\u{:04x}", cp));
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_and_sorted() {
        let mut m = BTreeMap::new();
        m.insert("b".to_string(), Value::Int(2));
        m.insert("a".to_string(), Value::Int(1));
        assert_eq!(canonical_json(&Value::Map(m)), r#"{"a":1,"b":2}"#);
    }

    #[test]
    fn escapes_non_ascii() {
        assert_eq!(canonical_json(&Value::str("é")), "\"\\u00e9\"");
        assert_eq!(canonical_json(&Value::str("\n")), "\"\\n\"");
        // Astral: U+1F600 -> surrogate pair 😀.
        assert_eq!(canonical_json(&Value::str("\u{1F600}")), "\"\\ud83d\\ude00\"");
    }
}
