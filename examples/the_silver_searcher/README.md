# Building the_silver_searcher (ag) with brix

The dogfood for **C++/C third-party dependency resolution** (`cpp.pkg_config`).
[the_silver_searcher](https://github.com/ggreer/the_silver_searcher) (`ag`) is a
popular code-search tool whose real dependencies — **PCRE, zlib, lzma** — are
system libraries discoverable via pkg-config. brix drives pkg-config, consumes
the flags, and owns the compile + link.

## What it demonstrates

- **`cpp.pkg_config(["libpcre", "zlib", "liblzma"])`** → a `DepSet` whose link
  flags (`-lpcre -lz -llzma`) feed the link. brix has no dependency resolver by
  design; it drives one and consumes its output (`docs/dependencies.md`).
- **A real multi-TU C build**: ag's 12 sources compiled to objects and linked
  into `ag`, with `-MMD` depfiles so a header edit recompiles only what includes
  it.
- **Glob exclusion**: `data.glob("src/{name}.c") - "*print_w32.c"` drops the
  Windows-only translation unit.
- **Consuming a configure step's output**: autotools generates `src/config.h`;
  brix builds from there (the same shape as consuming Maven's classpath — brix
  owns the graph, not the ecosystem's resolver/configure).

## Setup

    git clone --depth 1 https://github.com/ggreer/the_silver_searcher.git \
        ~/Projects/brix_test/the_silver_searcher
    cd ~/Projects/brix_test/the_silver_searcher && ./autogen.sh && ./configure

`./configure` generates `src/config.h` and needs PCRE / zlib / lzma installed
(it discovers them via pkg-config, the same libraries brix then resolves).
Override the checkout location with `BRIX_AG_ROOT`.

## Run

    python examples/the_silver_searcher/brix_build.py build     # -> build/ag
    python examples/the_silver_searcher/brix_build.py status

    ~/Projects/brix_test/the_silver_searcher/build/ag --version   # ag version 2.2.0
    ~/Projects/brix_test/the_silver_searcher/build/ag pcre_compile src/  # a real search

The build uses the brix in this repo (`../../src` on `sys.path`) and the Rust
engine by default.

## Note

System libraries arrive from pkg-config as `-l` flags, not file paths, so their
*contents* aren't fingerprinted — a system-lib upgrade via the package manager
is outside the project. A future `cpp.conan` / `cpp.vcpkg` adapter yielding
concrete `.a`/`.so` paths would populate the `DepSet`'s tracked `artifacts`.
