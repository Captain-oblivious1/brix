#!/usr/bin/env bash
# Assemble the source set brix compiles, in the fmt checkout. fmt has three
# src/*.cc files; fmt.cc is the C++20 *module* interface (needs -fmodules),
# so we compile only the classic library sources (format.cc, os.cc) plus our
# main.cc. CompileSourceAction takes one glob, so — exactly like a CMake
# target listing its sources — we gather the chosen ones into build-src/
# (symlinks to fmt's, a real copy of ours). No fmt sources are modified.
set -euo pipefail

ROOT="${BRIX_FMT_ROOT:-$HOME/Projects/brix_test/fmt}"
SRC="$ROOT/build-src"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$SRC"
ln -sf ../src/format.cc "$SRC/format.cc"
ln -sf ../src/os.cc "$SRC/os.cc"
cp "$HERE/main.cc" "$SRC/main.cc"
echo "assembled $SRC: $(ls "$SRC")"
