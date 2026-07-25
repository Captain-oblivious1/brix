#!/usr/bin/env python3
"""Cross-language FFI: a Rust library linked into a C program, both built by brix.

    python3 examples/rust_ffi/brix_build.py build
    ./examples/rust_ffi/build/app        # -> Rust says the answer is 42

`brix.rust` wraps **cargo** to build the crate as a C-ABI `staticlib`
(`crate/target/release/libgreet.a`); `brix.cpp` compiles `src/main.c` and links
it against that archive. No cross-package edge is declared: brix derives the
*cargo -> link* edge because the link's inputs include the Rust artifact, so
editing `crate/src/lib.rs` reruns cargo **and** relinks the C app. This is the
orchestration model — cargo owns the crate build; brix wires it to the rest of a
polyglot graph by its output.

Plain incremental stack (no atomic staging): cargo writes into its own
`target/`, which an atomic overlay can't redirect (like tsc's node_modules).
Needs `cargo` and a C compiler.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from brix import Graph, cli, cpp, rust  # noqa: E402

ROOT = Path(__file__).resolve().parent
RUST_LIB = "crate/target/release/libgreet.a"


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        # cargo builds the staticlib; its .a is this action's tracked output.
        rust.cargo("crate/Cargo.toml", root=ROOT),
        cpp.CompileSourceAction(
            source_file=d.glob("src/{name}.c"),
            object_file=d.glob("build/obj/{name}.o"),
            # Compile as C (not the default c++): the Rust symbol is exported
            # unmangled via the C ABI, so the caller must not C++-mangle it.
            compiler=d.tool("cc"),
        ),
        cpp.LinkApplicationAction(
            # The C objects plus the Rust archive: declaring the .a as a link
            # input is what makes brix derive the cargo -> link edge. Rust's std
            # is bundled into the staticlib, so it needs the usual system libs.
            object_files=d.glob("build/obj/{name}.o") + d.file(RUST_LIB),
            output_file=d.file("build/app"),
            linker=d.tool("cc"),
            link_options=["-lpthread", "-ldl", "-lm"],
        ),
    )
    return g


if __name__ == "__main__":
    cli.main(create_graph, root=ROOT)
