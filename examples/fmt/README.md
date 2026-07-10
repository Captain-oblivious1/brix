# Building an fmt app with brix (C++)

The C++ counterpart to `examples/junit5`: validate `brix.cpp` — per-source
parallel compilation and automatic `#include` **header-dependency tracking**
(via compiler depfiles) — against a famous real library's headers.

## Scope

Compile fmt's classic library sources (`src/format.cc`, `src/os.cc`) plus a
small `main.cc` that uses `fmt::format`/`fmt::print`, and link `build/hello`.
fmt's third source, `src/fmt.cc`, is the C++20 *module* interface (needs
`-fmodules`) and is excluded. No external deps — fmt is self-contained.

## Setup

    git clone --filter=blob:none https://github.com/fmtlib/fmt.git \
        ~/Projects/brix_test/fmt
    git -C ~/Projects/brix_test/fmt checkout 11.1.4
    examples/fmt/setup.sh          # assemble build-src/ (symlinks + main.cc)

`setup.sh` gathers the chosen sources into `build-src/` (symlinks to fmt's,
a copy of ours) — `CompileSourceAction` takes one glob, so this is how a
target lists exactly what it compiles, like CMake `target_sources`. No fmt
sources are modified. Override the checkout with `BRIX_FMT_ROOT`.

## Run

    python examples/fmt/brix.py build      # compile + link -> build/hello
    ~/Projects/brix_test/fmt/build/hello    # "brix+fmt says the answer is 42"
    python examples/fmt/brix.py status

## What it demonstrates

- **Per-source parallel compilation** (`CompileSourceAction` → one compile
  per translation unit), then **linking** (`LinkApplicationAction`).
- **Header-dep tracking**, the real point: `-MMD -MF` writes each object's
  `#include` prerequisites; brix scans the depfile and folds those headers
  into the fingerprint, with nothing declared. Verified on fmt's headers:
  editing `include/fmt/os.h` recompiles only `os` (the sole TU that includes
  it); editing the widely-included `include/fmt/base.h` recompiles all three.
  And a comment-only header edit that yields a byte-identical object correctly
  lets the **link replay** — brix recompiles the TU (it can't know the change
  was cosmetic) but sees the output is unchanged downstream.
