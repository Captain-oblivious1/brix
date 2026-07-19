# Polyglot monorepo (Java + TypeScript)

The closest available rehearsal of the air-gapped target — a ~50-service Java+TS
monorepo — in miniature. Two independent stacks live in one workspace and one
git repo:

```
backend/   Java   libs/model, services/orders          (java.JarAction)
frontend/  TS     shared, app  (app depends on shared)  (ts.CompileAction, ts-dts lens)
```

Each package is a sparse `brix.py` exposing `create_graph()`; the workspace
loader (`load_workspace`) merges them and builds identically to a monolith.

## What it proves that the single-language dogfoods don't

**Lazy loading partitions a mixed-language workspace.** `load_affected` loads
only the packages a change touches — *across languages*:

| you edit | brix loads | it does **not** load |
|---|---|---|
| a backend Java package | just that package | any frontend package |
| the TS library `shared` | `shared` **and** its dependent `app` | any backend package |
| the TS app | just `app` | everything else |
| an unreferenced file | nothing | — |

A backend edit never loads a frontend package and vice versa, so a large repo
touches only the affected slice regardless of how many languages it spans. The
`shared → app` edge is derived from `app` declaring `shared`'s built `.d.ts` as a
dependency (the **ts-dts** lens) — no cross-package edge is hand-declared.

## Run it

```
python3 examples/polyglot_monorepo/verify_polyglot.py
```

Selection is checked with **no build tools** (it is graph structure). The script
then builds for real what the toolchain present allows — the Java packages with
a JDK, the TS packages where `tsc` is available (`BRIX_TSC=…` or a global
`tsc`). Git-driven selection (`changed=None`, git enumerates the change set) is
the same call and is proven in `../monorepo_dogfood.py`; this example uses
explicit change sets so it needs no git state.

`tests/test_polyglot_monorepo.py` guards the selection partition (both engines)
and the Java build.
