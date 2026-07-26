"""Typed Python actions.

Currently: `PytestAction`, which runs a project's pytest suite with **per-test-file
impact selection by import discovery** — the Python member of brix's test-impact
family (Java `-Xlog:class+load`, C++ `-MMD`, TS imports).
"""

from __future__ import annotations

import sys

from .actions import Action, ExpandAll, ExternalProcessAction
from .data import DataExpr, FileData, GlobData, ToolData, ValueData

__all__ = ["PytestAction"]

# brix's pytest plugin: at session end it writes a brix JSON dependency manifest
# of the project modules the run imported. Loaded with `-p`; requires brix to be
# importable by the interpreter running pytest (installed, or via env=PYTHONPATH).
_PLUGIN = "brix._python.pytest_brixdeps"


def _single_binding(collection: GlobData, what: str) -> str:
    names = collection.pattern.binding_names
    if len(names) != 1:
        raise ValueError(
            f"{what} must have exactly one {{binding}}, got {collection.pattern.template!r}"
        )
    return names[0]


def _options_expr(options: list[str] | DataExpr | None) -> DataExpr:
    if isinstance(options, DataExpr):
        return options
    return ValueData(" ".join(options or []))


class PytestAction(Action):
    """Run each matched test file as its own pytest process, with per-file impact
    selection by import discovery.

    Lowers to an ``ExpandAll`` over ``test_sources`` — one ``pytest <file>`` per
    test file, wrapped with brix's pytest plugin, which records the project
    modules that file's tests actually imported as a brix JSON dependency
    manifest. brix folds it in via ``depfile=``, so editing a module re-runs only
    the test files that import it; the rest replay. Each run declares only the
    manifest as a file output, so the test verdict is the exit code, replayed on
    the fingerprint (which now includes the discovered modules). This is the
    Python analogue of ``java.JUnitTestAction`` / ``ts.RunTestAction`` /
    ``cpp.RunTestAction``.

    ``test_sources`` is a glob with exactly one ``{binding}`` (e.g.
    ``tests/test_{name}.py``); each match becomes one run. ``python`` is the
    interpreter (default the current one) — **brix must be importable by it**
    (pip-installed into its environment, or add brix's source via ``env=`` with a
    ``PYTHONPATH``). ``manifest_dir`` is where the per-file manifests are written;
    ``options`` are extra pytest arguments; ``env`` is passed through to each run.
    """

    def __init__(
        self,
        *,
        test_sources: GlobData,
        python: ToolData | None = None,
        manifest_dir: str = "build/.brix/pytest",
        options: list[str] | DataExpr | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.test_sources = test_sources
        self.python = python if python is not None else ToolData(sys.executable)
        self.manifest_dir = manifest_dir.rstrip("/")
        self.options = options
        self.env = dict(env or {})
        self._binding = _single_binding(test_sources, "test_sources")

    def lower(self) -> list[Action]:
        name = self._binding
        manifest = f"{self.manifest_dir}/{{{name}}}.json"
        run_one = ExternalProcessAction(
            name=f"pytest {{{name}}}",
            command_line=(
                "{python} -m pytest {testfile}"
                f" -p {_PLUGIN} --brix-manifest {{manifest}} {{options}}"
            ),
            inputs={
                "python": self.python,
                "testfile": FileData(self.test_sources.pattern.template),
                "options": _options_expr(self.options),
            },
            # The manifest is the only file output; the verdict is the exit code,
            # replayed on the fingerprint. A failed run drops it and re-runs.
            outputs={"manifest": FileData(manifest)},
            depfile=manifest,
            env=self.env,
        )
        return [
            ExpandAll(element_name=name, collection=self.test_sources, child=run_one)
        ]
