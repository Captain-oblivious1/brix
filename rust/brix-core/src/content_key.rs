//! Content-key lenses: how a datum's *comparison identity* is computed.
//!
//! A [`ContentKey`] lens turns a file's bytes into a digest of some semantic
//! identity — e.g. `jvm-abi` hashes a jar's API surface, not its bytes — so a
//! dependent's fingerprint changes only when the meaning it depends on
//! changes. Byte hashing is the *default* (an atom with no lens) and is
//! handled in the detector, not here: a lens exists precisely to compare by
//! *meaning* rather than bytes.
//!
//! The built-in lenses live outside brix-core's neutral fingerprint path, in
//! [`crate::lenses`] — the Rust mirror of "`brix.java` owns the ABI lens."
//! Dispatch is by a **registered name** (this module's registry), never a
//! fixed `enum` match, so a new lens is "register one more name" and the
//! deferred Python-callback escape hatch ([`CustomIdentities`]) stays cheap.
//!
//! Python remains the reference implementation for the built-ins; digests
//! must match it byte-for-byte (golden-pinned), and a `version` bump in one
//! implementation must be a version bump in both — the versioned tag
//! (`jvm-abi@1`) is part of the fingerprint payload, so bumping cleanly
//! invalidates everything the lens fingerprinted.

use std::collections::HashMap;
use std::path::Path;
use std::sync::LazyLock;

/// A named content lens. `name` is the un-versioned key it dispatches under
/// (e.g. `jvm-abi`); `tag` is the versioned fingerprint tag (`name@version`,
/// e.g. `jvm-abi@1`); `digest` turns the file's bytes into the lens's
/// comparison digest, erroring on a file it cannot interpret (the caller falls
/// back to byte hashing under a `:raw` tag).
pub trait ContentKey: Send + Sync {
    fn name(&self) -> &'static str;
    fn tag(&self) -> &'static str;
    fn digest(&self, path: &Path) -> Result<String, String>;
}

/// The built-in lens registry: name -> lens. Built by collecting the roster the
/// `lenses` module owns ([`crate::lenses::ALL`]) and keying each lens on its
/// own [`ContentKey::name`], so this neutral module names no language — adding
/// a built-in is adding it to `lenses::ALL`, nothing here. Mirrors the built-ins
/// registered in `src/brix/identity.py`.
static BUILTINS: LazyLock<HashMap<&'static str, &'static dyn ContentKey>> = LazyLock::new(|| {
    crate::lenses::ALL
        .iter()
        .map(|lens| (lens.name(), *lens))
        .collect()
});

/// The built-in lens for a name, or None for a name this core does not know
/// (the skin's engine owns those, via [`CustomIdentities`]).
pub fn builtin(name: &str) -> Option<&'static dyn ContentKey> {
    BUILTINS.get(name).copied()
}

/// The versioned fingerprint tag for a built-in lens, or None for an unknown
/// name.
pub fn tag(name: &str) -> Option<&'static str> {
    builtin(name).map(ContentKey::tag)
}

/// The digest of a file under a built-in lens. `Err` when the lens cannot
/// interpret the file — the caller falls back to byte hashing under a `:raw`
/// tag. Panics on unknown names; callers gate on [`tag`] / [`builtin`] first.
pub fn digest(name: &str, path: &Path) -> Result<String, String> {
    builtin(name)
        .unwrap_or_else(|| unreachable!("unknown content key {name}; gate on content_key::tag"))
        .digest(path)
}

/// Skin-registered content lenses, called across the boundary (the
/// `content_identity` extension trait). Built-ins win: the engine consults
/// this only for names [`tag`] does not know. This is the reserved
/// Python-callback escape hatch for custom, user-defined lenses.
pub trait CustomIdentities: Send + Sync {
    /// The versioned fingerprint tag (`name@version`) for a name this
    /// resolver knows, else None (then the name is a loud error).
    fn tag(&self, name: &str) -> Option<String>;

    /// The digest of a file. `Err` when the lens cannot interpret the file —
    /// the engine falls back to byte hashing under a `:raw` tag, exactly like
    /// a built-in (a skin plugin signals this by raising).
    fn digest(&self, name: &str, path: &Path) -> Result<String, String>;
}
