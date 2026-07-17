//! The `kotlin-abi` content identity: jvm-abi, except inline counts by bytes.
//! Mirrors `src/brix/kotlinabi.py` — see that module's docs for the leak
//! channels (inline bodies, their line tables, synthetics regenerated into
//! consumers) and the format facts (BitEncoding, protobuf field numbers,
//! flag bits), all pinned from the Kotlin compiler's own sources. The digest
//! is a cross-machine, cross-engine cache key: golden-pinned against the
//! Python reference (tests/fixtures/kotlinabi/), and a version bump in one
//! implementation must land in both.
//!
//! Per class in a jar: no `@Metadata` → jvm-abi class digest; Kotlin metadata
//! with no inline function/accessor → jvm-abi class digest (the recovered
//! skip); any inline member, nesting under such a class (`$`-prefixed name),
//! or metadata this parser will not vouch for → whole-class byte hash. Fail
//! closed: wrong in the slow direction, never stale.

use std::collections::BTreeSet;
use std::path::Path;

use sha2::{Digest, Sha256};

use crate::fingerprint::{canonical_json, Value};
use crate::lenses::jvmabi;
use crate::tsdts::hex;

type Result_<T> = Result<T, String>;

// @Metadata kinds (kotlin.Metadata.kind).
const KIND_CLASS: i64 = 1;
const KIND_FILE_FACADE: i64 = 2;
const KIND_SYNTHETIC: i64 = 3;
const KIND_MULTIFILE_PART: i64 = 5;

/// Metadata major versions this parser is calibrated against.
const SUPPORTED_MAJORS: [i64; 2] = [1, 2];

const FUNCTION_IS_INLINE: u64 = 1 << 10; // Flags.java: IS_INLINE
const ACCESSOR_IS_INLINE: u64 = 1 << 8; // Flags.java: IS_INLINE_ACCESSOR

const INTEGER: i64 = 3; // constant-pool tag inside canonical primitive elements

/// Metadata we will not vouch for — the class falls back to byte hashing.
struct Unparseable;
type Parsed<T> = Result<T, Unparseable>;

fn bad<T>() -> Parsed<T> {
    Err(Unparseable)
}

fn from_hex(s: &str) -> Parsed<Vec<u8>> {
    if s.len() % 2 != 0 {
        return bad();
    }
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).map_err(|_| Unparseable))
        .collect()
}

// -- @Metadata extraction from the canonical class ABI ------------------------

/// The @Metadata annotation's `(element name hex, value)` pairs, or None for
/// a non-Kotlin class.
fn metadata_elements(abi: &Value) -> Option<Vec<(&str, &Value)>> {
    let metadata_type = hex(b"Lkotlin/Metadata;");
    let Value::Map(map) = abi else { return None };
    let Some(Value::List(annotations)) = map.get("annotations") else {
        return None;
    };
    for ann in annotations {
        let Value::List(parts) = ann else { continue };
        let [Value::Str(type_hex), Value::List(pairs)] = parts.as_slice() else {
            continue;
        };
        if type_hex != &metadata_type {
            continue;
        }
        let mut out = Vec::new();
        for pair in pairs {
            let Value::List(kv) = pair else { continue };
            if let [Value::Str(name), value] = kv.as_slice() {
                out.push((name.as_str(), value));
            }
        }
        return Some(out);
    }
    None
}

fn element<'a>(elements: &[(&str, &'a Value)], name_hex: &str) -> Parsed<&'a Value> {
    elements
        .iter()
        .find(|(n, _)| *n == name_hex)
        .map(|(_, v)| *v)
        .ok_or(Unparseable)
}

/// Canonical form of an int-valued element: `["I", [INTEGER, n]]`.
fn int_element(value: &Value) -> Parsed<i64> {
    let Value::List(parts) = value else { return bad() };
    let [Value::Str(tag), Value::List(cv)] = parts.as_slice() else {
        return bad();
    };
    if tag != "I" {
        return bad();
    }
    let [Value::Int(pool_tag), Value::Int(n)] = cv.as_slice() else {
        return bad();
    };
    if *pool_tag != INTEGER {
        return bad();
    }
    Ok(*n)
}

fn int_array_element(value: &Value) -> Parsed<Vec<i64>> {
    let Value::List(parts) = value else { return bad() };
    let [Value::Str(tag), Value::List(items)] = parts.as_slice() else {
        return bad();
    };
    if tag != "[" {
        return bad();
    }
    items.iter().map(int_element).collect()
}

/// A String-array element's strings, as raw modified-UTF-8 bytes.
fn string_array_bytes(value: &Value) -> Parsed<Vec<Vec<u8>>> {
    let Value::List(parts) = value else { return bad() };
    let [Value::Str(tag), Value::List(items)] = parts.as_slice() else {
        return bad();
    };
    if tag != "[" {
        return bad();
    }
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let Value::List(sv) = item else { return bad() };
        let [Value::Str(t), Value::Str(hex_str)] = sv.as_slice() else {
            return bad();
        };
        if t != "s" {
            return bad();
        }
        out.push(from_hex(hex_str)?);
    }
    Ok(out)
}

/// Modified UTF-8 (JVMS 4.4.7) to UTF-16 code units.
fn mutf8_chars(data: &[u8]) -> Parsed<Vec<u16>> {
    let mut out = Vec::with_capacity(data.len());
    let mut i = 0;
    let n = data.len();
    while i < n {
        let b = data[i];
        if (0x01..=0x7F).contains(&b) {
            out.push(b as u16);
            i += 1;
        } else if (0xC0..=0xDF).contains(&b) && i + 1 < n && data[i + 1] & 0xC0 == 0x80 {
            out.push((((b & 0x1F) as u16) << 6) | (data[i + 1] & 0x3F) as u16);
            i += 2;
        } else if (0xE0..=0xEF).contains(&b)
            && i + 2 < n
            && data[i + 1] & 0xC0 == 0x80
            && data[i + 2] & 0xC0 == 0x80
        {
            out.push(
                (((b & 0x0F) as u16) << 12)
                    | (((data[i + 1] & 0x3F) as u16) << 6)
                    | (data[i + 2] & 0x3F) as u16,
            );
            i += 3;
        } else {
            return bad();
        }
    }
    Ok(out)
}

/// BitEncoding.decodeBytes, UTF-8 mode only (the modern encoding): drop the
/// `\u{0}` marker, then every char is one byte. The legacy 8-to-7 packing
/// (marker `\u{ffff}`) is not vouched for.
fn decode_d1(strings: &[Vec<u8>]) -> Parsed<Vec<u8>> {
    if strings.is_empty() {
        return bad();
    }
    let decoded: Vec<Vec<u16>> = strings
        .iter()
        .map(|s| mutf8_chars(s))
        .collect::<Parsed<_>>()?;
    if decoded[0].is_empty() || decoded[0][0] != 0x0000 {
        return bad();
    }
    let mut out = Vec::new();
    for (idx, chars) in decoded.iter().enumerate() {
        for &c in &chars[if idx == 0 { 1 } else { 0 }..] {
            if c > 0xFF {
                return bad();
            }
            out.push(c as u8);
        }
    }
    Ok(out)
}

// -- just enough protobuf to find the inline flags -----------------------------

struct Proto<'a> {
    d: &'a [u8],
    p: usize,
}

impl<'a> Proto<'a> {
    fn new(d: &'a [u8]) -> Self {
        Proto { d, p: 0 }
    }

    fn at_end(&self) -> bool {
        self.p >= self.d.len()
    }

    fn varint(&mut self) -> Parsed<u64> {
        let mut result: u64 = 0;
        let mut shift = 0;
        loop {
            if self.p >= self.d.len() {
                return bad();
            }
            let b = self.d[self.p];
            self.p += 1;
            result |= ((b & 0x7F) as u64) << shift;
            if b & 0x80 == 0 {
                return Ok(result);
            }
            shift += 7;
            if shift > 63 {
                return bad();
            }
        }
    }

    fn advance(&mut self, n: u64) -> Parsed<()> {
        let n = usize::try_from(n).map_err(|_| Unparseable)?;
        if self.p + n > self.d.len() {
            return bad();
        }
        self.p += n;
        Ok(())
    }

    fn bytes_field(&mut self) -> Parsed<&'a [u8]> {
        let n = usize::try_from(self.varint()?).map_err(|_| Unparseable)?;
        if self.p + n > self.d.len() {
            return bad();
        }
        let b = &self.d[self.p..self.p + n];
        self.p += n;
        Ok(b)
    }

    fn skip(&mut self, wire: u64) -> Parsed<()> {
        match wire {
            0 => self.varint().map(|_| ()),
            1 => self.advance(8),
            2 => {
                let n = self.varint()?;
                self.advance(n)
            }
            5 => self.advance(4),
            _ => bad(), // groups (3/4) or garbage: metadata.proto uses neither
        }
    }
}

/// ProtoBuf.Function: flags = 9. A message carrying only the pre-1.0
/// `old_flags` (field 1, different bit layout) is not vouched for.
fn function_is_inline(data: &[u8]) -> Parsed<bool> {
    let mut r = Proto::new(data);
    let mut flags = None;
    let mut saw_old_flags = false;
    while !r.at_end() {
        let key = r.varint()?;
        let (field, wire) = (key >> 3, key & 7);
        if field == 9 && wire == 0 {
            flags = Some(r.varint()?);
        } else if field == 1 && wire == 0 {
            saw_old_flags = true;
            r.varint()?;
        } else {
            r.skip(wire)?;
        }
    }
    match flags {
        None if saw_old_flags => bad(),
        None => Ok(false),
        Some(f) => Ok(f & FUNCTION_IS_INLINE != 0),
    }
}

/// ProtoBuf.Property: getter_flags = 7, setter_flags = 8; absent accessor
/// flags mean "not inline" by definition (metadata.proto). `old_flags`
/// without the modern `flags` (field 11) is not vouched for.
fn property_has_inline_accessor(data: &[u8]) -> Parsed<bool> {
    let mut r = Proto::new(data);
    let mut inline = false;
    let mut flags = None;
    let mut saw_old_flags = false;
    while !r.at_end() {
        let key = r.varint()?;
        let (field, wire) = (key >> 3, key & 7);
        if (field == 7 || field == 8) && wire == 0 {
            inline = inline || r.varint()? & ACCESSOR_IS_INLINE != 0;
        } else if field == 11 && wire == 0 {
            flags = Some(r.varint()?);
        } else if field == 1 && wire == 0 {
            saw_old_flags = true;
            r.varint()?;
        } else {
            r.skip(wire)?;
        }
    }
    if flags.is_none() && saw_old_flags {
        return bad();
    }
    Ok(inline)
}

/// Scan a Class (kind 1) or Package (kinds 2/5) message for any inline
/// function or inline property accessor.
fn payload_has_inline(kind: i64, d1: &[u8]) -> Parsed<bool> {
    let mut r = Proto::new(d1);
    let table = r.varint()?; // the varint-delimited StringTableTypes message
    r.advance(table)?;
    let (fn_field, prop_field) = if kind == KIND_CLASS { (9, 10) } else { (3, 4) };
    while !r.at_end() {
        let key = r.varint()?;
        let (field, wire) = (key >> 3, key & 7);
        if field == fn_field && wire == 2 {
            if function_is_inline(r.bytes_field()?)? {
                return Ok(true);
            }
        } else if field == prop_field && wire == 2 {
            if property_has_inline_accessor(r.bytes_field()?)? {
                return Ok(true);
            }
        } else {
            r.skip(wire)?;
        }
    }
    Ok(false)
}

// -- per-class mode ------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Abi,
    Raw,
}

impl Mode {
    fn label(self) -> &'static str {
        match self {
            Mode::Abi => "abi",
            Mode::Raw => "raw",
        }
    }
}

/// The class's own mode, from its own metadata. Nesting under an
/// inline-carrying class is the jar level's job (`digest_jar`).
fn class_mode(abi: &Value) -> Mode {
    let Some(elements) = metadata_elements(abi) else {
        return Mode::Abi; // not a Kotlin class: jvm-abi semantics are sound
    };
    match class_mode_inner(&elements) {
        Ok(mode) => mode,
        Err(Unparseable) => Mode::Raw, // fail closed: never go stale
    }
}

fn class_mode_inner(elements: &[(&str, &Value)]) -> Parsed<Mode> {
    let mv = int_array_element(element(elements, &hex(b"mv"))?)?;
    if mv.is_empty() || !SUPPORTED_MAJORS.contains(&mv[0]) {
        return bad(); // uncalibrated metadata version
    }
    let kind = int_element(element(elements, &hex(b"k"))?)?;
    if kind == KIND_SYNTHETIC {
        // Lambdas, DefaultImpls, WhenMappings: no member list of their own.
        // Consumers only read them via inlining, and then they nest under the
        // inline-carrying class — the jar level catches that.
        return Ok(Mode::Abi);
    }
    if kind == KIND_CLASS || kind == KIND_FILE_FACADE || kind == KIND_MULTIFILE_PART {
        let d1 = decode_d1(&string_array_bytes(element(elements, &hex(b"d1"))?)?)?;
        return Ok(if payload_has_inline(kind, &d1)? {
            Mode::Raw
        } else {
            Mode::Abi
        });
    }
    // Multifile facades (kind 4) and anything newer: not vouched for.
    bad()
}

/// Is any `$`-prefix ancestor of this binary name flagged? Nested and
/// synthetic classes (`Outer$Inner`, `CoreKt$combine$1`) carry the declaring
/// class's name as a prefix.
fn enclosed_in(name: &[u8], flagged: &BTreeSet<Vec<u8>>) -> bool {
    let mut end = name.len();
    while let Some(i) = name[..end].iter().rposition(|&b| b == b'$') {
        if i > 0 && flagged.contains(&name[..i]) {
            return true;
        }
        end = i;
        if end == 0 {
            break;
        }
    }
    false
}

/// (binary name, own mode, canonical ABI) for one class file.
fn class_facts(data: &[u8]) -> Result_<(Vec<u8>, Mode, Value)> {
    let abi = jvmabi::class_abi(data)?;
    let name = match &abi {
        Value::Map(map) => match map.get("name") {
            Some(Value::Str(name_hex)) => from_hex(name_hex).map_err(|_| "bad class name hex")?,
            _ => Vec::new(),
        },
        _ => Vec::new(),
    };
    let mode = class_mode(&abi);
    Ok((name, mode, abi))
}

fn part(name: &[u8], mode: Mode, digest: &str) -> String {
    format!("{}:{}:{}", hex(name), mode.label(), digest)
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex(&hasher.finalize())
}

fn abi_digest(abi: &Value) -> String {
    sha256_hex(canonical_json(abi).as_bytes())
}

// -- digests -------------------------------------------------------------------

/// kotlin-abi digest of a single class file. Without a jar for context, a
/// `$`-nested name could be a lambda regenerated from an inline body, so it
/// counts by bytes.
pub fn digest_class(data: &[u8]) -> Result_<String> {
    let (name, mut mode, abi) = class_facts(data)?;
    if mode == Mode::Abi && name.contains(&b'$') {
        mode = Mode::Raw;
    }
    let digest = match mode {
        Mode::Abi => abi_digest(&abi),
        Mode::Raw => sha256_hex(data),
    };
    Ok(sha256_hex(part(&name, mode, &digest).as_bytes()))
}

/// kotlin-abi digest of a jar. Two passes: decide each class's own mode, then
/// flow `raw` down to everything nested under a flagged name. Also counts
/// `META-INF/*.kotlin_module` entries (multifile/package-part tables the
/// compiler reads) by bytes; other resources are ignored, as in jvm-abi.
pub fn digest_jar(path: &Path) -> Result_<String> {
    let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| e.to_string())?;
    let mut names: Vec<String> = archive.file_names().map(str::to_string).collect();
    names.sort();
    let mut classes: Vec<(Vec<u8>, Mode, Value, String)> = Vec::new();
    let mut parts: Vec<String> = Vec::new();
    let mut flagged: BTreeSet<Vec<u8>> = BTreeSet::new();
    for entry_name in names {
        let is_class = entry_name.ends_with(".class");
        let is_module =
            entry_name.starts_with("META-INF/") && entry_name.ends_with(".kotlin_module");
        if !is_class && !is_module {
            continue;
        }
        let mut entry = archive.by_name(&entry_name).map_err(|e| e.to_string())?;
        let mut data = Vec::new();
        std::io::Read::read_to_end(&mut entry, &mut data).map_err(|e| e.to_string())?;
        if is_class {
            let (name, mode, abi) = class_facts(&data)?;
            if mode == Mode::Raw {
                flagged.insert(name.clone());
            }
            classes.push((name, mode, abi, sha256_hex(&data)));
        } else {
            parts.push(format!("module:{}", sha256_hex(&data)));
        }
    }
    for (name, mut mode, abi, raw) in classes {
        if mode == Mode::Abi && enclosed_in(&name, &flagged) {
            mode = Mode::Raw;
        }
        let digest = match mode {
            Mode::Abi => abi_digest(&abi),
            Mode::Raw => raw,
        };
        parts.push(part(&name, mode, &digest));
    }
    parts.sort();
    Ok(sha256_hex(parts.join("\n").as_bytes()))
}

/// kotlin-abi digest of a jar or a single `.class` file. `Err` on anything
/// the class parser cannot interpret — the engine falls back to a `:raw`
/// byte hash of the whole file.
pub fn digest(path: &Path) -> Result_<String> {
    if let Ok(file) = std::fs::File::open(path) {
        if zip::ZipArchive::new(file).is_ok() {
            return digest_jar(path);
        }
    }
    let data = std::fs::read(path).map_err(|e| e.to_string())?;
    digest_class(&data)
}
