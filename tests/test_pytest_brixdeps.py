"""The brix pytest plugin: it records the project modules a test run imported as
a brix JSON dependency manifest (the Python side of test-impact by discovery).

Run in a subprocess (a fresh interpreter) so `sys.modules` reflects only the
test's own import closure, not this suite's — which is exactly how a per-test-file
brix action would invoke it.
"""

import json
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run_pytest(workspace: Path, testfile: str, manifest: Path) -> int:
    env = {"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", testfile,
            "-p", "brix._python.pytest_brixdeps",
            "--brix-manifest", str(manifest),
            "-q",
        ],
        cwd=workspace, env=env, capture_output=True, text=True,
    )
    return proc.returncode


def test_manifest_lists_only_imported_project_modules(tmp_path):
    # A tiny project: two modules, a test that imports only one of them.
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("")
    (tmp_path / "pkg/used.py").write_text("VALUE = 1\n")
    (tmp_path / "pkg/unused.py").write_text("OTHER = 2\n")
    (tmp_path / "test_it.py").write_text(
        "from pkg.used import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    manifest = tmp_path / "out/dep.json"

    assert _run_pytest(tmp_path, "test_it.py", manifest) == 0

    inputs = set(json.loads(manifest.read_text())["inputs"])
    # The imported module and the test are recorded; the un-imported one is not,
    # and neither is the stdlib / pytest (outside the workspace).
    assert "pkg/used.py" in inputs
    assert "test_it.py" in inputs
    assert "pkg/unused.py" not in inputs
    assert not any(i.startswith("/") for i in inputs)  # relative only


def test_no_manifest_without_the_option(tmp_path):
    """The plugin is inert unless --brix-manifest is passed (no file written)."""
    (tmp_path / "test_it.py").write_text("def test_ok():\n    assert True\n")
    env = {"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_it.py",
         "-p", "brix._python.pytest_brixdeps", "-q"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert not list(tmp_path.glob("**/*.json"))
