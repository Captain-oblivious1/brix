# tested_ts — TypeScript test-impact

The TypeScript side of test-impact selection. Two modules with an import edge
(`adder` imports `util`) and an independent `greeter`, each with a vitest test.
It's the fixture the TS provider is proven on, and the shape a real workspace
takes.

## The provider

`brix.testimpact_ts` reads TS's binding unit — the **module (file)** — from the
static `import` graph:

- `code_deps` — `file → the workspace files it imports` (relative specifiers
  resolved to real files; bare specifiers like `vitest` are external and inert).
- `test_edges` — a `*.test.ts` file → the files it imports; its closure is those
  plus everything they transitively import.
- **dynamic** — a file doing `import(<expr>)` / `require(<expr>)` on a *computed*
  specifier loads a module by a run-time name, so it's marked dynamic and every
  test reaching it always runs. A dynamic import with a *literal* specifier is
  statically resolvable and stays an ordinary edge.

So editing `util.ts` selects `adder`'s test (it imports `adder`, which imports
`util`) and never `greeter`'s — the same closure-selection as every other
provider, over the same language-agnostic core.

## Proven against real vitest

`tests/test_testimpact_ts_oracle.py` is the equivalence oracle: it edits a
module, runs vitest for real, and asserts **every test file that fails in the
full run is selected**. Teeth-checked — a blinded selection misses the failing
test and the oracle reports UNDER-SELECTION.

## Run the tests

```
npm install        # typescript + vitest, locally
npm test           # vitest run — both pass
```

The oracle reuses this install (its `node_modules` is symlinked into a throwaway
workspace), so once you've run `npm install` here the oracle needs no network.

## Selection in a real brix build

`brix_build.py` wires it in, mirroring C++'s engine-native approach:

```
python brix_build.py build      # cold — both test files run
python brix_build.py build      # CACHED — nothing runs
# edit src/util.ts   (adder imports it; greeter does not)
python brix_build.py build      # re-runs adder's test only; greeter's replays
```

Each test file is one `ts.RunTestAction` — `vitest run <file>`, no outputs, so it
replays on its input fingerprint. Its byte-lens inputs are the test file plus its
transitive import closure, computed by `TsProvider.test_input_sets()`. So the
incremental engine re-runs a test only when a file in its closure changes:
**per-file selection, done by the engine**, because the run unit (a test file) is
the selection unit — exactly how `tested_cpp` gets per-binary selection.

The one TS-specific piece: unlike a C++ binary, a test file is not a build product
whose staleness the engine already tracks, so each `RunTestAction` is handed its
import closure explicitly. A test reaching a computed `import()`/`require()` or an
unresolved import is given the whole source set instead (run on any change) — the
footprint form of fail-open.

Note: because brix tracks the *source* bytes directly (no compile step folds
anything away), any edit to a file in a test's closure re-runs it — even a change
a compiler would optimize out. That's the safe direction.
