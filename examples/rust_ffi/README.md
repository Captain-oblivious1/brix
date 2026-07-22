# Rust ↔ C FFI

A Rust library linked into a C program, both built by brix — the cross-language
edge in miniature.

```
crate/   Rust   a C-ABI staticlib (crate/src/lib.rs -> libgreet.a)   rust.cargo
src/     C      main.c calls answer(), links the archive             cpp.*
```

```
python3 examples/rust_ffi/brix_build.py build
./examples/rust_ffi/build/app        # -> Rust says the answer is 42
```

## What it shows

`brix.rust` wraps **cargo** to build the crate as a `staticlib`; `brix.cpp`
compiles `main.c` and links it against the archive. No cross-package edge is
declared — brix derives the **cargo → link** edge because the link's inputs
include the Rust artifact. So editing `crate/src/lib.rs` reruns cargo *and*
relinks the C app, while the unchanged C compile **replays**:

```
# edit answer() to return 7, rebuild:
ok:  cargo build -> crate/target/release/libgreet.a
ok:  compile main (replayed)      # C source unchanged
ok:  link build/app               # relinked against the new archive
./build/app                       # -> Rust says the answer is 7
```

This is the **orchestration** model: cargo owns the crate build; brix wires it
into a polyglot graph by its output, and applies its own incrementality at the
crate/link granularity. Two notes carried in `brix.rust`: `main.c` is compiled
as **C** (`compiler=cc`), since Rust exports the symbol unmangled via the C ABI;
and the build uses the **plain incremental stack**, because cargo writes its own
`target/` that atomic staging can't redirect. Needs `cargo` and a C compiler.
