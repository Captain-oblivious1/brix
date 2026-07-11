//! TypeScript `.d.ts` content identity. Mirrors `src/brix/tsdts.py` — the
//! digest is a cross-machine, cross-engine cache key, so `normalize` must
//! match Python byte-for-byte (golden-pinned).
//!
//! Strips `//` and `/* */` comments (respecting string and template literals)
//! and collapses each run of formatting whitespace to a single space;
//! whitespace inside a string/template literal is preserved verbatim. Never
//! sorts or reorders declarations — over-approximation only, the safe
//! direction for a cache key.

use std::path::Path;

use sha2::{Digest, Sha256};

/// Python's `str.isspace()` — Unicode whitespace plus the FS/GS/RS/US
/// separators (0x1c–0x1f), which `char::is_whitespace` excludes.
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

pub fn normalize(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::new();
    let mut prev_space = true; // true so leading whitespace is dropped
    let mut i = 0;
    let n = chars.len();
    while i < n {
        let c = chars[i];
        let nxt = if i + 1 < n { chars[i + 1] } else { '\0' };
        if c == '/' && nxt == '/' {
            i += 2;
            while i < n && chars[i] != '\n' {
                i += 1;
            }
        } else if c == '/' && nxt == '*' {
            i += 2;
            while i < n && !(chars[i] == '*' && i + 1 < n && chars[i + 1] == '/') {
                i += 1;
            }
            i += 2; // skip the closing */
        } else if matches!(c, '\'' | '"' | '`') {
            let quote = c;
            out.push(c);
            i += 1;
            while i < n {
                let d = chars[i];
                out.push(d);
                if d == '\\' && i + 1 < n {
                    // escape: take the next char verbatim
                    out.push(chars[i + 1]);
                    i += 2;
                    continue;
                }
                i += 1;
                if d == quote {
                    break;
                }
            }
            prev_space = false;
        } else if py_isspace(c) {
            if !prev_space {
                out.push(' ');
                prev_space = true;
            }
            i += 1;
        } else {
            out.push(c);
            prev_space = false;
            i += 1;
        }
    }
    // Python strips with str.strip() (same whitespace set as isspace).
    out.trim_matches(py_isspace).to_string()
}

/// Identity digest of a `.d.ts` file. `Err` (so the engine falls back to a
/// `:raw` byte hash) on a missing file or one that is not UTF-8 text.
pub fn digest(path: &Path) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
    let text = String::from_utf8(bytes).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    hasher.update(normalize(&text).as_bytes());
    Ok(hex(&hasher.finalize()))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn comments_and_formatting_collapse() {
        let a = normalize("export declare function f(x: number): string;\n// doc\n");
        let b = normalize("/** doc */ export  declare\nfunction f(x: number): string;");
        assert_eq!(a, b);
        assert_eq!(a, "export declare function f(x: number): string;");
    }

    #[test]
    fn string_literal_whitespace_is_preserved() {
        let n = normalize("type T = \"a  b\";");
        assert_eq!(n, "type T = \"a  b\";");
        // ...including inside template literals, and escapes pass verbatim.
        assert_eq!(normalize("type U = `x  ${'y'}`;"), "type U = `x  ${'y'}`;");
        assert_eq!(normalize("type V = \"q\\\"  r\";"), "type V = \"q\\\"  r\";");
    }

    #[test]
    fn slashes_inside_strings_are_not_comments() {
        assert_eq!(normalize("import x from \"a//b\";"), "import x from \"a//b\";");
    }
}
