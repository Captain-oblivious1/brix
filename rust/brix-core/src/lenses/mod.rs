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
pub mod tsdts;

/// The `jvm-abi` lens: hash a jar/class's ABI, not its bytes, so a dependent
/// recompiles on API changes only.
pub struct JvmAbi;

/// The `ts-dts` lens: hash a `.d.ts` API surface, not its bytes, so a consumer
/// recompiles on an API-surface change only.
pub struct TsDts;

/// Singletons registered by name in [`crate::content_key`].
pub static JVM_ABI: JvmAbi = JvmAbi;
pub static TS_DTS: TsDts = TsDts;

impl ContentKey for JvmAbi {
    fn tag(&self) -> &'static str {
        "jvm-abi@1"
    }
    fn digest(&self, path: &Path) -> Result<String, String> {
        jvmabi::digest(path)
    }
}

impl ContentKey for TsDts {
    fn tag(&self) -> &'static str {
        "ts-dts@1"
    }
    fn digest(&self, path: &Path) -> Result<String, String> {
        tsdts::digest(path)
    }
}
