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

## Not yet wired into a brix build

Unlike `tested_lib` (Java) and `tested_cpp` (C++), this example does not yet
select *inside a brix build* — there is no `brix_build.py` here. The provider and
its equivalence guarantee are done; the remaining slice is a brix TS test-run
action. The intended shape mirrors C++: run `vitest run <file>` per test file as
a tracked action whose byte-lens inputs are that file's transitive import
closure, so the incremental engine re-runs only the test files a change reaches —
per-file selection for free, exactly as `tested_cpp` gets per-binary selection.
