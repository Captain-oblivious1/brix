# leveldb — a C++ static library, built by brix

A real-project dogfood: build [google/leveldb](https://github.com/google/leveldb)'s
library into `libleveldb.a` with `brix.cpp`, then link and run a smoke program
against it. This is the larger C++ target the `fmt` dogfood pointed to — it
proves `cpp.ArchiveAction`, parallel compilation at scale (38 translation
units), and automatic `#include` header-dep tracking on a real source tree.

## Setup

```sh
git clone --depth 1 https://github.com/google/leveldb.git ~/Projects/brix_test/leveldb
examples/leveldb/setup.sh                    # generate port_config.h + smoke/
```

The library sources are globbed **straight from leveldb's tree** — no staging.
Each source dir interleaves library code with `*_test.cc`, benchmarks, and the
Windows port, so `brix.py` narrows the globs with the `-` operator:

```python
data.glob("db/{name}.cc") - ["*_test.cc", "db/leveldbutil.cc"]
data.glob("util/{name}.cc") - ["*_test.cc", "util/env_windows.cc", ...]
```

The resulting source set is exactly the `add_library()` list in upstream
`CMakeLists.txt`. `setup.sh` only writes the two files that must exist inside
the workspace: `gen/config/port/port_config.h` (the header CMake generates via
`configure_file`, here for POSIX Linux with the optional compression/CRC
libraries off) and the smoke program in `smoke/`.

Override the checkout location with `BRIX_LEVELDB_ROOT`.

## Build & run

```sh
python examples/leveldb/brix.py build        # compile 38 TUs -> ar -> libleveldb.a, link smoke
python examples/leveldb/brix.py run smoke     # (re)link + run the smoke program
python examples/leveldb/brix.py status        # what would rebuild, and why
```

`build` alone builds everything (the archive and the linked smoke binary).
`run smoke` is a build-then-act subcommand: it builds `build/smoke_test`, then
executes it (open a fresh DB, batch-put three keys, get one back, iterate,
assert the round-trip). The archive lands at `build/lib/libleveldb.a`; alias
`lib` builds just that.

## What it demonstrates

- **Glob exclusion on a real tree** — per-dir `data.glob(...) - [...]`
  narrows leveldb's mixed source dirs down to the 38 library TUs in place;
  editing a real file (source or header) re-stales its compiles directly.
- **`cpp.ArchiveAction`** bundling 38 objects into a static library with
  `ar Dcrs` (deterministic, byte-reproducible), then **`cpp.LinkApplicationAction`**
  linking a program against the `.a` (`-lpthread` for leveldb's POSIX env).
- **Parallel compilation at scale** — all 38 library TUs compile concurrently,
  capped by brix's worker limit.
- **Selective, depfile-driven recompiles on a real tree.** Edit a header and
  only the translation units that transitively include it recompile — learned
  from the compiler's `-MMD` depfile, nothing declared. For example, editing
  `db/skiplist.h` recompiles exactly the 5 TUs that reach it through
  `db/memtable.h` (memtable, db_impl, repair, version_set, write_batch); the
  other 33 are untouched. A comment-only edit yields byte-identical objects, so
  `ar` and the link correctly *replay* rather than redo work.

Validated against leveldb 1.23 (`7ee830d`). Compression libraries
(snappy/zstd/crc32c) are off; this builds the pure library and its smoke test.
