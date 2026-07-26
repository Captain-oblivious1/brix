"""A pytest plugin that records which project modules a test run imported, as a
brix JSON dependency manifest — the Python counterpart of the Java
`-Xlog:class+load` recorder and the C++ `-MMD` depfile.

A Python test's real dependency set is the modules it imports (transitively).
That is awkward to predict statically, but trivial to observe: after a pytest
session, `sys.modules` holds exactly what was loaded. This plugin walks it at
session end, keeps the workspace ``.py`` files (dropping the stdlib and anything
under the Python environment — the venv, which often lives inside the project),
and writes them as ``{"version": 1, "inputs": [...]}`` to the path given by the
``--brix-manifest`` option. Point a brix action's ``depfile=`` at that same path
and brix re-runs the test only when a module it actually imported changes.

Enable with ``pytest -p brix._python.pytest_brixdeps --brix-manifest <path>``.
Run **one test file per pytest process** for per-file selection (each process's
`sys.modules` is then that file's own import closure, not the union of the whole
suite).
"""

import json
import sys
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption(
        "--brix-manifest",
        action="store",
        default=None,
        help="write a brix JSON dependency manifest of imported modules here",
    )


def _manifest_inputs(workspace: Path) -> list[str]:
    env_roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    inputs: set[str] = set()
    for module in list(sys.modules.values()):
        source = getattr(module, "__file__", None)
        if not source:
            continue
        resolved = Path(source).resolve()
        if resolved.suffix != ".py":
            continue
        if any(resolved.is_relative_to(root) for root in env_roots):
            continue  # installed dependency (venv / site-packages), not source
        try:
            inputs.add(resolved.relative_to(workspace).as_posix())
        except ValueError:
            continue  # imported source outside the workspace tree
    return sorted(inputs)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 (pytest hook)
    dest = session.config.getoption("--brix-manifest")
    if not dest:
        return
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"version": 1, "inputs": _manifest_inputs(Path.cwd())}, indent=1)
    )
