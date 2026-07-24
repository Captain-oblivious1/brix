"""The C++ test-impact provider (design: docs/test-impact-selection.md).

The pure parts (depfile parsing, the CTest subsetter) are checked directly; the
`view()` build is checked on *real* compiled objects, because the whole claim is
that an object's `-MMD` depfile plus its `nm` symbols are a sound picture of what
a test binary can execute — a hand-built graph would just restate the claim.
"""

import shutil
import subprocess

import pytest

from brix.testimpact import SelectionInput, select_tests, _vocabulary
from brix.testimpact_cpp import CppProvider, parse_depfile, references_dl

needs_cpp = pytest.mark.skipif(
    shutil.which("g++") is None or shutil.which("nm") is None,
    reason="no g++/nm on PATH",
)


# --- depfile parsing ---------------------------------------------------------

def test_parse_a_single_line_rule():
    target, prereqs = parse_depfile("build/foo.o: src/foo.cpp include/foo.h\n")
    assert target == "build/foo.o"
    assert prereqs == frozenset({"src/foo.cpp", "include/foo.h"})


def test_parse_a_continued_rule():
    text = "build/foo.o: src/foo.cpp include/a.h \\\n include/b.h \\\n include/c.h\n"
    _, prereqs = parse_depfile(text)
    assert prereqs == frozenset(
        {"src/foo.cpp", "include/a.h", "include/b.h", "include/c.h"}
    )


def test_parse_non_rule_is_empty():
    assert parse_depfile("") == (None, frozenset())
    assert parse_depfile("no colon here") == (None, frozenset())


# --- the CTest subsetter -----------------------------------------------------

def test_select_args_are_an_anchored_ctest_filter():
    args = CppProvider(tests={}).select_args(["b_test", "a_test"])
    assert args == ["-R", "^(a_test|b_test)$"]  # sorted, anchored, OR-joined


def test_select_args_empty_when_nothing_selected():
    assert CppProvider(tests={}).select_args([]) == []


# --- view() on real objects --------------------------------------------------

def _compile(tmp_path, rel, source, *, include=()):
    """Compile one .cpp to <rel>.o with a -MMD depfile, cwd=tmp_path so paths
    in the depfile are repo-relative (as brix writes them)."""
    src = tmp_path / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(source)
    obj = f"{rel[:-4]}.o"
    (tmp_path / obj).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["g++", "-std=c++17", "-MMD", "-MF", f"{obj}.d", "-c", rel, "-o", obj]
    for inc in include:
        cmd[1:1] = [f"-I{inc}"]
    proc = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return obj


@pytest.fixture
def cpp_world(tmp_path):
    """util <- a, and an independent b, each with its own test object. Mirrors a
    real layout: two test binaries over a shared library object."""
    (tmp_path / "include").mkdir()
    (tmp_path / "include/util.h").write_text("#pragma once\nint util_val();\n")
    (tmp_path / "include/a.h").write_text("#pragma once\nint a_val();\n")
    (tmp_path / "include/b.h").write_text("#pragma once\nint b_val();\n")
    util_o = _compile(tmp_path, "src/util.cpp",
                      '#include "util.h"\nint util_val(){ return 1; }\n', include=["include"])
    a_o = _compile(tmp_path, "src/a.cpp",
                   '#include "a.h"\n#include "util.h"\nint a_val(){ return util_val()+1; }\n',
                   include=["include"])
    b_o = _compile(tmp_path, "src/b.cpp",
                   '#include "b.h"\nint b_val(){ return 9; }\n', include=["include"])
    at_o = _compile(tmp_path, "test/a_test.cpp",
                    '#include "a.h"\nint main(){ return a_val()==2 ? 0 : 1; }\n', include=["include"])
    bt_o = _compile(tmp_path, "test/b_test.cpp",
                    '#include "b.h"\nint main(){ return b_val()==9 ? 0 : 1; }\n', include=["include"])
    return CppProvider(
        tests={"a_test": [a_o, util_o, at_o], "b_test": [b_o, bt_o]},
        root=tmp_path,
    )


def _select(provider, changed):
    # `known` is the full vocabulary — for C++ the changed units are source and
    # header files, which are edge *targets* (a compile's prerequisites), not the
    # object keys; using only the keys would read every source edit as new code.
    view = provider.view()
    return select_tests(SelectionInput(view, frozenset(changed), frozenset(_vocabulary(view))))


@needs_cpp
def test_view_reads_depfiles_and_the_link_graph(cpp_world):
    view = cpp_world.view()
    assert set(view.test_edges) == {"a_test", "b_test"}
    # a's object compiles from a.cpp and pulls in util.h + a.h (user headers)
    assert "src/a.cpp" in view.code_deps["src/a.o"]
    assert "include/util.h" in view.code_deps["src/a.o"]
    assert "include/util.h" not in view.code_deps["src/b.o"]
    assert not view.uncovered
    assert view.binding_unit == "object file"


@needs_cpp
def test_editing_a_shared_header_selects_only_the_binaries_that_include_it(cpp_world):
    # util.h is included by a (via a.o and util.o) but not by b.
    sel = _select(cpp_world, {"include/util.h"})
    assert not sel.run_all
    assert sel.tests == frozenset({"a_test"})


@needs_cpp
def test_editing_a_leaf_source_selects_its_binary_only(cpp_world):
    sel = _select(cpp_world, {"src/b.cpp"})
    assert sel.tests == frozenset({"b_test"})


@needs_cpp
def test_editing_the_shared_library_source_drags_its_dependent(cpp_world):
    # util.cpp feeds util.o, which a_test links; b_test does not.
    sel = _select(cpp_world, {"src/util.cpp"})
    assert sel.tests == frozenset({"a_test"})


@needs_cpp
def test_a_dlopen_object_makes_its_binary_always_run(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include/plug.h").write_text("#pragma once\nint plug();\n")
    plug_o = _compile(tmp_path, "src/plug.cpp",
                      '#include "plug.h"\n#include <dlfcn.h>\n'
                      'int plug(){ void* h=dlopen("x",2); return h?1:0; }\n', include=["include"])
    pt_o = _compile(tmp_path, "test/plug_test.cpp",
                    '#include "plug.h"\nint main(){ return 0; }\n', include=["include"])
    other_o = _compile(tmp_path, "src/other.cpp", "int other(){ return 0; }\n")
    ot_o = _compile(tmp_path, "test/other_test.cpp", "int main(){ return 0; }\n")
    provider = CppProvider(
        tests={"plug_test": [plug_o, pt_o], "other_test": [other_o, ot_o]},
        root=tmp_path,
    )
    view = provider.view()
    assert "src/plug.o" in view.dynamic_units
    # Even a change to something unrelated must still run the dlopen binary,
    # because what it loads is unknowable — the local fail-open.
    sel = select_tests(SelectionInput(
        view, frozenset({"src/other.cpp"}),
        frozenset(view.code_deps) | frozenset(view.test_edges),
    ))
    assert "plug_test" in sel.tests   # always-run
    assert "other_test" in sel.tests  # actually changed
