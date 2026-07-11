//! The `jvm-abi` content identity: hash a jar/class's ABI, not its bytes.
//! Mirrors `src/brix/jvmabi.py` — see that module's docs for what the ABI
//! includes/excludes and why. The digest is a cross-machine, cross-engine
//! cache key: the canonical payload here must serialize to the exact bytes
//! Python's `json.dumps(abi, sort_keys=True, separators=(",", ":"))`
//! produces (golden-pinned), which is why the ABI is built as
//! `fingerprint::Value` and digested by `fingerprint::digest`'s serializer.
//!
//! Faithfulness notes:
//! - Utf8 constants are carried as lowercase hex of their raw bytes (no
//!   modified-UTF-8 decoding step to disagree on across languages).
//! - Float/double constants are their raw IEEE-754 bits (`Value::UInt`).
//! - Python sorts nested lists with its polymorphic `list.__lt__`; `py_cmp`
//!   mirrors it. Where Python would raise `TypeError` (None compared against
//!   a str — possible only in malformed class files no compiler emits) the
//!   two engines may disagree, Python degrading to `:raw`; a cross-engine
//!   digest disagreement can only cause a rebuild, never a stale hit.

use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::path::Path;

use sha2::{Digest, Sha256};

use crate::fingerprint::{canonical_json, Value};
use crate::tsdts::hex;

// Constant-pool tags (JVMS Table 4.4-B).
const UTF8: u8 = 1;
const INTEGER: u8 = 3;
const FLOAT: u8 = 4;
const LONG: u8 = 5;
const DOUBLE: u8 = 6;
const CLASS: u8 = 7;
const STRING: u8 = 8;
const FIELDREF: u8 = 9;
const METHODREF: u8 = 10;
const INTERFACE_METHODREF: u8 = 11;
const NAME_AND_TYPE: u8 = 12;
const METHOD_HANDLE: u8 = 15;
const METHOD_TYPE: u8 = 16;
const DYNAMIC: u8 = 17;
const INVOKE_DYNAMIC: u8 = 18;
const MODULE: u8 = 19;
const PACKAGE: u8 = 20;

const ACC_PRIVATE: u16 = 0x0002;

type Result_<T> = Result<T, String>;

fn err<T>(msg: impl Into<String>) -> Result_<T> {
    Err(msg.into())
}

struct Reader<'a> {
    d: &'a [u8],
    p: usize,
}

impl<'a> Reader<'a> {
    fn new(d: &'a [u8]) -> Reader<'a> {
        Reader { d, p: 0 }
    }

    fn u1(&mut self) -> Result_<u8> {
        let v = *self.d.get(self.p).ok_or("truncated class file")?;
        self.p += 1;
        Ok(v)
    }

    fn u2(&mut self) -> Result_<u16> {
        let b = self.d.get(self.p..self.p + 2).ok_or("truncated class file")?;
        self.p += 2;
        Ok(u16::from_be_bytes([b[0], b[1]]))
    }

    fn u4(&mut self) -> Result_<u32> {
        let b = self.d.get(self.p..self.p + 4).ok_or("truncated class file")?;
        self.p += 4;
        Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }

    fn s4(&mut self) -> Result_<i32> {
        Ok(self.u4()? as i32)
    }

    fn s8(&mut self) -> Result_<i64> {
        Ok(((self.u4()? as u64) << 32 | self.u4()? as u64) as i64)
    }

    fn raw8(&mut self) -> Result_<u64> {
        Ok((self.u4()? as u64) << 32 | self.u4()? as u64)
    }

    /// Like Python slicing, a short tail truncates rather than errors.
    fn take(&mut self, n: usize) -> &'a [u8] {
        let end = (self.p + n).min(self.d.len());
        let b = &self.d[self.p..end];
        self.p = end;
        b
    }
}

/// One constant-pool entry (Long/Double occupy two slots; the second is None).
#[derive(Clone)]
enum Const {
    Utf8(Vec<u8>),
    Integer(i32),
    Float(u32),  // IEEE-754 bits, not the float
    Long(i64),
    Double(u64), // IEEE-754 bits
    Ref1(u8, u16),
    Ref2(u8, u16, u16),
    MethodHandle(u16, u16), // (kind as u2-widened, index) — see const_value
}

fn parse_constant_pool(r: &mut Reader) -> Result_<Vec<Option<Const>>> {
    let count = r.u2()? as usize;
    let mut pool: Vec<Option<Const>> = vec![None; count];
    let mut i = 1;
    while i < count {
        let tag = r.u1()?;
        pool[i] = Some(match tag {
            UTF8 => {
                let length = r.u2()? as usize;
                Const::Utf8(r.take(length).to_vec())
            }
            INTEGER => Const::Integer(r.s4()?),
            FLOAT => Const::Float(r.u4()?),
            LONG => Const::Long(r.s8()?),
            DOUBLE => Const::Double(r.raw8()?),
            CLASS | STRING | METHOD_TYPE | MODULE | PACKAGE => Const::Ref1(tag, r.u2()?),
            FIELDREF | METHODREF | INTERFACE_METHODREF | NAME_AND_TYPE => {
                Const::Ref2(tag, r.u2()?, r.u2()?)
            }
            METHOD_HANDLE => Const::MethodHandle(r.u1()? as u16, r.u2()?),
            DYNAMIC | INVOKE_DYNAMIC => Const::Ref2(tag, r.u2()?, r.u2()?),
            other => return err(format!("unknown constant-pool tag {other}")),
        });
        // Long and Double occupy two pool slots (JVMS 4.4.5).
        i += if matches!(tag, LONG | DOUBLE) { 2 } else { 1 };
    }
    Ok(pool)
}

/// A Utf8 constant as hex of its raw bytes — encoding-agnostic and
/// deterministic across languages.
fn utf8(pool: &[Option<Const>], index: u16) -> Result_<String> {
    match pool.get(index as usize).and_then(|e| e.as_ref()) {
        Some(Const::Utf8(bytes)) => Ok(hex(bytes)),
        _ => err(format!("constant {index} is not Utf8")),
    }
}

fn utf8_bytes(pool: &[Option<Const>], index: u16) -> Result_<&[u8]> {
    match pool.get(index as usize).and_then(|e| e.as_ref()) {
        Some(Const::Utf8(bytes)) => Ok(bytes),
        _ => err(format!("constant {index} is not Utf8")),
    }
}

fn class_name(pool: &[Option<Const>], index: u16) -> Result_<Option<String>> {
    if index == 0 {
        return Ok(None);
    }
    match pool.get(index as usize).and_then(|e| e.as_ref()) {
        Some(Const::Ref1(CLASS, name_index)) => Ok(Some(utf8(pool, *name_index)?)),
        _ => err(format!("constant {index} is not Class")),
    }
}

/// A class name in a context where Python would sort it as a str — index 0
/// (None) is malformed there and turns into an error (Python would degrade
/// to `:raw` through its own TypeError; both engines end at a rebuild).
fn class_name_str(pool: &[Option<Const>], index: u16) -> Result_<String> {
    class_name(pool, index)?.ok_or_else(|| "class reference 0 in a name list".to_string())
}

/// A canonical, order-independent form of a loadable constant.
fn const_value(pool: &[Option<Const>], index: u16) -> Result_<Value> {
    let entry = pool
        .get(index as usize)
        .and_then(|e| e.as_ref())
        .ok_or_else(|| format!("bad constant index {index}"))?;
    Ok(match entry {
        Const::Integer(v) => Value::List(vec![Value::Int(INTEGER as i64), Value::Int(*v as i64)]),
        Const::Long(v) => Value::List(vec![Value::Int(LONG as i64), Value::Int(*v)]),
        Const::Float(bits) => {
            Value::List(vec![Value::Int(FLOAT as i64), Value::Int(*bits as i64)])
        }
        Const::Double(bits) => Value::List(vec![Value::Int(DOUBLE as i64), Value::UInt(*bits)]),
        Const::Ref1(STRING, i) => {
            Value::List(vec![Value::Int(STRING as i64), Value::Str(utf8(pool, *i)?)])
        }
        Const::Ref1(CLASS, i) => {
            Value::List(vec![Value::Int(CLASS as i64), Value::Str(utf8(pool, *i)?)])
        }
        // MethodHandle/MethodType/Dynamic constants: structural refs.
        Const::Ref1(tag, a) => Value::List(vec![Value::Int(*tag as i64), Value::Int(*a as i64)]),
        Const::Ref2(tag, a, b) => Value::List(vec![
            Value::Int(*tag as i64),
            Value::Int(*a as i64),
            Value::Int(*b as i64),
        ]),
        Const::MethodHandle(kind, i) => Value::List(vec![
            Value::Int(METHOD_HANDLE as i64),
            Value::Int(*kind as i64),
            Value::Int(*i as i64),
        ]),
        Const::Utf8(_) => return err(format!("constant {index} is not loadable")),
    })
}

/// Python's polymorphic list comparison, over the Value shapes the ABI uses.
/// Cross-type comparisons (unreachable in valid class files) get a stable
/// type-rank order instead of Python's TypeError — see the module docs.
fn py_cmp(a: &Value, b: &Value) -> Ordering {
    fn rank(v: &Value) -> u8 {
        match v {
            Value::Null => 0,
            Value::Bool(_) => 1,
            Value::Int(_) | Value::UInt(_) => 2,
            Value::Str(_) => 3,
            Value::List(_) => 4,
            Value::Map(_) => 5,
        }
    }
    match (a, b) {
        (Value::Int(x), Value::Int(y)) => x.cmp(y),
        (Value::UInt(x), Value::UInt(y)) => x.cmp(y),
        (Value::Int(x), Value::UInt(y)) => {
            if *x < 0 {
                Ordering::Less
            } else {
                (*x as u64).cmp(y)
            }
        }
        (Value::UInt(_), Value::Int(_)) => py_cmp(b, a).reverse(),
        (Value::Str(x), Value::Str(y)) => x.cmp(y),
        (Value::List(x), Value::List(y)) => {
            for (xa, ya) in x.iter().zip(y.iter()) {
                let ord = py_cmp(xa, ya);
                if ord != Ordering::Equal {
                    return ord;
                }
            }
            x.len().cmp(&y.len())
        }
        _ => rank(a).cmp(&rank(b)),
    }
}

fn sort_values(values: &mut [Value]) {
    values.sort_by(py_cmp);
}

/// An attribute table, grouped exactly as Python does it: first by
/// name-*index* in first-seen order, then merged by name bytes.
struct Attributes {
    named: Vec<(Vec<u8>, Vec<Vec<u8>>)>,
}

impl Attributes {
    fn parse(pool: &[Option<Const>], r: &mut Reader) -> Result_<Attributes> {
        let count = r.u2()?;
        let mut by_index: Vec<(u16, Vec<Vec<u8>>)> = Vec::new();
        for _ in 0..count {
            let name_index = r.u2()?;
            let length = r.u4()? as usize;
            let info = r.take(length).to_vec();
            match by_index.iter_mut().find(|(i, _)| *i == name_index) {
                Some((_, infos)) => infos.push(info),
                None => by_index.push((name_index, vec![info])),
            }
        }
        let mut named: Vec<(Vec<u8>, Vec<Vec<u8>>)> = Vec::new();
        for (name_index, infos) in by_index {
            let name = utf8_bytes(pool, name_index)?.to_vec();
            match named.iter_mut().find(|(n, _)| *n == name) {
                Some((_, all)) => all.extend(infos),
                None => named.push((name, infos)),
            }
        }
        Ok(Attributes { named })
    }

    fn get(&self, name: &[u8]) -> Option<&Vec<Vec<u8>>> {
        self.named.iter().find(|(n, _)| n == name).map(|(_, infos)| infos)
    }
}

// -- annotation canonicalization (JVMS 4.7.16) --------------------------------

fn read_element_value(pool: &[Option<Const>], r: &mut Reader) -> Result_<Value> {
    let tag = r.u1()?;
    let t = tag as char;
    if t == 's' {
        // A String-valued element: the index points *directly* at a Utf8
        // constant (not a String constant), so carry it like the `c`/`e`
        // tags do — routing it through `const_value` (loadable constants)
        // would error on the Utf8 (mirrors `_read_element_value`).
        return Ok(Value::List(vec![Value::str("s"), Value::Str(utf8(pool, r.u2()?)?)]));
    }
    if "BCDFIJSZ".contains(t) {
        // Primitive-valued elements: Integer / Long / Float / Double.
        return Ok(Value::List(vec![Value::str(t.to_string()), const_value(pool, r.u2()?)?]));
    }
    Ok(match t {
        'e' => Value::List(vec![
            Value::str("e"),
            Value::Str(utf8(pool, r.u2()?)?),
            Value::Str(utf8(pool, r.u2()?)?),
        ]),
        'c' => Value::List(vec![Value::str("c"), Value::Str(utf8(pool, r.u2()?)?)]),
        '@' => Value::List(vec![Value::str("@"), read_annotation(pool, r)?]),
        '[' => {
            let n = r.u2()?;
            let mut items = Vec::with_capacity(n as usize);
            for _ in 0..n {
                items.push(read_element_value(pool, r)?);
            }
            Value::List(vec![Value::str("["), Value::List(items)])
        }
        _ => return err(format!("bad annotation element tag {tag}")),
    })
}

fn read_annotation(pool: &[Option<Const>], r: &mut Reader) -> Result_<Value> {
    let type_index = r.u2()?;
    let n = r.u2()?;
    let mut pairs = Vec::with_capacity(n as usize);
    for _ in 0..n {
        let name = utf8(pool, r.u2()?)?;
        pairs.push(Value::List(vec![Value::Str(name), read_element_value(pool, r)?]));
    }
    sort_values(&mut pairs); // element order is not significant
    Ok(Value::List(vec![Value::Str(utf8(pool, type_index)?), Value::List(pairs)]))
}

fn read_annotations(pool: &[Option<Const>], info: &[u8]) -> Result_<Vec<Value>> {
    let mut r = Reader::new(info);
    let n = r.u2()?;
    let mut anns = Vec::with_capacity(n as usize);
    for _ in 0..n {
        anns.push(read_annotation(pool, &mut r)?);
    }
    sort_values(&mut anns);
    Ok(anns)
}

fn read_parameter_annotations(pool: &[Option<Const>], info: &[u8]) -> Result_<Value> {
    let mut r = Reader::new(info);
    let num_params = r.u1()?;
    let mut params = Vec::with_capacity(num_params as usize);
    for _ in 0..num_params {
        let n = r.u2()?;
        let mut anns = Vec::with_capacity(n as usize);
        for _ in 0..n {
            anns.push(read_annotation(pool, &mut r)?);
        }
        sort_values(&mut anns);
        params.push(Value::List(anns));
    }
    Ok(Value::List(params)) // per-parameter order IS significant; do not sort
}

fn declaration_annotations(pool: &[Option<Const>], named: &Attributes) -> Result_<Vec<Value>> {
    let mut anns = Vec::new();
    for attr in [&b"RuntimeVisibleAnnotations"[..], &b"RuntimeInvisibleAnnotations"[..]] {
        if let Some(infos) = named.get(attr) {
            for info in infos {
                anns.extend(read_annotations(pool, info)?);
            }
        }
    }
    sort_values(&mut anns);
    Ok(anns)
}

// -- members and class ---------------------------------------------------------

/// A Signature attribute: a single u2 index into a Utf8 constant.
fn utf8_index(pool: &[Option<Const>], info: &[u8]) -> Result_<String> {
    utf8(pool, Reader::new(info).u2()?)
}

/// One field or method as (name, descriptor, abi); None for a private member.
type Member = (String, String, Value);

fn member_abi(pool: &[Option<Const>], r: &mut Reader) -> Result_<Option<Member>> {
    let access = r.u2()?;
    let name = utf8(pool, r.u2()?)?;
    let descriptor = utf8(pool, r.u2()?)?;
    let named = Attributes::parse(pool, r)?;
    if access & ACC_PRIVATE != 0 {
        return Ok(None);
    }
    let mut abi: BTreeMap<String, Value> = BTreeMap::new();
    abi.insert("access".into(), Value::Int(access as i64));
    abi.insert("name".into(), Value::Str(name.clone()));
    abi.insert("descriptor".into(), Value::Str(descriptor.clone()));
    if let Some(infos) = named.get(b"Signature") {
        abi.insert("signature".into(), Value::Str(utf8_index(pool, &infos[0])?));
    }
    if let Some(infos) = named.get(b"ConstantValue") {
        // static final inlined constants
        abi.insert("constant".into(), const_value(pool, Reader::new(&infos[0]).u2()?)?);
    }
    if let Some(infos) = named.get(b"Exceptions") {
        // throws clause
        let mut r2 = Reader::new(&infos[0]);
        let n = r2.u2()?;
        let mut throws = Vec::with_capacity(n as usize);
        for _ in 0..n {
            throws.push(class_name_str(pool, r2.u2()?)?);
        }
        throws.sort();
        abi.insert("throws".into(), Value::List(throws.into_iter().map(Value::Str).collect()));
    }
    let decl = declaration_annotations(pool, &named)?;
    if !decl.is_empty() {
        abi.insert("annotations".into(), Value::List(decl));
    }
    let mut params = Vec::new();
    for attr in
        [&b"RuntimeVisibleParameterAnnotations"[..], &b"RuntimeInvisibleParameterAnnotations"[..]]
    {
        if let Some(infos) = named.get(attr) {
            for info in infos {
                params.push(read_parameter_annotations(pool, info)?);
            }
        }
    }
    if !params.is_empty() {
        abi.insert("param_annotations".into(), Value::List(params));
    }
    if let Some(infos) = named.get(b"AnnotationDefault") {
        // annotation-type element defaults
        abi.insert("default".into(), read_element_value(pool, &mut Reader::new(&infos[0]))?);
    }
    Ok(Some((name, descriptor, Value::Map(abi))))
}

fn inner_classes(pool: &[Option<Const>], info: &[u8]) -> Result_<Value> {
    let mut r = Reader::new(info);
    let n = r.u2()?;
    let mut out = Vec::with_capacity(n as usize);
    for _ in 0..n {
        let inner = class_name(pool, r.u2()?)?;
        let outer_idx = r.u2()?;
        let outer = if outer_idx != 0 { class_name(pool, outer_idx)? } else { None };
        let name_idx = r.u2()?;
        let simple = if name_idx != 0 { Some(utf8(pool, name_idx)?) } else { None };
        let flags = r.u2()?;
        let opt = |v: Option<String>| v.map_or(Value::Null, Value::Str);
        out.push(Value::List(vec![
            opt(inner),
            opt(outer),
            opt(simple),
            Value::Int(flags as i64),
        ]));
    }
    sort_values(&mut out);
    Ok(Value::List(out))
}

/// The canonical ABI structure of one class file.
pub fn class_abi(data: &[u8]) -> Result_<Value> {
    let mut r = Reader::new(data);
    if r.u4()? != 0xCAFEBABE {
        return err("not a class file (bad magic)");
    }
    r.u2()?; // minor version (excluded from ABI)
    r.u2()?; // major version (excluded)
    let pool = parse_constant_pool(&mut r)?;
    let access = r.u2()?;
    let this_class = class_name(&pool, r.u2()?)?;
    let super_class = class_name(&pool, r.u2()?)?;
    let interface_count = r.u2()?;
    let mut interfaces = Vec::with_capacity(interface_count as usize);
    for _ in 0..interface_count {
        interfaces.push(class_name_str(&pool, r.u2()?)?);
    }
    interfaces.sort();

    let mut fields: Vec<Member> = Vec::new();
    for _ in 0..r.u2()? {
        if let Some(m) = member_abi(&pool, &mut r)? {
            fields.push(m);
        }
    }
    let mut methods: Vec<Member> = Vec::new();
    for _ in 0..r.u2()? {
        if let Some(m) = member_abi(&pool, &mut r)? {
            methods.push(m);
        }
    }
    let named = Attributes::parse(&pool, &mut r)?;

    fields.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    methods.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    let members = |ms: Vec<Member>| Value::List(ms.into_iter().map(|(_, _, v)| v).collect());
    let opt = |v: Option<String>| v.map_or(Value::Null, Value::Str);

    let mut abi: BTreeMap<String, Value> = BTreeMap::new();
    abi.insert("access".into(), Value::Int(access as i64));
    abi.insert("name".into(), opt(this_class));
    abi.insert("super".into(), opt(super_class));
    abi.insert("interfaces".into(), Value::List(interfaces.into_iter().map(Value::Str).collect()));
    abi.insert("fields".into(), members(fields));
    abi.insert("methods".into(), members(methods));
    if let Some(infos) = named.get(b"Signature") {
        abi.insert("signature".into(), Value::Str(utf8_index(&pool, &infos[0])?));
    }
    let decl = declaration_annotations(&pool, &named)?;
    if !decl.is_empty() {
        abi.insert("annotations".into(), Value::List(decl));
    }
    // Accessibility / subclassing structure.
    if let Some(infos) = named.get(b"NestHost") {
        abi.insert(
            "nest_host".into(),
            opt(class_name(&pool, Reader::new(&infos[0]).u2()?)?),
        );
    }
    for (attr, key) in [(&b"NestMembers"[..], "nest_members"), (&b"PermittedSubclasses"[..], "permitted")] {
        if let Some(infos) = named.get(attr) {
            let mut r2 = Reader::new(&infos[0]);
            let n = r2.u2()?;
            let mut names = Vec::with_capacity(n as usize);
            for _ in 0..n {
                names.push(class_name_str(&pool, r2.u2()?)?);
            }
            names.sort();
            abi.insert(key.into(), Value::List(names.into_iter().map(Value::Str).collect()));
        }
    }
    if let Some(infos) = named.get(b"InnerClasses") {
        abi.insert("inner".into(), inner_classes(&pool, &infos[0])?);
    }
    Ok(Value::Map(abi))
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex(&hasher.finalize())
}

pub fn digest_class(data: &[u8]) -> Result_<String> {
    Ok(sha256_hex(canonical_json(&class_abi(data)?).as_bytes()))
}

/// ABI digest of a jar: per-class digests in sorted-name order, concatenated
/// and hashed. Non-class entries (resources, manifest) are ignored.
pub fn digest_jar(path: &Path) -> Result_<String> {
    let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| e.to_string())?;
    let mut names: Vec<String> = archive.file_names().map(str::to_string).collect();
    names.sort();
    let mut parts = String::new();
    for name in names {
        if !name.ends_with(".class") {
            continue;
        }
        let mut entry = archive.by_name(&name).map_err(|e| e.to_string())?;
        let mut data = Vec::new();
        std::io::Read::read_to_end(&mut entry, &mut data).map_err(|e| e.to_string())?;
        parts.push_str(&digest_class(&data)?);
    }
    Ok(sha256_hex(parts.as_bytes()))
}

/// ABI digest of a jar or a single `.class` file. `Err` on anything the
/// parser cannot interpret — the engine falls back to a `:raw` byte hash.
pub fn digest(path: &Path) -> Result_<String> {
    if let Ok(file) = std::fs::File::open(path) {
        if zip::ZipArchive::new(file).is_ok() {
            return digest_jar(path);
        }
    }
    let data = std::fs::read(path).map_err(|e| e.to_string())?;
    digest_class(&data)
}
