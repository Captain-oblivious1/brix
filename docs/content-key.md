# Comparison semantics and membership: `content_key` and glob exclusion

Status: **implemented, both engines (2026-07-10)** — glob exclusion and the
`content_key` relocation. Fingerprints are byte-identical across the
relocation (golden-verified); this note is the contract that was built. Companion to
`docs/boundary-schema.md` (the wire) and `docs/rust-port.md` (the engine split).

It settles two changes that came out of the leveldb dogfood, plus the principle
that ties them together:

1. **Glob exclusion** — `data.glob("{name}.cc") - "*_test.cc"`.
2. **Retiring `identity="jvm-abi"`** in favour of a `content_key` comparison that
   lives on the data objects and is implemented as a name-dispatched Rust trait,
   with `brix.java` owning the ABI lens.

Both fall out of one rule, so the rule comes first.

## The load-bearing principle: comparison *semantics* vs comparison *state*

The graph (data expressions + generic actions) is **clean-build truth**: it says
what data exists and what to run, and it must be correct with no build history at
all. The layers (`Incremental`, `Cache`, the worker daemon) own everything about
*reuse across builds*. The question that kept relocating the ABI lens was "which
side of that line is it on," and the answer is that the line has two sides that
look alike:

- **Comparison *state*** — the actual digests, cache entries, last-build
  fingerprints. This is layer-owned and lives in metadata. It must never appear
  in the graph. (No change; stated for completeness.)
- **Comparison *semantics*** — a *static declaration* of **how** a datum is
  compared. This is already legitimate graph truth, and brix already encodes it:
  `ToolData`, `FileData`, and `ValueData` are three atom types that each compare
  themselves differently (tool identity, content bytes, literal text). That
  distinction only ever *matters* to fingerprinting, yet nobody calls it a leak,
  because the strategy is intrinsic to *what the datum is*.

So the rule is **"no comparison *state* in the graph,"** not "no fingerprinting
concepts in the graph." Comparison *semantics* — including a content lens — are
allowed in the graph, in the same category as the `Tool`/`File`/`Value`
distinction. That is why `identity=` felt wrong: not because it was in the graph,
but because it was in the *wrong locus* and spelled as an opaque string.

## Membership is clean-build truth: `exclude` via the `-` operator

`exclude` answers "which files are in this collection" — pure clean-build truth
(it changes what gets built from scratch, no layer involved). So it belongs on
the data model, and the natural spelling reuses brix's existing operator idiom:
`+` already unions data expressions, and set-difference is its partner.

```python
data.glob("{name}.cc") - "*_test.cc"
data.glob("{name}.cc") - ["*_test.cc", "*_benchmark.cc", "util/env_windows.cc"]
```

Decisions:

- **Right-hand side is fnmatch**, matched against the repo-relative POSIX path
  (`*`, `?`, `[...]`). fnmatch, not brix-pattern syntax, because the whole point
  is `*`-style wildcards, which brix patterns (`{name}`, `{name:*}`) can't spell.
  The include side stays brix-pattern syntax; the two mini-languages are
  deliberately different (include binds, exclude filters).
- **`__sub__` returns an immutable copy** of the `GlobData` with the pattern(s)
  appended to an internal `exclude` list. It does **not** produce a
  `DataDifference` wrapper (mirroring `DataUnion`), because `ExpandAll` and
  `cpp.CompileSourceAction` drive off a single pattern with a binding
  (`source_files.pattern.binding_names`); a wrapper would break that. This is a
  deliberate asymmetry with `+`: union *joins two exprs*, exclude *filters one*.
- **`find()` honours excludes**; `substitute()` ignores them (an exclude narrows
  membership, it is meaningless on an output template).
- **The planner ignores excludes for edge inference** (overlap is computed on the
  base pattern). Excludes can only *shrink* a set, so ignoring them keeps overlap
  conservative — we never drop a real dependency edge. The cost is that an exclude
  cannot be used to *break* a false cycle (that would need proving the excluded
  set covers the overlap region); documented, not supported in v1.
- **Wire**: the `glob` atom gains `"exclude": [str]?` (absent = none). Additive,
  stays schema version 1 (see `docs/boundary-schema.md`).
- **Fingerprint**: the exclude list is part of the glob's identity — changing it
  changes the build — so it joins the material wherever the glob's template does.

This lets `examples/leveldb` drop its `setup.sh` staging and glob the library
sources in place, which becomes the real-tree proof.

## Comparison is `content_key`, implemented as a name-dispatched Rust trait

Replace the `identity="jvm-abi"` string kwarg with a polymorphic comparison that
lives on the data objects. Across builds we never hold the old file — only the
digest we persisted — so the method is not `__eq__(other)`; it is a
**`content_key`** that returns "the bytes that must not change," and equality
falls out of comparing stored keys:

```
ValueData  -> the text
ToolData   -> tool identity (version / resolved path)
FileData   -> hash of contents  (mtime+size as a fast-path to skip rehashing)
AbiFile    -> the JVM ABI digest      # provided by brix.java, not the core
```

### The logic is Rust; the Python surface is declaration only

brix has a Python reference engine and a Rust core, and the Rust core fingerprints
natively — it cannot call a Python `content_key` per file. Rather than mirror the
lens logic in Python and bridge it, the **comparison logic is a Rust trait**, and
both engines use it. This is the same direction as phase 4c (delete the Python
engine internals): the *computation* is Rust, the *Python side is pure
declaration*.

```
brix-core:                  trait ContentKey { fn key(&self, path, bytes) -> Vec<u8> }
                            + the default impl (byte hash)      // language-neutral
brix-core::lenses (or a
  separate crate), outside
  the neutral fingerprint
  path:                     impl ContentKey for JvmAbi { ... }  // registered "jvm-abi"
                            impl ContentKey for TsDts  { ... }  // registered "ts-dts"
```

- **Byte-hashing becomes just another `ContentKey` impl**, so dispatch is
  uniform: the atom's strategy name selects an impl; absent name = the byte
  default. This mostly *formalizes* what `detector.rs`/`jvmabi.rs` already do.
- **Dispatch is by a registered name, never a fixed `enum` match.** This is the
  one choice that keeps the deferred escape hatch (below) cheap: adding a
  Python-backed lens later is "register one more name," not a re-plumb of the
  fingerprint path.
- **The built-in lenses live *outside* brix-core's neutral path** — a `lenses`
  module or a separate crate — the Rust mirror of "`brix.java` owns `AbiFile`."
  Today `jvmabi.rs` sits *inside* brix-core, so this is a move, not a rename. How
  far to push it (submodule vs. separate crate) is a knob; the invariant is that
  the fingerprint path dispatches through the trait and does not name Java.

### Python authoring surface

The atom/helper carries a **strategy name** that the author never types:

```python
inputs={"classpath": java.abi(data.glob("classes/{rel}.class"))}   # -> name "jvm-abi" on the wire
inputs={"classpath": data.glob("classes/{rel}.class")}             # -> byte default
```

`java.abi(...)` must apply across a **glob or a union** (a classpath is many
jars), so under the hood it is "a lens applied across the atoms of an
expression": `java.abi(expr)` walks `expr.atoms()` and rewrites each plain
`FileData`/`GlobData` to its lens counterpart (`brix.java.AbiFile`/`AbiGlob`),
passing tools, values, and already-lensed atoms through untouched.

**Settled encoding (was deferred):** the lens is a **typed atom subclass**, not
a strategy field. `AbiFile(FileData)`/`AbiGlob(GlobData)` in `brix.java` set a
class attribute `content_key = "jvm-abi"`; the base `DataAtom.content_key` is
`None` (byte hashing). The comparison lens is thus *intrinsic to the atom's
type*, in the same category as the `File`/`Tool`/`Value` distinction — exactly
the "comparison semantics, not state" framing above. `substitute`/`__sub__`
reconstruct via `type(self)`, so a lens survives narrowing and expansion. On the
wire the atom carries `"content_key": str?` (the old `identity` field, renamed);
`None`/absent is the byte default. The two-consumer case is unchanged in spirit:
`java.abi(jars)` on the compile classpath, plain `jars` on the run classpath —
same files, two declarations, no magic string, no hand-duplicated copies.

### Custom, user-defined lenses: (b) now, (a) later

With `content_key` in Rust, a brand-new lens means a Rust impl + rebuild. Two
options were on the table: (a) keep the Python-callback escape hatch
(`CustomIdentities`) so pure-Python users can define exotic lenses without Rust;
(b) drop it — lenses are a Rust-native registered set.

**Decision: (b) now, (a) kept in reserve.** The asymmetry is the reason: *adding*
the callback later is purely additive (a new opt-in path that doesn't disturb the
built-in trait dispatch); *removing* it later would be breaking. The real lens set
is small and known (jvm-abi, ts-dts, maybe a few more for the monorepo), so we
start closed and open a hole only when a concrete case needs to fit through it —
and it'll fit better for being built against a real need. The name-registry
dispatch above is what preserves the option at zero ongoing cost.

## Invariant and how we verified

This whole change is **relocation, not new behavior**: for an equivalent graph the
fingerprint bytes are **identical** to before. The lens name is folded into the
fingerprint material at the same position it occupied; only its *source* moved
(atom kwarg → typed atom subclass → Rust `ContentKey` trait dispatch).
Verification, as built: the golden fingerprints (`rust/brix-core/tests/golden/
fingerprints.json`) were regenerated from the new surface — **every `fingerprint`
digest is byte-for-byte unchanged; only the `spec` strings changed** (`identity` →
`content_key`), which is the invariant made mechanical. The content-identity tests
(`tests/test_content_identity.py`, `tests/test_ts_dts_identity.py`, the Rust
`identity` test) were rewritten to the typed-atom surface with the **same
assertions** (an ABI-only change to a dependency replays the compile; a byte change
re-runs), plus the unknown-lens-is-loud case (now a subclass naming an
unregistered lens). Full suite green under both engines.

## Boundary-schema deltas (all additive, still version 1)

- `glob` atom gains `"exclude": [str]?`.
- The atom's `identity` field is renamed to `content_key` (same position in the
  wire shape, byte-identical fingerprint material). The author never sets it: a
  skin's typed atom (`brix.java.AbiFile`) carries it; `None`/absent is byte
  hashing. Pinned in `docs/boundary-schema.md`.
- No `"version"` bump: exclude is additive; the lens relocation preserves the same
  digests, so caches do not split.

## Rust structure (as built)

- `brix-core::content_key` — the `ContentKey` trait (`tag`, `digest`), a
  name-keyed registry of built-in lenses, and the `CustomIdentities` skin-callback
  escape hatch (reserved, unused by default).
- `brix-core::lenses` — the built-in `impl ContentKey`s (`JvmAbi`, `TsDts`) and
  the `jvmabi`/`tsdts` digest code, **outside** the neutral fingerprint path. The
  detector/`content` dispatch through the trait and name no language. Byte hashing
  stays a distinct 3-element material shape (`["file", path, hash]`) vs a lens's
  4-element `["file", path, tag, digest]` — the shape difference is load-bearing
  for the byte-identity invariant, so byte hashing is *not* folded into the trait.

## Sequencing and model split

1. **`-`/exclude first** — small, mechanical, fully specified above. Cheap-model
   (Fable) work against this note. Unblocks the leveldb in-place retrofit.
2. **`content_key` trait refactor second** — crosses the Python/Rust boundary on
   the fingerprint path under the byte-identical invariant. Build on Opus; Fable
   review pass (its reserved role). Touches `docs/boundary-schema.md`.
