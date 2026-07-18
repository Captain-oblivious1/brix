"""TypeScript import tracking: editing an undeclared imported source rebuilds.

The correctness hole this closes: a module's declared `sources` glob captures
its own tree, but a TS `import` can reach a shared `.ts` *outside* that tree.
Without discovering the real import graph, editing that shared file would leave
the module stale — the TS version of the C/C++ header problem.

`ts.CompileAction(track_imports=True)` (the default) wraps `tsc` in brix's
`--listFiles` recorder, which writes a Makefile depfile the incremental layer
scans like any C/C++ one. These tests drive the whole channel with an
**executable fake `tsc`** (no real tsc needed): it models `// import: <path>`
comments as the import graph, emits `.js`/`.d.ts`, and prints an absolute
`--listFiles` list — including a `node_modules` entry and an out-of-tree file —
so the wrapper's filtering is exercised too. `test_ts_equivalence.py` covers the
same behavior against a real tsc when one is present.
"""

import os
import shutil
import stat
from pathlib import Path

import pytest

from brix import Graph, IncrementalBuilder, Reporter, ts
from brix.data import ToolData

# A stand-in for tsc: resolves `// import: <relpath>` comments as the import
# graph, concatenates the transitive set into each `.js` (so an imported edit
# changes output bytes), emits a `.d.ts`, and on --listFiles prints absolute
# paths of every file read plus a node_modules and an out-of-tree path.
FAKE_TSC = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path

args = sys.argv[1:]
list_files = "--listFiles" in args
args = [a for a in args if a != "--listFiles"]

out_dir, sources = None, []
i = 0
while i < len(args):
    a = args[i]
    if a == "--outDir":
        out_dir = args[i + 1]; i += 2; continue
    if a.startswith("--"):
        i += 1; continue          # valueless flags: --declaration, --module...
    sources.append(a); i += 1

def imports_of(path):
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if s.startswith("// import:"):
            yield s.split(":", 1)[1].strip()

def transitive(seed):
    seen, stack = [], [seed]
    while stack:
        f = stack.pop(0)
        if f in seen or not Path(f).is_file():
            continue
        seen.append(f)
        stack.extend(imports_of(f))
    return seen

read = []
out = Path(out_dir)
out.mkdir(parents=True, exist_ok=True)
for s in sources:
    files = transitive(s)
    read.extend(files)
    js = "".join(Path(f).read_text() for f in files)
    stem = Path(s).stem
    (out / f"{stem}.js").write_text(js)
    (out / f"{stem}.d.ts").write_text(f"// decl {stem}\n")

if list_files:
    for f in dict.fromkeys(read):
        print(os.path.abspath(f))
    print(os.path.abspath(os.environ["FAKE_NODE_MODULES"]))
    print(os.path.abspath(os.environ["FAKE_OUTSIDE"]))
'''


class Ran(Reporter):
    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "app").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "node_modules/typescript/lib").mkdir(parents=True)
    # app/main.ts imports a shared file OUTSIDE its declared `sources` glob.
    (root / "app/main.ts").write_text(
        "// import: shared/util.ts\nexport const main = 1;\n"
    )
    (root / "shared/util.ts").write_text("export const util = 'v1';\n")
    (root / "shared/unused.ts").write_text("export const unused = 1;\n")
    (root / "node_modules/typescript/lib/lib.d.ts").write_text("// lib\n")
    outside = tmp_path / "outside.ts"
    outside.write_text("// outside the workspace\n")

    fake = tmp_path / "fake-tsc"
    fake.write_text(FAKE_TSC)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("FAKE_NODE_MODULES", str(root / "node_modules/typescript/lib/lib.d.ts"))
    monkeypatch.setenv("FAKE_OUTSIDE", str(outside))
    # No engine override: the wrapper is a plain command and the depfile scan
    # exists in both engines, so this runs on the default (Rust) engine and,
    # when the suite is run with BRIX_ENGINE=py, on the Python one.
    return root, fake


def _graph(root, fake):
    def make():
        g = Graph()
        d = g.data
        g.add(
            ts.CompileAction(
                sources=d.glob("app/{name}.ts"),
                out_dir="app/dist",
                tsc=ToolData(str(fake)),
            )
        )
        return g

    return make


def _build(root, fake):
    rep = Ran()
    result = IncrementalBuilder(_graph(root, fake), root=root).build(reporter=rep)
    assert result.success, result.errors
    return rep.labels


def test_editing_an_undeclared_imported_source_rebuilds(workspace):
    root, fake = workspace
    assert _build(root, fake)  # clean build runs
    assert _build(root, fake) == []  # no-op: nothing re-runs

    # The imported (but undeclared) shared file changes -> the compile re-runs.
    (root / "shared/util.ts").write_text("export const util = 'v2';\n")
    assert _build(root, fake) == ["tsc -> app/dist"]
    # ...and its content actually flows into the emitted .js.
    assert "v2" in (root / "app/dist/main.js").read_text()


def test_editing_an_unimported_sibling_does_not_rebuild(workspace):
    root, fake = workspace
    _build(root, fake)
    # shared/unused.ts is never imported, so it is not a discovered dep.
    (root / "shared/unused.ts").write_text("export const unused = 2;\n")
    assert _build(root, fake) == []


def test_depfile_records_only_workspace_sources(workspace):
    root, fake = workspace
    _build(root, fake)
    depfile = (root / "app/dist/.brix-tsdeps.d").read_text()
    # The declared source and the undeclared import are recorded...
    assert "app/main.ts" in depfile
    assert "shared/util.ts" in depfile
    # ...but node_modules, the out-of-tree file, and emitted .js are not.
    assert "node_modules" not in depfile
    assert "outside" not in depfile
    assert ".js" not in depfile


def test_track_imports_false_is_a_bare_spawn(workspace):
    root, fake = workspace

    def make():
        g = Graph()
        g.add(
            ts.CompileAction(
                sources=g.data.glob("app/{name}.ts"),
                out_dir="app/dist",
                tsc=ToolData(str(fake)),
                track_imports=False,
            )
        )
        return g

    rep = Ran()
    assert IncrementalBuilder(make, root=root).build(reporter=rep).success
    # No depfile is written when tracking is off.
    assert not (root / "app/dist/.brix-tsdeps.d").exists()
