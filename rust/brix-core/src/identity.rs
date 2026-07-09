//! Built-in content-identity plugins: fingerprint a file by *meaning*, not
//! bytes. Mirrors `src/brix/identity.py`'s registry for the plugins that ship
//! with brix — `jvm-abi` and `ts-dts` — so every language skin gets them from
//! the core. Custom skin-registered plugins don't exist here: a graph naming
//! an identity this registry doesn't know stays on the skin's own engine
//! (`lowering.supports_rust_engine`).
//!
//! Python remains the reference implementation; digests must match it
//! byte-for-byte (golden-pinned), and a `version` bump in one implementation
//! must be a version bump in both — the versioned tag (`jvm-abi@1`) is part
//! of the fingerprint payload, so bumping cleanly invalidates everything the
//! plugin fingerprinted.

use std::path::Path;

/// The versioned fingerprint tag for a built-in identity, or None for a name
/// this core does not know (the skin's engine owns those).
pub fn tag(name: &str) -> Option<&'static str> {
    match name {
        "jvm-abi" => Some("jvm-abi@1"),
        "ts-dts" => Some("ts-dts@1"),
        _ => None,
    }
}

/// The identity digest of a file. `Err` when the plugin cannot interpret the
/// file — the engine falls back to byte hashing under a `:raw` tag.
/// Panics on unknown names; callers gate on [`tag`] first.
pub fn digest(name: &str, path: &Path) -> Result<String, String> {
    match name {
        "jvm-abi" => crate::jvmabi::digest(path),
        "ts-dts" => crate::tsdts::digest(path),
        _ => unreachable!("unknown identity {name}; gate on identity::tag"),
    }
}
