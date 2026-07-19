//! A Rust library exposed to C via the C ABI.

/// Returns the answer. Consumed by ../src/main.c across the language boundary.
#[no_mangle]
pub extern "C" fn answer() -> i32 {
    42
}
