# Hierarchical build files + lazy loading

A monorepo doesn't need one giant `brix_build.py`. Split it into package build files
placed sparsely at module/service boundaries; each is responsible for its
subtree until a deeper `brix_build.py` carves out its own. This example is the
smallest workspace that shows both halves of the story — and doubles as the
copy-paste template for adopting it.

## The workspace

```
brix_build.py                 workspace entry point (no create_graph — it wires cli.main)
tools/step.py           one shared build tool, an input of every package
libs/core/    brix_build.py   core:  src.txt          -> build/core/core.txt
services/api/ brix_build.py   api:   build/core/*     -> build/api/api.txt     (depends on core)
services/web/ brix_build.py   web:   build/api/*      -> build/web/web.txt     (depends on api)
docs/         brix_build.py   docs:  src.txt          -> build/docs/docs.txt   (independent)
```

The chain is `core → api → web`; `docs` shares nothing with it. No package
*declares* a dependency on another — `api` simply consumes the file `core`
produces, and brix derives the edge from that overlap.

## Build it (the safe default)

```
python3 examples/hierarchical/brix_build.py build
python3 examples/hierarchical/brix_build.py status
```

The root `brix_build.py` calls `load_workspace(ROOT).graph`: it loads **every**
package and merges them, so a hierarchical workspace builds *identically* to the
equivalent monolith. This is the floor — nothing is skipped, nothing can go
stale. `build/web/web.txt` ends up holding `web(api(core(hello)))`, the visible
lineage of the whole chain.

## Load only what changed (the opt-in)

Swap the entry point's one line for `load_affected(ROOT).graph` and brix asks
git what changed since the last build, then loads only the affected packages
plus their dependents — falling back to a full load whenever it can't *prove* a
package is unaffected. The invariant is deliberately one-sided: **over-loading
wastes a parse; under-loading ships a stale build**, so every uncertainty
resolves toward loading more.

## Prove it

```
python3 examples/hierarchical/verify_lazy.py
```

drives `load_affected` with explicit change sets (so it needs no git state) and
checks the loaded set against expectations — and every subset build against a
from-scratch full build, byte-for-byte:

| edit | loads | skips |
|---|---|---|
| `libs/core/src.txt` (a leaf source) | `core`, `api`, `web` | `docs` |
| `docs/src.txt` (the independent package) | `docs` | the chain |
| `tools/step.py` (the shared tool) | **everything** | — |
| an unreferenced file | **nothing** | everything |

The shared-tool row is the point: `tools/step.py` lives in no package's subtree,
so "load the package that owns the changed file" would load *nothing* and ship a
stale build. Selection is pattern-based instead — every package that references
the tool is loaded — which is why editing it correctly rebuilds the world.
