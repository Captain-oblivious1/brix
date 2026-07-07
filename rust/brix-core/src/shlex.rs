//! POSIX word splitting, matching Python's `shlex.split(s)` (which uses
//! `posix=True, whitespace_split=True`) for the subset brix relies on:
//! whitespace separation, single quotes (fully literal), double quotes
//! (backslash escapes `"` and `\`), and backslash escaping outside quotes.
//! An unterminated quote is an error, exactly as Python raises `ValueError`.
//!
//! Mirrors the use in `src/brix/process.py`; pinned by golden fixtures.

/// Whitespace that separates words (Python `shlex.whitespace` default).
fn is_ws(c: char) -> bool {
    c == ' ' || c == '\t' || c == '\r' || c == '\n'
}

pub fn split(input: &str) -> Result<Vec<String>, String> {
    let chars: Vec<char> = input.chars().collect();
    let mut i = 0;
    let mut words: Vec<String> = Vec::new();
    // `current` is Some once a word has started — even an empty word from `""`.
    let mut current: Option<String> = None;

    while i < chars.len() {
        let c = chars[i];
        if is_ws(c) {
            if let Some(w) = current.take() {
                words.push(w);
            }
            i += 1;
        } else if c == '\'' {
            // Single quote: everything literal until the next single quote.
            let buf = current.get_or_insert_with(String::new);
            i += 1;
            let start = i;
            while i < chars.len() && chars[i] != '\'' {
                i += 1;
            }
            if i >= chars.len() {
                return Err("No closing quotation".to_string());
            }
            buf.extend(&chars[start..i]);
            i += 1; // closing '
        } else if c == '"' {
            // Double quote: backslash escapes only `"` and `\`; else literal.
            let buf = current.get_or_insert_with(String::new);
            i += 1;
            loop {
                if i >= chars.len() {
                    return Err("No closing quotation".to_string());
                }
                let d = chars[i];
                if d == '"' {
                    i += 1;
                    break;
                }
                if d == '\\' && i + 1 < chars.len() && (chars[i + 1] == '"' || chars[i + 1] == '\\')
                {
                    buf.push(chars[i + 1]);
                    i += 2;
                } else {
                    buf.push(d);
                    i += 1;
                }
            }
        } else if c == '\\' {
            // Outside quotes: backslash escapes the next char literally.
            let buf = current.get_or_insert_with(String::new);
            if i + 1 < chars.len() {
                buf.push(chars[i + 1]);
                i += 2;
            } else {
                // Trailing backslash: Python raises "No escaped character".
                return Err("No escaped character".to_string());
            }
        } else {
            current.get_or_insert_with(String::new).push(c);
            i += 1;
        }
    }
    if let Some(w) = current.take() {
        words.push(w);
    }
    Ok(words)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_and_quotes() {
        assert_eq!(split("a b c").unwrap(), vec!["a", "b", "c"]);
        assert_eq!(split("  spaced   out ").unwrap(), vec!["spaced", "out"]);
        assert_eq!(split("").unwrap(), Vec::<String>::new());
        assert_eq!(split("'a b' c").unwrap(), vec!["a b", "c"]);
        assert_eq!(split("a'b'c").unwrap(), vec!["abc"]); // adjacency concatenates
        assert_eq!(split(r#""a\"b""#).unwrap(), vec!["a\"b"]);
    }

    #[test]
    fn unterminated_is_error() {
        assert!(split("'no close").is_err());
        assert!(split("\"no close").is_err());
    }
}
