"""Guard the polyglot (Java+TS) monorepo dogfood.

The claim under test — the one the single-language dogfoods don't make — is that
lazy loading partitions a *mixed-language* workspace: a backend (Java) edit
never loads a frontend (TS) package, and vice versa, while an intra-stack
dependent (TS `app` on `shared`) is still pulled in. Selection is graph
structure, so it is checked with no build tools; a gated build then confirms the
Java slice is buildable here (TS builds where `tsc` is present).

The example is copied to a temp dir so nothing is left under examples/.
"""

import shutil
from pathlib import Path

import pytest

from brix import IncrementalBuilder, load_affected, load_workspace

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "polyglot_monorepo"

CASES = [
    ("backend/libs/model/src/main/java/com/example/model/Model.java",
     {"backend/libs/model"}),
    ("backend/services/orders/src/main/java/com/example/orders/Orders.java",
     {"backend/services/orders"}),
    ("frontend/shared/src/index.ts", {"frontend/shared", "frontend/app"}),
    ("frontend/app/src/main.ts", {"frontend/app"}),
    ("notes.txt", set()),
]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "polyglot_monorepo"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("build", "__pycache__"))
    load_workspace(root).save_index()  # record the dependency index (no build)
    return root


def _loaded(root, changed):
    ws = load_affected(root, changed=changed)
    return {Path(p).resolve().relative_to(root).as_posix() for p in ws.patterns}


@pytest.mark.parametrize("changed,expected", CASES)
def test_cross_language_selection(workspace, changed, expected):
    assert _loaded(workspace, [changed]) == expected


def test_app_tracks_its_npm_dependency(workspace):
    # frontend/app declares an npm dep via ts.npm; its .d.ts is tracked through
    # the ts-dts lens (not placed on the command line — tsc resolves it on disk).
    from brix.ts import DtsFile

    ws = load_workspace(workspace)
    app = next(a for a in ws.graph.actions if a.name == "tsc -> build/frontend/app")
    lensed = {a.path for a in app.inputs["types"].atoms() if isinstance(a, DtsFile)}
    assert "frontend/node_modules/pretty/index.d.ts" in lensed


def test_backend_and_frontend_never_cross(workspace):
    # No backend edit pulls in a frontend package, and no frontend edit pulls in
    # a backend package — stated directly, over every package.
    backend = _loaded(workspace, ["backend/libs/model/src/main/java/com/example/model/Model.java"])
    frontend = _loaded(workspace, ["frontend/shared/src/index.ts"])
    assert not any(p.startswith("frontend/") for p in backend)
    assert not any(p.startswith("backend/") for p in frontend)


_have_jdk = all(shutil.which(t) for t in ("javac", "jar"))


@pytest.mark.skipif(not _have_jdk, reason="no JDK (javac/jar)")
def test_backend_java_slice_builds(workspace):
    ws = load_affected(
        workspace,
        changed=[
            "backend/libs/model/src/main/java/com/example/model/Model.java",
            "backend/services/orders/src/main/java/com/example/orders/Orders.java",
        ],
    )
    result = IncrementalBuilder(lambda: ws.graph, root=workspace).build()
    assert result.success, result.errors
    assert (workspace / "build/backend/model.jar").is_file()
    assert (workspace / "build/backend/orders.jar").is_file()
