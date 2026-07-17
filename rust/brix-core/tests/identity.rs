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
    action_fingerprint, jvmabi, kotlinabi, tsdts, Action, BuildMetadata, NodeContext,
    TimeAndHashChangeDetector,
};

// The golden contract, identical to EXPECTED in test_content_identity.py. A
// change on either side that these do not catch would be a silent cross-engine
// cache divergence.
const EXPECTED: [(&str, &str); 6] = [
    ("base", "70286f3e666e92342509bcbd9f7f4855ccf3d20c4b6e025ea0f7b7899d137b18"),
    ("impl", "70286f3e666e92342509bcbd9f7f4855ccf3d20c4b6e025ea0f7b7899d137b18"),
    ("addmethod", "90c437bf23a6b8c1289b441eec393dcbeee425bf9fff20f05d65f69ecaae441f"),
    ("constant", "36f7d159f8b658b7b391441a5acc616f76169868764c2c13a1e14dbf0b333b13"),
    ("annotation", "140b9c778e2bdccc225e15f24c7e11f470777ecc8fec03b656d6f1fe3ecb48d4"),
    // String-valued annotation element (@Since("1.13")) — the case that used
    // to error out of the parser (into :raw). Same pinned digest as Python:
    // this line IS the cross-engine parity assertion for the fix.
    ("strannotation", "0c7e6d76dd379b1a5a9b066729c009090977264e93d9130a6ca09c8b0b79c434"),
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

/// The fingerprint of an action whose input carries a content lens, as the
/// incremental/cache layers would compute it (mirrors `_dependent_fingerprint`
/// in both Python files; the spec JSON is the shape `brix.lowering` emits).
fn content_key_fingerprint(root: &Path, path: &str, content_key: &str) -> Result<String, String> {
    let spec = format!(
        r#"{{"kind": "process", "name": "x", "command_line": "use {{f}}",
            "inputs": {{"f": [{{"kind": "file", "path": "{path}", "content_key": "{content_key}"}}]}},
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
    let first = content_key_fingerprint(&dir, "x.jar", "jvm-abi").unwrap();
    assert_eq!(first.len(), 64); // did not error
    // The :raw fallback is a byte hash, so different bytes -> different key.
    std::fs::write(&target, "still not a jar, but different bytes").unwrap();
    assert_ne!(first, content_key_fingerprint(&dir, "x.jar", "jvm-abi").unwrap());
}

#[test]
fn unknown_identity_is_loud() {
    // In the full system an unknown name never reaches the core (the Python
    // skin's `supports_rust_engine` keeps custom-identity graphs on the
    // Python engine), but a wire spec naming one must still error, not guess.
    let dir = scratch("unknown-identity");
    std::fs::write(dir.join("x.jar"), "data").unwrap();
    let err = content_key_fingerprint(&dir, "x.jar", "no-such-identity").unwrap_err();
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
    assert_eq!(det.content_key_digest(&j, "jvm-abi").unwrap().unwrap(), "SENTINEL");
    // A real (uncached) computation stores its result for the next call.
    let real = TimeAndHashChangeDetector::with_metadata(std::sync::Arc::new(
        BuildMetadata::with_directory(&dir, "meta2"),
    ))
    .content_key_digest(&j, "jvm-abi")
    .unwrap()
    .unwrap();
    assert_eq!(real, jvmabi::digest_jar(&j).unwrap());
}

// -- kotlin-abi (test_kotlinabi.py) --------------------------------------------

// The golden contract, identical to EXPECTED in test_kotlinabi.py: both
// engines must produce these digests for the committed kotlinc fixtures.
const KOTLIN_EXPECTED: [(&str, &str); 14] = [
    ("plain_base", "976787a605e8b3562939ef662e1719b19b61d104e19043a7b1644f16e5e1ea04"),
    ("plain_impl", "976787a605e8b3562939ef662e1719b19b61d104e19043a7b1644f16e5e1ea04"),
    ("facade_base", "f4a70838ca81f2736ee9fde8eb862f0f17f7d71977ff27dd63569b3b38277592"),
    ("facade_impl", "f4a70838ca81f2736ee9fde8eb862f0f17f7d71977ff27dd63569b3b38277592"),
    ("inline_base", "19b02b5477b49a08c1608a3910965b5a483273e69aff1e41199ea6c4071fd12c"),
    ("inline_lineshift", "89c1d52a7630211c51456a383aa3d72faabce1a348aa2b8ebf0e3edfa659300a"),
    ("inline_bodychange", "386013fab5298b8670b7d87892cca0875e731c505b8a992bd01d9874bb507bc3"),
    ("accessor", "5c55f8043971f42dbb00933ba7a25ee8bce203e2be7398962508e19a6da1a53e"),
    ("nested_owner", "91fca98211b1c3c2c58bd76644c1ab581440e823aaed05f1f5d69d129d483e79"),
    ("nested_synthetic", "279c4a06cc844e7a2ce0231e155d80172dcdbd2369c36162ebfd1c6d96a6ca81"),
    ("nested_synthetic_impl", "79be08de901b7ddabc113d5e5dcc032508c7aecb44b5c312601c39d874095503"),
    ("multifile_facade", "900866a1ca5d1165e531762c30c54c6952be128ad3dfcd69dbdef4ec1f6a30c2"),
    ("multifile_part", "ee9fd3d9aaa8e43b700ae9ec36b38449d24121dd55e567515753adac17b5e769"),
    ("futuremv", "ddb25d3e8c228dff72e6e1868742cea1cab072470a57c2f2dbd4ab876a29141d"),
];

fn kcls(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/kotlinabi")
        .join(format!("{name}.class"));
    std::fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()))
}

fn kjar(dir: &Path, entries: &[(&str, &[u8])], name: &str) -> PathBuf {
    let path = dir.join(name);
    let file = std::fs::File::create(&path).unwrap();
    let mut zip = zip::ZipWriter::new(file);
    for (entry, data) in entries {
        zip.start_file(*entry, zip::write::SimpleFileOptions::default()).unwrap();
        zip.write_all(data).unwrap();
    }
    zip.finish().unwrap();
    path
}

#[test]
fn kotlin_pinned_class_digests() {
    for (name, digest) in KOTLIN_EXPECTED {
        assert_eq!(kotlinabi::digest_class(&kcls(name)).unwrap(), digest, "{name}");
    }
}

#[test]
fn kotlin_no_inline_implementation_change_hashes_equal() {
    assert_eq!(
        kotlinabi::digest_class(&kcls("plain_base")),
        kotlinabi::digest_class(&kcls("plain_impl"))
    );
    assert_eq!(
        kotlinabi::digest_class(&kcls("facade_base")),
        kotlinabi::digest_class(&kcls("facade_impl"))
    );
}

#[test]
fn kotlin_inline_body_and_line_shift_hash_differently() {
    let base = kotlinabi::digest_class(&kcls("inline_base")).unwrap();
    assert_ne!(base, kotlinabi::digest_class(&kcls("inline_bodychange")).unwrap());
    // Only line numbers changed — but inlined code carries them into
    // consumers (SMAP). jvm-abi cannot see it; kotlin-abi must.
    assert_ne!(base, kotlinabi::digest_class(&kcls("inline_lineshift")).unwrap());
    assert_eq!(
        jvmabi::digest_class(&kcls("inline_base")),
        jvmabi::digest_class(&kcls("inline_lineshift"))
    );
}

#[test]
fn kotlin_enclosure_rule_byte_counts_synthetics_under_inline() {
    // The synthetics differ only in the anonymous object's body (equal
    // jvm-abi — the guard); the enclosure rule alone makes the jars differ.
    assert_eq!(
        jvmabi::digest_class(&kcls("nested_synthetic")),
        jvmabi::digest_class(&kcls("nested_synthetic_impl"))
    );
    let dir = scratch("kabi-enclosure");
    let owner = kcls("nested_owner");
    let (s1, s2) = (kcls("nested_synthetic"), kcls("nested_synthetic_impl"));
    let a = kjar(&dir, &[
        ("abidemo/NestedKt.class", owner.as_slice()),
        ("abidemo/NestedKt$runner$1.class", s1.as_slice()),
    ], "a.jar");
    let b = kjar(&dir, &[
        ("abidemo/NestedKt.class", owner.as_slice()),
        ("abidemo/NestedKt$runner$1.class", s2.as_slice()),
    ], "b.jar");
    assert_ne!(kotlinabi::digest_jar(&a).unwrap(), kotlinabi::digest_jar(&b).unwrap());
    // Without the inline-flagged owner, nothing flags the $-ancestor: the
    // synthetics count by ABI and the body-only difference stays invisible.
    let c = kjar(&dir, &[("abidemo/NestedKt$runner$1.class", s1.as_slice())], "c.jar");
    let d = kjar(&dir, &[("abidemo/NestedKt$runner$1.class", s2.as_slice())], "d.jar");
    assert_eq!(kotlinabi::digest_jar(&c).unwrap(), kotlinabi::digest_jar(&d).unwrap());
}

#[test]
fn kotlin_module_entries_count_by_bytes_and_resources_are_ignored() {
    let dir = scratch("kabi-module");
    let plain_cls = kcls("plain_base");
    let base: &[(&str, &[u8])] = &[("abidemo/Greeter.class", plain_cls.as_slice())];
    let plain = kjar(&dir, base, "plain.jar");
    let m1 = kjar(&dir, &[
        ("abidemo/Greeter.class", plain_cls.as_slice()),
        ("META-INF/app.kotlin_module", b"v1".as_slice()),
    ], "m1.jar");
    let m2 = kjar(&dir, &[
        ("abidemo/Greeter.class", plain_cls.as_slice()),
        ("META-INF/app.kotlin_module", b"v2".as_slice()),
    ], "m2.jar");
    let noise = kjar(&dir, &[
        ("abidemo/Greeter.class", plain_cls.as_slice()),
        ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n".as_slice()),
        ("banner.txt", b"hello".as_slice()),
    ], "noise.jar");
    assert_ne!(kotlinabi::digest_jar(&plain).unwrap(), kotlinabi::digest_jar(&m1).unwrap());
    assert_ne!(kotlinabi::digest_jar(&m1).unwrap(), kotlinabi::digest_jar(&m2).unwrap());
    assert_eq!(kotlinabi::digest_jar(&plain).unwrap(), kotlinabi::digest_jar(&noise).unwrap());
}

#[test]
fn kotlin_malformed_class_raises() {
    assert!(kotlinabi::digest_class(b"\xde\xad\xbe\xef not a class file").is_err());
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
        content_key_fingerprint(&a, "api.d.ts", "ts-dts").unwrap(),
        content_key_fingerprint(&b, "api.d.ts", "ts-dts").unwrap()
    );
}

#[test]
fn dependent_fingerprint_reacts_to_api_change() {
    let (a, c) = (scratch("dts-fp-base"), scratch("dts-fp-change"));
    std::fs::write(a.join("api.d.ts"), BASE).unwrap();
    std::fs::write(c.join("api.d.ts"), API_CHANGE).unwrap();
    assert_ne!(
        content_key_fingerprint(&a, "api.d.ts", "ts-dts").unwrap(),
        content_key_fingerprint(&c, "api.d.ts", "ts-dts").unwrap()
    );
}

#[test]
fn undecodable_file_falls_back_to_raw() {
    // A non-text file under ts-dts identity must not crash — digest errors and
    // the engine byte-hashes under :raw, so different bytes still differ.
    let dir = scratch("dts-raw");
    let target = dir.join("api.d.ts");
    std::fs::write(&target, b"\xff\xfe\x00\x01 not utf-8").unwrap();
    let first = content_key_fingerprint(&dir, "api.d.ts", "ts-dts").unwrap();
    assert_eq!(first.len(), 64);
    std::fs::write(&target, b"\xff\xfe\x00\x02 different bytes").unwrap();
    assert_ne!(first, content_key_fingerprint(&dir, "api.d.ts", "ts-dts").unwrap());
}
