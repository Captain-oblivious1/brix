"""The equivalence oracle for the TS provider, run against **real vitest**
(design: docs/test-impact-selection.md, "The equivalence oracle").

`test_testimpact_ts.py` proves the provider parses and resolves imports
correctly; this proves the governing invariant end to end. It writes real TS,
runs vitest for real, and asserts:

    **every test file that fails in the full run is in the selected set.**

Reuses the `examples/tested_ts` install (its node_modules is symlinked into a
throwaway workspace), so it needs no network. Skipped if that install is absent.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from brix.testimpact import SelectionInput, select_tests, _vocabulary
from brix.testimpact_ts import TsProvider

REPO = Path(__file__).resolve().parents[1]
NODE_MODULES = REPO / "examples/tested_ts/node_modules"
VITEST = NODE_MODULES / ".bin/vitest"

needs_vitest = pytest.mark.skipif(
    not VITEST.exists(),
    reason="no vitest install (run `npm install` in examples/tested_ts)",
)

# util <- adder (add = a+b+base-10, so 5 at baseline); greeter independent.
BASELINE = dict(uval=10, gword="Hello")


def _sources(uval, gword) -> dict:
    return {
        "package.json": '{ "type": "module" }\n',
        "src/util.ts": f"export function base() {{ return {uval}; }}\n",
        "src/adder.ts": "import { base } from './util';\n"
                        "export function add(a: number, b: number) { return a + b + base() - 10; }\n",
        "src/greeter.ts": f"export function greet(w: string) {{ return `{gword}, ${{w}}!`; }}\n",
        "test/adder.test.ts": "import { expect, test } from 'vitest';\n"
                              "import { add } from '../src/adder';\n"
                              "test('adds', () => expect(add(2, 3)).toBe(5));\n",
        "test/greeter.test.ts": "import { expect, test } from 'vitest';\n"
                                "import { greet } from '../src/greeter';\n"
                                "test('greets', () => expect(greet('World')).toBe('Hello, World!'));\n",
    }


_PARAM_UNIT = {"uval": "src/util.ts", "gword": "src/greeter.ts"}
FILES = ["src/util.ts", "src/adder.ts", "src/greeter.ts",
         "test/adder.test.ts", "test/greeter.test.ts"]
TESTS = ["test/adder.test.ts", "test/greeter.test.ts"]


def _write(root: Path, sources: dict) -> None:
    for rel, text in sources.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _run(root: Path, test_file: str) -> bool:
    """Run one test file under vitest; True when it passed."""
    proc = subprocess.run(
        [str(VITEST), "run", test_file],
        cwd=root, capture_output=True, text=True,
    )
    return proc.returncode == 0


def _failing(root: Path) -> set:
    return {t for t in TESTS if not _run(root, t)}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("ts_oracle")
    _write(root, _sources(**BASELINE))
    os.symlink(NODE_MODULES, root / "node_modules")  # reuse the example's install
    return root


@needs_vitest
def test_baseline_is_green(world):
    assert _failing(world) == set()


@needs_vitest
@pytest.mark.parametrize(
    "unit,edit",
    [
        ("src/util.ts", dict(uval=11)),     # shared module: adder imports it, greeter does not
        ("src/greeter.ts", dict(gword="Hi")),  # independent leaf
    ],
)
def test_selected_run_matches_the_full_run(world, unit, edit):
    provider = TsProvider(files=FILES, root=world)
    view = provider.view()
    selection = select_tests(
        SelectionInput(view, frozenset({unit}), frozenset(_vocabulary(view)))
    )

    _write(world, _sources(**{**BASELINE, **edit}))
    try:
        failing = _failing(world)
        assert failing, f"editing {unit} turned nothing red — oracle is toothless"
        missed = failing - selection.tests
        assert not missed, (
            f"UNDER-SELECTION editing {unit}: the full run fails {sorted(missed)} "
            f"but selection ran {sorted(selection.tests)}"
        )
    finally:
        _write(world, _sources(**BASELINE))


@needs_vitest
def test_selection_actually_skips_work(world):
    """Editing greeter must leave adder's test unselected."""
    provider = TsProvider(files=FILES, root=world)
    view = provider.view()
    selection = select_tests(
        SelectionInput(view, frozenset({"src/greeter.ts"}), frozenset(_vocabulary(view)))
    )
    assert not selection.run_all
    assert selection.tests == {"test/greeter.test.ts"}
