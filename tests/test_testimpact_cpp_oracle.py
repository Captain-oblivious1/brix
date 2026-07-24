"""The equivalence oracle for the C++ provider, run against **real gtest**
(design: docs/test-impact-selection.md, "The equivalence oracle").

`test_testimpact_cpp.py` proves the provider reads depfiles and symbols
correctly; this proves the governing invariant end to end. It compiles real test
binaries, edits code, runs the binaries for real, and asserts:

    **every test binary that fails in the full run is in the selected set.**

Over-selection is free; a binary that fails the full run and is not selected is
the failure mode the whole design exists to make impossible. Needs g++, the
gtest libraries, and nm — skipped otherwise.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from brix.testimpact import SelectionInput, select_tests, _vocabulary
from brix.testimpact_cpp import CppProvider

_GTEST_LIB = any(
    list(Path(d).glob("libgtest.*"))
    for d in ("/usr/lib", "/usr/lib64", "/usr/local/lib")
    if Path(d).is_dir()
)
needs_gtest = pytest.mark.skipif(
    shutil.which("g++") is None or shutil.which("nm") is None or not _GTEST_LIB,
    reason="no g++/nm/gtest",
)

GTEST_LINK = ["-lgtest", "-lgtest_main", "-lpthread"]

# util <- a (a_val = util_val + util_bonus), and an independent b. util_bonus is
# an inline in the *header*, so editing the header must propagate to a_test.
BASELINE = dict(uval=1, hval=10, bval=9)  # a_val = 11, b_val = 9


def _sources(uval, hval, bval) -> dict:
    return {
        "include/util.h": f"#pragma once\nint util_val();\ninline int util_bonus(){{ return {hval}; }}\n",
        "src/util.cpp": f'#include "util.h"\nint util_val(){{ return {uval}; }}\n',
        "include/a.h": "#pragma once\nint a_val();\n",
        "src/a.cpp": '#include "a.h"\n#include "util.h"\nint a_val(){ return util_val()+util_bonus(); }\n',
        "include/b.h": "#pragma once\nint b_val();\n",
        "src/b.cpp": f'#include "b.h"\nint b_val(){{ return {bval}; }}\n',
        # Tests assert the baseline values; a code edit that changes them fails.
        "test/a_test.cpp": '#include <gtest/gtest.h>\n#include "a.h"\nTEST(A, v){ EXPECT_EQ(a_val(), 11); }\n',
        "test/b_test.cpp": '#include <gtest/gtest.h>\n#include "b.h"\nTEST(B, v){ EXPECT_EQ(b_val(), 9); }\n',
    }


# Which source parameter names which unit, so an edit maps to a changed file.
_PARAM_UNIT = {"uval": "src/util.cpp", "hval": "include/util.h", "bval": "src/b.cpp"}

# Each test binary and the objects linked into it (the link graph the provider
# is handed). a_test pulls in the shared util object; b_test does not.
LINK = {
    "a_test": ["src/a.o", "src/util.o", "test/a_test.o"],
    "b_test": ["src/b.o", "test/b_test.o"],
}
SOURCES = ["src/util.cpp", "src/a.cpp", "src/b.cpp", "test/a_test.cpp", "test/b_test.cpp"]


def _write(root: Path, sources: dict) -> None:
    for rel, text in sources.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _compile_all(root: Path) -> None:
    for rel in SOURCES:
        obj = f"{rel[:-4]}.o"
        proc = subprocess.run(
            ["g++", "-std=c++17", "-Iinclude", "-MMD", "-MF", f"{obj}.d",
             "-c", rel, "-o", obj],
            cwd=root, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr


def _link_all(root: Path) -> None:
    (root / "build").mkdir(exist_ok=True)
    for name, objs in LINK.items():
        proc = subprocess.run(
            ["g++", "-std=c++17", *objs, *GTEST_LINK, "-o", f"build/{name}"],
            cwd=root, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr


def _failing(root: Path) -> set:
    """The full run's failing binaries: run each and collect the non-zero exits."""
    failing = set()
    for name in LINK:
        proc = subprocess.run([f"./build/{name}"], cwd=root, capture_output=True)
        if proc.returncode != 0:
            failing.add(name)
    return failing


def _provider(root: Path) -> CppProvider:
    return CppProvider(tests=LINK, root=root)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("cpp_oracle")
    _write(root, _sources(**BASELINE))
    _compile_all(root)
    _link_all(root)
    return root


@needs_gtest
def test_baseline_is_green(world):
    assert _failing(world) == set()


@needs_gtest
@pytest.mark.parametrize(
    "unit,edit",
    [
        # A leaf source: only b_test links b.
        ("src/b.cpp", dict(bval=0)),
        # The shared library source: a_test links util.o, b_test does not.
        ("src/util.cpp", dict(uval=2)),
        # A header carrying an inline used only by a: the change must propagate
        # through the depfile to a_test, and reach nothing in b.
        ("include/util.h", dict(hval=20)),
    ],
)
def test_selected_run_matches_the_full_run(world, unit, edit):
    provider = _provider(world)
    view = provider.view()
    known = frozenset(_vocabulary(view))
    selection = select_tests(SelectionInput(view, frozenset({unit}), known))

    _write(world, _sources(**{**BASELINE, **edit}))
    _compile_all(world)
    _link_all(world)
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
        _compile_all(world)
        _link_all(world)


@needs_gtest
def test_selection_actually_skips_work(world):
    """Equivalence is trivial if we always run everything. Editing b must leave
    a_test unselected."""
    provider = _provider(world)
    view = provider.view()
    selection = select_tests(
        SelectionInput(view, frozenset({"src/b.cpp"}), frozenset(_vocabulary(view)))
    )
    assert not selection.run_all
    assert selection.tests == {"b_test"}
