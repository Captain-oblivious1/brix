//! Rust mirror of `tests/test_content_identity.py` and
//! `tests/test_ts_dts_identity.py` — same cases, same names. The `.class`
//! fixtures are the committed ones under `tests/fixtures/jvmabi/` and the
//! `EXPECTED` digests are the exact values pinned in the Python test: both
//! implementations must produce them, so the two suites cross-check each
//! other byte-for-byte. Registry/plugin cases that exercise Python's dynamic
//! registration have no Rust counterpart (built-ins are compiled in).

use std::io::Write;
use std::path::{Path, PathBuf};

use brix_core::{
    action_fingerprint, jvmabi, tsdts, Action, BuildMetadata, NodeContext,
    TimeAndHashChangeDetector,
};

// The golden contract, identical to EXPECTED in test_content_identity.py. A
// change on either side that these do not catch would be a silent cross-engine
// cache divergence.
const EXPECTED: [(&str, &str); 5] = [
    ("base", "70286f3e666e92342509bcbd9f7f4855ccf3d20c4b6e025ea0f7b7899d137b18"),
    ("impl", "70286f3e666e92342509bcbd9f7f4855ccf3d20c4b6e025ea0f7b7899d137b18"),
    ("addmethod", "90c437bf23a6b8c1289b441eec393dcbeee425bf9fff20f05d65f69ecaae441f"),
    ("constant", "36f7d159f8b658b7b391441a5acc616f76169868764c2c13a1e14dbf0b333b13"),
    ("annotation", "140b9c778e2bdccc225e15f24c7e11f470777ecc8fec03b656d6f1fe3ecb48d4"),
];

fn cls(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/jvmabi")
        .join(format!("{name}.class"));
    std::fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()))
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "brix-identity-{tag}-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn jar(dir: &Path, fixture: &str, name: &str) -> PathBuf {
    let path = dir.join(name);
    let file = std::fs::File::create(&path).unwrap();
    let mut zip = zip::ZipWriter::new(file);
    zip.start_file("abidemo/Lib.class", zip::write::SimpleFileOptions::default()).unwrap();
    zip.write_all(&cls(fixture)).unwrap();
    zip.finish().unwrap();
    path
}

/// The fingerprint of an action whose input carries `identity=`, as the
/// incremental/cache layers would compute it (mirrors `_dependent_fingerprint`
/// in both Python files; the spec JSON is the shape `brix.lowering` emits).
fn identity_fingerprint(root: &Path, path: &str, identity: &str) -> Result<String, String> {
    let spec = format!(
        r#"{{"kind": "process", "name": "x", "command_line": "use {{f}}",
            "inputs": {{"f": [{{"kind": "file", "path": "{path}", "identity": "{identity}"}}]}},
            "outputs": {{}}}}"#
    );
    let action: Action = serde_json::from_str(&spec).unwrap();
    action_fingerprint(&action, &NodeContext::for_root(root), &TimeAndHashChangeDetector::new())
}

// -- the parser (test_content_identity.py) -----------------------------------

#[test]
fn pinned_class_digests() {
    for (name, digest) in EXPECTED {
        assert_eq!(jvmabi::digest_class(&cls(name)).unwrap(), digest, "{name}");
    }
}

#[test]
fn implementation_only_change_hashes_equal() {
    // Different bodies, different private field init, different private
    // method: the API is identical, so the ABI digest must be too.
    assert_eq!(jvmabi::digest_class(&cls("base")), jvmabi::digest_class(&cls("impl")));
}

#[test]
fn api_changes_each_hash_differently() {
    let base = jvmabi::digest_class(&cls("base")).unwrap();
    let changed: std::collections::HashSet<String> = ["addmethod", "constant", "annotation"]
        .iter()
        .map(|n| jvmabi::digest_class(&cls(n)).unwrap())
        .collect();
    assert!(!changed.contains(&base));
    assert_eq!(changed.len(), 3);
}

#[test]
fn jar_abi_impl_only_equal() {
    let dir = scratch("jar-impl");
    assert_eq!(
        jvmabi::digest_jar(&jar(&dir, "base", "a.jar")),
        jvmabi::digest_jar(&jar(&dir, "impl", "b.jar"))
    );
}

#[test]
fn jar_abi_api_change_differs() {
    let dir = scratch("jar-api");
    assert_ne!(
        jvmabi::digest_jar(&jar(&dir, "base", "a.jar")),
        jvmabi::digest_jar(&jar(&dir, "addmethod", "b.jar"))
    );
}

#[test]
fn digest_autodetects_jar_vs_class() {
    let dir = scratch("autodetect");
    let j = jar(&dir, "base", "util.jar");
    assert_eq!(jvmabi::digest(&j), jvmabi::digest_jar(&j));
    let cf = dir.join("base.class");
    std::fs::write(&cf, cls("base")).unwrap();
    assert_eq!(jvmabi::digest(&cf).unwrap(), jvmabi::digest_class(&cls("base")).unwrap());
}

#[test]
fn malformed_class_raises() {
    assert!(jvmabi::digest_class(b"not a class file").is_err());
}

// -- the engine wiring --------------------------------------------------------

#[test]
fn non_jar_with_identity_falls_back_to_raw() {
    let dir = scratch("raw-fallback");
    let target = dir.join("x.jar");
    std::fs::write(&target, "not a jar at all").unwrap();
    let first = identity_fingerprint(&dir, "x.jar", "jvm-abi").unwrap();
    assert_eq!(first.len(), 64); // did not error
    // The :raw fallback is a byte hash, so different bytes -> different key.
    std::fs::write(&target, "still not a jar, but different bytes").unwrap();
    assert_ne!(first, identity_fingerprint(&dir, "x.jar", "jvm-abi").unwrap());
}

#[test]
fn unknown_identity_is_loud() {
    // In the full system an unknown name never reaches the core (the Python
    // skin's `supports_rust_engine` keeps custom-identity graphs on the
    // Python engine), but a wire spec naming one must still error, not guess.
    let dir = scratch("unknown-identity");
    std::fs::write(dir.join("x.jar"), "data").unwrap();
    let err = identity_fingerprint(&dir, "x.jar", "no-such-identity").unwrap_err();
    assert!(err.contains("no-such-identity"), "{err}");
}

// -- metadata cache -----------------------------------------------------------

#[test]
fn metadata_caches_identity_alongside_byte_hash() {
    let meta = BuildMetadata::with_directory(scratch("meta-cache"), "meta");
    let sig = [123, 45];
    meta.store_hash("/p", &sig, "BYTES");
    meta.store_identity("/p", &sig, "jvm-abi@1", "ABI");
    // Both live in one stat-validated entry.
    assert_eq!(meta.cached_hash("/p", &sig).unwrap(), "BYTES");
    assert_eq!(meta.cached_identity("/p", &sig, "jvm-abi@1").unwrap(), "ABI");
    // A changed signature invalidates both.
    assert_eq!(meta.cached_hash("/p", &[999, 9]), None);
    assert_eq!(meta.cached_identity("/p", &[999, 9], "jvm-abi@1"), None);
    // Re-hashing under a new signature drops the now-stale identity digest.
    meta.store_hash("/p", &[999, 9], "NEWBYTES");
    assert_eq!(meta.cached_identity("/p", &[999, 9], "jvm-abi@1"), None);
}

#[test]
fn identity_digest_cached_by_signature() {
    // Python proves the cache hit with a spy plugin; built-ins are compiled in
    // here, so instead seed the metadata with a sentinel digest under the
    // file's current signature and observe the detector return it unrecomputed.
    let dir = scratch("digest-cached");
    let j = jar(&dir, "base", "util.jar");
    let meta = std::sync::Arc::new(BuildMetadata::with_directory(&dir, "meta"));
    let det = TimeAndHashChangeDetector::with_metadata(meta.clone());
    let sig = det.stat_signature(&j).unwrap();
    meta.store_identity(&j.to_string_lossy(), &sig, "jvm-abi@1", "SENTINEL");
    assert_eq!(det.identity_digest(&j, "jvm-abi").unwrap().unwrap(), "SENTINEL");
    // A real (uncached) computation stores its result for the next call.
    let real = TimeAndHashChangeDetector::with_metadata(std::sync::Arc::new(
        BuildMetadata::with_directory(&dir, "meta2"),
    ))
    .identity_digest(&j, "jvm-abi")
    .unwrap()
    .unwrap();
    assert_eq!(real, jvmabi::digest_jar(&j).unwrap());
}

// -- ts-dts (test_ts_dts_identity.py) ------------------------------------------

const BASE: &str = "\
// the public API of the util module
export declare function greet(name: string): string;
export interface Options {
    loud: boolean;
}
";

// Same public surface, different doc comments and formatting only.
const IMPL_ONLY: &str = "\
/** Greets a person. Reworded doc, reindented — API unchanged. */
export declare function greet(name:    string): string;

export interface Options { loud: boolean; }
";

// A real API change: a new optional parameter.
const API_CHANGE: &str = "\
export declare function greet(name: string, times?: number): string;
export interface Options {
    loud: boolean;
}
";

#[test]
fn comment_and_format_changes_hash_equal() {
    let dir = scratch("dts-impl");
    let a = dir.join("a.d.ts");
    let b = dir.join("b.d.ts");
    std::fs::write(&a, BASE).unwrap();
    std::fs::write(&b, IMPL_ONLY).unwrap();
    assert_eq!(tsdts::digest(&a), tsdts::digest(&b));
}

#[test]
fn api_change_hashes_differently() {
    let dir = scratch("dts-api");
    let a = dir.join("a.d.ts");
    let c = dir.join("c.d.ts");
    std::fs::write(&a, BASE).unwrap();
    std::fs::write(&c, API_CHANGE).unwrap();
    assert_ne!(tsdts::digest(&a), tsdts::digest(&c));
}

#[test]
fn whitespace_inside_string_literal_is_significant() {
    // "a  b" and "a b" are different string-literal *types*; formatting
    // collapse must not conflate them, while whitespace outside strings is.
    assert_ne!(
        tsdts::normalize(r#"export type T = "a  b";"#),
        tsdts::normalize(r#"export type T = "a b";"#)
    );
    assert_eq!(
        tsdts::normalize("export  type   T = X;"),
        tsdts::normalize("export type T = X;")
    );
}

#[test]
fn double_slash_inside_string_is_not_a_comment() {
    let kept = tsdts::normalize(r#"export const u: "http://x/y";"#);
    assert!(kept.contains("http://x/y"), "{kept}");
}

#[test]
fn dependent_fingerprint_ignores_impl_and_doc_change() {
    let (a, b) = (scratch("dts-fp-a"), scratch("dts-fp-b"));
    std::fs::write(a.join("api.d.ts"), BASE).unwrap();
    std::fs::write(b.join("api.d.ts"), IMPL_ONLY).unwrap();
    assert_eq!(
        identity_fingerprint(&a, "api.d.ts", "ts-dts").unwrap(),
        identity_fingerprint(&b, "api.d.ts", "ts-dts").unwrap()
    );
}

#[test]
fn dependent_fingerprint_reacts_to_api_change() {
    let (a, c) = (scratch("dts-fp-base"), scratch("dts-fp-change"));
    std::fs::write(a.join("api.d.ts"), BASE).unwrap();
    std::fs::write(c.join("api.d.ts"), API_CHANGE).unwrap();
    assert_ne!(
        identity_fingerprint(&a, "api.d.ts", "ts-dts").unwrap(),
        identity_fingerprint(&c, "api.d.ts", "ts-dts").unwrap()
    );
}

#[test]
fn undecodable_file_falls_back_to_raw() {
    // A non-text file under ts-dts identity must not crash — digest errors and
    // the engine byte-hashes under :raw, so different bytes still differ.
    let dir = scratch("dts-raw");
    let target = dir.join("api.d.ts");
    std::fs::write(&target, b"\xff\xfe\x00\x01 not utf-8").unwrap();
    let first = identity_fingerprint(&dir, "api.d.ts", "ts-dts").unwrap();
    assert_eq!(first.len(), 64);
    std::fs::write(&target, b"\xff\xfe\x00\x02 different bytes").unwrap();
    assert_ne!(first, identity_fingerprint(&dir, "api.d.ts", "ts-dts").unwrap());
}
