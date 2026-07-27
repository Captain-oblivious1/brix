# Task for ChatGPT: two more TypeScript skins (`rust` and `kotlin` core)

You are adding two language "skins" to brix's **TypeScript** authoring API, by
faithfully mirroring the existing Python ones. This is a mechanical,
pattern-following task with an **objective pass/fail check** (an oracle). Four
skins are already done — copy their shape exactly.

---

## Absolute rules (read these first)

1. **Only create/edit files under `ts/`.** You will *read* Python files under
   `src/brix/` as the reference to mirror, but you must **not modify any Python
   file, any Rust file (`rust/`), or `docs/boundary-schema.md`.** If you feel you
   need to, stop — you've misunderstood the task.
2. **Never touch fingerprint / engine / soundness code.** You are not changing
   how brix decides what to rebuild. Skins only *build a JSON description* of the
   build and hand it to the engine. That's all.
3. **The oracle is truth. If it fails, fix the TypeScript to match the Python —
   never change the Python to match your TypeScript.** The whole point is that
   the TS skin lowers to a JSON spec byte-for-byte equal to the Python skin's.
4. **Do not invent API.** Mirror the Python skin's public surface. Where a piece
   depends on something not yet in the TS core (a "worker", a `DepSet`
   resolver), **defer it** exactly as the existing TS skins defer such things
   (see `ts/src/java.ts` and `ts/src/cpp.ts` for how — they throw a clear "not
   available in the TypeScript frontend yet" error).
5. **Ask nothing, guess nothing about design.** If the oracle passes and the
   rules above hold, you're done. Don't add features, don't refactor shared code.
6. **You should NOT need to modify the TS *core* files** — `ts/src/actions.ts`,
   `data.ts`, `lowering.ts`, `engine.ts`, `builders.ts`, `cli.ts`, `env.ts`.
   Everything these two skins need already exists there (the content-lens
   pattern, the `SYSTEM`/`env` sentinels, `ExpandAll`, etc.). Your work is: two
   **new** files (`ts/src/rust.ts`, `ts/src/kotlin.ts`), the matching oracle
   files, and adding `export * as ...` lines to `ts/src/index.ts`. If you think
   you need to change a core file, you've misunderstood the task — stop.

---

## What a "skin" is (context)

brix builds are described with a few **generic** building blocks (in
`ts/src/actions.ts` and `ts/src/data.ts`): `ExternalProcessAction` ("run this
command"), `ExpandAll` ("do that once per file in a glob"), and data atoms
(`FileData`, `GlobData`, `ToolData`, `ValueData`, and lens subclasses).

A **skin** is a small convenience class for one language (e.g. `JarAction` for
Java) that has a `lower()` method returning a list of those generic actions. It
adds **no new behaviour** — it just saves the user from writing the raw command.
`Graph.add(...)` calls `.lower()` automatically.

The TypeScript API is **the Python API, transliterated**, with exactly these
deviations:
- `snake_case` → `camelCase` (`command_line` → `commandLine`, `element_name` →
  `elementName`). The **wire JSON keys stay snake_case** — see how the existing
  skins' lowering emits them.
- Constructors take **one options object** (TS has no keyword arguments):
  `new JarAction({ sourceFiles: ..., outputFile: ... })`.
- TS has no operator overloading, so Python's `a + b` (union) is `a.plus(b)` and
  a glob's `g - "*_test"` is `g.excluding("*_test")`. (You probably won't need
  these for this task.)

---

## The template — study these first

Four skins are already implemented and oracle-proven. **Read all of them and
their oracles before writing anything** — you are producing more of exactly this:

| TS skin | mirrors | oracle (Python dump) | oracle test |
|---|---|---|---|
| `ts/src/python.ts` | `src/brix/python.py` | `ts/oracle/py_python_skin.py` | `ts/src/test/skin_python.test.ts` |
| `ts/src/ts.ts` | `src/brix/ts.py` | `ts/oracle/py_ts_skin.py` | `ts/src/test/skin_ts.test.ts` |
| `ts/src/java.ts` | `src/brix/java.py` | `ts/oracle/py_java_skin.py` | `ts/src/test/skin_java.test.ts` |
| `ts/src/cpp.ts` | `src/brix/cpp.py` | `ts/oracle/py_cpp_skin.py` | `ts/src/test/skin_cpp.test.ts` |

Note especially:
- **The lens pattern** in `ts/src/java.ts` (`AbiFile`/`AbiGlob`/`abi`) and
  `ts/src/ts.ts` (`DtsFile`/`DtsGlob`/`dts`) — a lens subclass just sets
  `this.contentKey = "..."`. You'll copy this shape for kotlin.
- **The env-sentinel path**: `SYSTEM` is imported from `ts/src/env.ts`; the core
  already handles it. Skins that set `env: { VAR: SYSTEM }` just work.
- **How deferral is done**: `ts/src/java.ts`'s `JarAction` throws if
  `classpath`/`javacWorker` are passed. `ts/src/cpp.ts` throws for `deps`.
- **How the oracle works**: the `.test.ts` builds a graph in TS and lowers it
  (`graphToSpec`), spawns `python3` on the matching `oracle/py_*_skin.py` (which
  builds the *same* graph in Python and prints its lowered JSON), and asserts
  `assert.deepStrictEqual(tsSpec, pySpec)`. The Python dump scripts add `src` to
  `sys.path` themselves, so you only need `python3` on PATH.

After writing each skin, **export it** from `ts/src/index.ts` (see the existing
`export * as python from "./python";` lines).

---

## Deliverable 1: the `rust` skin

Mirror **`src/brix/rust.py`**. Create `ts/src/rust.ts` with:

- **`CargoBuildAction`** — mirror it fully. It has one `lower()` returning a
  single `ExternalProcessAction` that runs `cargo build`. It uses the
  env-sentinel path: `env: { ...CARGO_ENV, ...userEnv }` where `CARGO_ENV` is the
  `{ RUSTFLAGS: SYSTEM, RUSTUP_TOOLCHAIN: SYSTEM, CARGO_HOME: SYSTEM,
  CARGO_NET_OFFLINE: SYSTEM }` map, plus an `envUsed` option. Study how the
  Python `env={**_CARGO_ENV, **(self.env or {})}` merge works and reproduce it
  (user's entry for a name wins). Constructor options (camelCased): `manifest`,
  `artifact` (string or `FileData`), `sources`, `cargo`, `release` (default
  true), `offline` (default false), `options`, `env`, `envUsed`.

- **DEFER `cargo()`** (the manifest-reading helper). It parses `Cargo.toml`
  (TOML) and walks the filesystem, which the TS core can't do cleanly yet.
  Do **not** implement it. (Optionally add a `// TODO` note; do not stub it in a
  way that changes behaviour.)

Then: export from `index.ts`, and write `ts/oracle/py_rust_skin.py` +
`ts/src/test/skin_rust.test.ts` (copy the shape of the cpp ones). The reference
graph in the oracle should exercise `CargoBuildAction` with `release`, `offline`,
`options`, an explicit `env` entry, and an `envUsed` entry.

---

## Deliverable 2: the `kotlin` skin (core only)

Mirror **`src/brix/kotlin.py`**. Create `ts/src/kotlin.ts` with:

- **`KotlinAbiFile` / `KotlinAbiGlob` / `abi`** — the `kotlin-abi` content lens.
  Identical shape to java.ts's `AbiFile`/`AbiGlob`/`abi`, but with
  `contentKey = "kotlin-abi"`. Straight copy-with-rename.

- **`JarAction`** — mirror **only the spawn path** (`_compile_via_spawn` in the
  Python). It compiles with `kotlinc` (spawned) and packages with `jar`.
  **DEFER the worker path** (`_compile_via_worker`) and the `classpath` option:
  if `kotlincWorker` or `classpath` is passed to the constructor, **throw** a
  clear "not available in the TypeScript frontend yet" error — copy exactly how
  `ts/src/java.ts`'s `JarAction` throws for `classpath`/`javacWorker`. (Those
  need brix's shipped `KotlincWorker.java` helper and the `DepSet` resolver,
  which aren't in the TS frontend yet.)

- **DEFER `kotlinc_worker(graph, ...)`** entirely — it references a brix-shipped
  Java source. Do not implement it.

Then: export from `index.ts`, and write `ts/oracle/py_kotlin_skin.py` +
`ts/src/test/skin_kotlin.test.ts`. The reference graph should exercise
`JarAction` (spawn path) and the `kotlin-abi` lens (e.g. via `abi(...)` on some
data), staying within the non-deferred surface.

---

## How to build and verify

Prerequisites on the machine: `node` (22+), `cargo`, `python3`. From the repo:

```bash
cd ts
npm install                 # once
npm run build:native:debug  # builds the Rust engine addon (do NOT edit Rust — just build it)
npx tsc -p tsconfig.json    # compile the TS; must exit 0 with no errors
node --test dist/test/*.test.js
```

**Definition of done:**
- `npx tsc` compiles with **zero errors**.
- `node --test` reports **all tests passing**, including your two new ones
  (`skin_rust` and `skin_kotlin`) — i.e. each new skin's lowering
  `deepStrictEqual`s the Python skin's.
- You changed files **only under `ts/`**.
- The four existing skins and their oracles still pass, untouched.

If a new oracle fails, the message shows the JSON diff between the TS and Python
specs — adjust your TS lowering until they match. Do not touch the Python side.
