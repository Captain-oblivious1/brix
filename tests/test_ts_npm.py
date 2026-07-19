"""ts.npm — resolve npm packages' TypeScript type declarations into a DepSet.

brix does not run npm; these tests lay out a fake ``node_modules`` on disk (a
package that ships its own types, a package typed via DefinitelyTyped, a scoped
package) and check that ``ts.npm`` collects the right ``.d.ts`` and that
``ts.CompileAction(deps=…)`` tracks them through the ts-dts lens. No tsc needed:
resolution is filesystem + graph structure.
"""

from pathlib import Path

import pytest

from brix import Graph, ts
from brix.deps import DepSet
from brix.ts import DtsFile


def _pkg(root: Path, rel: str, dts: dict[str, str]) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text('{"name": "x", "types": "index.d.ts"}')
    for name, body in dts.items():
        (d / name).write_text(body)


@pytest.fixture
def node_modules(tmp_path: Path) -> Path:
    nm = tmp_path / "node_modules"
    # ships its own types, with a nested sub-declaration
    _pkg(nm, "left-pad", {"index.d.ts": "export function leftPad(): string;",
                          "util.d.ts": "export function u(): number;"})
    # typed via DefinitelyTyped (@types/lodash)
    _pkg(nm, "@types/lodash", {"index.d.ts": "export function chunk(): void;"})
    # scoped package with its own types
    _pkg(nm, "@acme/widget", {"index.d.ts": "export const w: number;"})
    # a nested install that must NOT be tracked (top-level is hoisted)
    _pkg(nm, "left-pad/node_modules/dep", {"index.d.ts": "export const x: 1;"})
    return tmp_path


def _rel(depset: DepSet) -> set[str]:
    return set(depset.artifacts)


def test_resolves_own_types_including_nested_but_not_nested_node_modules(node_modules):
    got = _rel(ts.npm(["left-pad"], root=node_modules))
    assert got == {"node_modules/left-pad/index.d.ts", "node_modules/left-pad/util.d.ts"}
    # the nested node_modules/dep .d.ts is excluded
    assert not any("node_modules/dep" in p for p in got)


def test_resolves_definitely_typed_at_types(node_modules):
    got = _rel(ts.npm(["lodash"], root=node_modules))
    assert got == {"node_modules/@types/lodash/index.d.ts"}


def test_resolves_scoped_package(node_modules):
    got = _rel(ts.npm(["@acme/widget"], root=node_modules))
    assert got == {"node_modules/@acme/widget/index.d.ts"}


def test_multiple_names_compose_and_dedup(node_modules):
    got = ts.npm(["left-pad", "lodash", "left-pad"], root=node_modules)
    assert got.artifacts.count("node_modules/left-pad/index.d.ts") == 1
    assert "node_modules/@types/lodash/index.d.ts" in got.artifacts


def test_missing_package_raises_actionably(node_modules):
    with pytest.raises(FileNotFoundError, match="no .d.ts type declarations for 'nope'"):
        ts.npm(["nope"], root=node_modules)


def test_compileaction_tracks_deps_through_the_ts_dts_lens(node_modules):
    deps = ts.npm(["left-pad"], root=node_modules)
    graph = Graph()
    d = graph.data
    graph.add(
        ts.CompileAction(
            sources=d.glob("src/{name}.ts"),
            out_dir="dist",
            deps=deps,
            track_imports=False,
        )
    )
    (action,) = graph.actions
    # The dep .d.ts ride the "types" input, ts-dts-lensed (DtsFile), and are
    # tracked, not placed on the command line (tsc resolves node_modules itself).
    types = action.inputs["types"]
    lensed = {a.path for a in types.atoms() if isinstance(a, DtsFile)}
    assert "node_modules/left-pad/index.d.ts" in lensed
    assert "{types}" not in action.command_line


def test_deps_compose_with_explicit_dependencies(node_modules):
    deps = ts.npm(["lodash"], root=node_modules)
    graph = Graph()
    d = graph.data
    graph.add(
        ts.CompileAction(
            sources=d.glob("src/{name}.ts"),
            out_dir="dist",
            dependencies=[d.glob("build/shared/{name}.d.ts")],
            deps=deps,
            track_imports=False,
        )
    )
    (action,) = graph.actions
    lensed = {a.path for a in action.inputs["types"].atoms() if isinstance(a, DtsFile)}
    # both the upstream module's .d.ts glob and the npm .d.ts are tracked
    assert "node_modules/@types/lodash/index.d.ts" in lensed
