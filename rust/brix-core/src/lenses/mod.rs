//! Built-in content-key lenses, living *outside* brix-core's neutral
//! fingerprint path. The fingerprint path (`detector`, `content`) dispatches
//! through the [`crate::content_key::ContentKey`] trait and this module's
//! registry entries; it never names a language. Each lens is the Rust mirror
//! of a lens a skin would own (`brix.java` owns the ABI lens).
//!
//! Python (`src/brix/jvmabi.py`, `src/brix/tsdts.py`) remains the reference
//! implementation; the digests are golden-pinned so the two engines agree
//! byte-for-byte.

use std::path::Path;

use crate::content_key::ContentKey;

pub mod jvmabi;
pub mod kotlinabi;
pub mod tsdts;

/// The `jvm-abi` lens: hash a jar/class's ABI, not its bytes, so a dependent
/// recompiles on API changes only.
pub struct JvmAbi;

/// The `kotlin-abi` lens: jvm-abi semantics, except classes whose Kotlin
/// metadata declares inline members (whose implementation compiles into
/// consumers) count by whole-class bytes.
pub struct KotlinAbi;

/// The `ts-dts` lens: hash a `.d.ts` API surface, not its bytes, so a consumer
/// recompiles on an API-surface change only.
pub struct TsDts;

/// Singletons rostered in [`ALL`] and keyed by name in [`crate::content_key`].
pub static JVM_ABI: JvmAbi = JvmAbi;
pub static KOTLIN_ABI: KotlinAbi = KotlinAbi;
pub static TS_DTS: TsDts = TsDts;

/// The built-in lens roster. `content_key`'s registry is built by iterating
/// this and keying each lens on its own [`ContentKey::name`], so the neutral
/// core names no language: adding a built-in is defining it here and listing it
/// here.
pub static ALL: &[&'static dyn ContentKey] = &[&JVM_ABI, &KOTLIN_ABI, &TS_DTS];

impl ContentKey for JvmAbi {
    fn name(&self) -> &'static str {
        "jvm-abi"
    }
    fn tag(&self) -> &'static str {
        "jvm-abi@1"
    }
    fn digest(&self, path: &Path) -> Result<String, String> {
        jvmabi::digest(path)
    }
}

impl ContentKey for KotlinAbi {
    fn name(&self) -> &'static str {
        "kotlin-abi"
    }
    fn tag(&self) -> &'static str {
        "kotlin-abi@1"
    }
    fn digest(&self, path: &Path) -> Result<String, String> {
        kotlinabi::digest(path)
    }
}

impl ContentKey for TsDts {
    fn name(&self) -> &'static str {
        "ts-dts"
    }
    fn tag(&self) -> &'static str {
        "ts-dts@1"
    }
    fn digest(&self, path: &Path) -> Result<String, String> {
        tsdts::digest(path)
    }
}
