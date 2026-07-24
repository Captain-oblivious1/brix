"""The equivalence oracle for the Java provider, run against a **real JUnit**
(design: docs/test-impact-selection.md, "The equivalence oracle").

`test_testimpact.py` proves the core's algebra against a synthetic world;
`test_testimpact_java.py` proves the provider reads real bytecode correctly.
Neither actually runs a test. This does: it edits code, runs the suite for
real, and asserts the governing invariant end to end —

    **every test that fails in the full run is in the selected set.**

That is the only property that matters. Over-selection is free; a test that
fails in the full run and is not selected is the failure mode the whole design
exists to make impossible.

Needs a `junit-platform-console-standalone` jar (the runner brix's
`JUnitTestAction` already drives). Point `BRIX_JUNIT_CONSOLE_JAR` at one, or
drop it in a `lib/` directory under ~/Projects/brix_test; otherwise skipped.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from brix.testimpact import SelectionInput, select_tests
from brix.testimpact_java import JavaProvider, fqn

from test_testimpact_java import compile_java


def _console_jar():
    env = os.environ.get("BRIX_JUNIT_CONSOLE_JAR")
    if env and Path(env).is_file():
        return Path(env)
    found = sorted(
        Path.home().glob("Projects/brix_test/*/lib/junit-platform-console-standalone*.jar")
    )
    return found[-1] if found else None


CONSOLE_JAR = _console_jar()

needs_junit = pytest.mark.skipif(
    CONSOLE_JAR is None or shutil.which("javac") is None or shutil.which("java") is None,
    reason="no JDK + junit-platform-console-standalone jar (set BRIX_JUNIT_CONSOLE_JAR)",
)


# A/B/C with a real dependency edge (B calls A) and one assertion per class, so
# a changed return value actually turns a test red.
def _sources(a_value: int, b_bonus: int, c_value: int, p_value: int) -> dict:
    return {
        "com/acme/A.java": f"package com.acme; public class A {{ public static int v() {{ return {a_value}; }} }}",
        "com/acme/B.java": f"package com.acme; public class B {{ public static int v() {{ return A.v() + {b_bonus}; }} }}",
        "com/acme/C.java": f"package com.acme; public class C {{ public static int v() {{ return {c_value}; }} }}",
        # Reached only by the string in ParamTest's @MethodSource annotation —
        # nothing in any test's bytecode references this class.
        "com/acme/Providers.java": f"""
            package com.acme;
            import java.util.stream.Stream;
            public class Providers {{
                static Stream<Integer> args() {{ return Stream.of({p_value}); }}
            }}
        """,
    }


TESTS = {
    "com/acme/ATest.java": """
        package com.acme;
        import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class ATest { @Test void v() { assertEquals(1, A.v()); } }
    """,
    "com/acme/BTest.java": """
        package com.acme;
        import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class BTest { @Test void v() { assertEquals(2, B.v()); } }
    """,
    "com/acme/CTest.java": """
        package com.acme;
        import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class CTest { @Test void v() { assertEquals(9, C.v()); } }
    """,
    # The escape: this test's data comes from a class named only by a string.
    # Its compiled bytecode contains no reference to `Providers` at all.
    "com/acme/ParamTest.java": """
        package com.acme;
        import org.junit.jupiter.params.ParameterizedTest;
        import org.junit.jupiter.params.provider.MethodSource;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class ParamTest {
            @ParameterizedTest
            @MethodSource("com.acme.Providers#args")
            void v(int given) { assertEquals(3, given); }
        }
    """,
}

BASELINE = dict(a_value=1, b_bonus=1, c_value=9, p_value=3)  # all green


def _run(tests_dir, code_dir, select_args) -> bool:
    """Run the console launcher; True when the selection passed."""
    proc = subprocess.run(
        [
            "java",
            "-jar",
            str(CONSOLE_JAR),
            "execute",
            "--disable-banner",
            "--details=none",
            "--fail-if-no-tests",
            "--class-path",
            str(tests_dir),
            "--class-path",
            str(code_dir),
            *select_args,
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _verdicts(tests_dir, code_dir, units) -> set:
    """The full run's failing set. Running each test class separately is the
    same suite — these classes share no state — and gives a per-class verdict
    without parsing the launcher's report."""
    return {u for u in units if not _run(tests_dir, code_dir, ["--select-class", fqn(u)])}


@pytest.fixture(scope="module")
def suite(tmp_path_factory):
    """Compile the baseline world once; edits recompile only the code."""
    tmp_path = tmp_path_factory.mktemp("oracle")
    code = compile_java(tmp_path, _sources(**BASELINE), out="classes")
    tests = compile_java(
        tmp_path, TESTS, out="tests", classpath=[code, CONSOLE_JAR]
    )
    return tmp_path, code, tests


@needs_junit
def test_baseline_is_green(suite):
    """Guard the oracle itself: if the baseline is not green, a later
    'no failures' result would prove nothing."""
    tmp_path, code, tests = suite
    units = {"com/acme/ATest", "com/acme/BTest", "com/acme/CTest", "com/acme/ParamTest"}
    assert _verdicts(tests, code, units) == set()


@needs_junit
@pytest.mark.parametrize(
    "unit,edit",
    [
        # Edit A: ATest fails outright, and BTest fails too because B is
        # computed from A — the transitive case selection must not miss.
        ("com/acme/A", dict(a_value=5)),
        ("com/acme/B", dict(b_bonus=7)),
        ("com/acme/C", dict(c_value=0)),
        # The string-named data provider. No test's bytecode mentions it, so
        # the class closure alone would skip ParamTest and report a false pass.
        ("com/acme/Providers", dict(p_value=4)),
    ],
)
def test_selected_run_matches_the_full_run(suite, unit, edit):
    tmp_path, code, tests = suite
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[CONSOLE_JAR])
    view = provider.view()
    known = frozenset(view.code_deps) | frozenset(view.test_edges)
    selection = select_tests(SelectionInput(view, frozenset({unit}), known))

    # Apply the edit for real, then ask JUnit what the FULL suite thinks.
    compile_java(tmp_path, _sources(**{**BASELINE, **edit}), out="classes")
    try:
        failing = _verdicts(tests, code, view.all_tests)
        assert failing, f"editing {unit} did not turn anything red — oracle is toothless"
        missed = failing - selection.tests
        assert not missed, (
            f"UNDER-SELECTION editing {unit}: the full run fails {sorted(missed)} "
            f"but selection ran {sorted(selection.tests)}"
        )
        # And the selected run really does reproduce the failure.
        assert not _run(tests, code, provider.select_args(selection.tests))
    finally:
        compile_java(tmp_path, _sources(**BASELINE), out="classes")


@needs_junit
def test_selection_actually_skips_work(suite):
    """The other half of the deal: equivalence is trivial if we always run
    everything. Editing C must leave A's and B's tests unselected."""
    tmp_path, code, tests = suite
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[CONSOLE_JAR])
    view = provider.view()
    known = frozenset(view.code_deps) | frozenset(view.test_edges)
    selection = select_tests(SelectionInput(view, frozenset({"com/acme/C"}), known))
    assert not selection.run_all
    assert selection.tests == {"com/acme/CTest"}
