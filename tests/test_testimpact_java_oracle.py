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
def _sources(
    a_value: int, b_bonus: int, c_value: int, p_value: int, tool_value: int, xml_value: int
) -> dict:
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
        # Auto-detected JUnit extension, registered ONLY in META-INF/services.
        # It changes what tests observe, yet no test's bytecode names it and
        # the code that loads it is the harness — which the direction rule
        # deliberately excludes. Neither the reflection rule nor the harness
        # boundary can catch this; only the name-addressed fail-open can.
        "com/acme/Stamp.java": f"""
            package com.acme;
            import org.junit.jupiter.api.extension.BeforeEachCallback;
            import org.junit.jupiter.api.extension.ExtensionContext;
            public class Stamp implements BeforeEachCallback {{
                public void beforeEach(ExtensionContext ctx) {{
                    System.setProperty("stamp", "{tool_value}");
                }}
            }}
        """,
        # Named ONLY by a JVM system property on the run command. No file and
        # no bytecode mentions it; the platform's factory finder loads it.
        "com/acme/MyFactory.java": f"""
            package com.acme;
            import javax.xml.parsers.DocumentBuilder;
            import javax.xml.parsers.DocumentBuilderFactory;
            public class MyFactory extends DocumentBuilderFactory {{
                public MyFactory() {{ System.setProperty("xmlstamp", "{xml_value}"); }}
                public DocumentBuilder newDocumentBuilder() {{ return null; }}
                public void setAttribute(String n, Object v) {{}}
                public Object getAttribute(String n) {{ return null; }}
                public void setFeature(String n, boolean v) {{}}
                public boolean getFeature(String n) {{ return false; }}
            }}
        """,
    }


# The setting that names `MyFactory`. Passed to the JVM, and handed to the
# provider as `runtime_values` — the third and last source a name can come from.
XML_PROPERTY = "-Djavax.xml.parsers.DocumentBuilderFactory=com.acme.MyFactory"


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
    # Observes what the auto-detected extension did. Its bytecode references
    # nothing but `System` — there is no path from this test to `Stamp`.
    "com/acme/StampTest.java": """
        package com.acme;
        import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class StampTest {
            @Test void v() { assertEquals("7", System.getProperty("stamp")); }
        }
    """,
    # Observes what the property-named factory did. References only platform
    # types; nothing connects this test to `MyFactory`.
    "com/acme/XmlTest.java": """
        package com.acme;
        import javax.xml.parsers.DocumentBuilderFactory;
        import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class XmlTest {
            @Test void v() {
                DocumentBuilderFactory.newInstance();
                assertEquals("7", System.getProperty("xmlstamp"));
            }
        }
    """,
}

BASELINE = dict(  # all green
    a_value=1, b_bonus=1, c_value=9, p_value=3, tool_value=7, xml_value=7
)


def _run(tests_dir, code_dir, select_args) -> bool:
    """Run the console launcher; True when the selection passed."""
    proc = subprocess.run(
        [
            "java",
            XML_PROPERTY,
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
    code = compile_java(tmp_path, _sources(**BASELINE), out="classes", classpath=[CONSOLE_JAR])
    services = code / "META-INF/services"
    services.mkdir(parents=True, exist_ok=True)
    (services / "org.junit.jupiter.api.extension.Extension").write_text("com.acme.Stamp\n")
    (code / "junit-platform.properties").write_text(
        "junit.jupiter.extensions.autodetection.enabled=true\n"
    )
    tests = compile_java(
        tmp_path, TESTS, out="tests", classpath=[code, CONSOLE_JAR]
    )
    return tmp_path, code, tests


@needs_junit
def test_baseline_is_green(suite):
    """Guard the oracle itself: if the baseline is not green, a later
    'no failures' result would prove nothing."""
    tmp_path, code, tests = suite
    units = {
        "com/acme/ATest",
        "com/acme/BTest",
        "com/acme/CTest",
        "com/acme/ParamTest",
        "com/acme/StampTest",
        "com/acme/XmlTest",
    }
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
        # The service-registered extension. Nothing names it in any bytecode,
        # and its loader is the harness — which direction deliberately
        # excludes. Only the name-addressed fail-open catches this one.
        ("com/acme/Stamp", dict(tool_value=8)),
        # Named only by a JVM setting — the third name source. Caught only
        # because `runtime_values` is scanned.
        ("com/acme/MyFactory", dict(xml_value=8)),
    ],
)
def test_selected_run_matches_the_full_run(suite, unit, edit):
    tmp_path, code, tests = suite
    provider = JavaProvider(
        test_roots=[tests],
        classpath=[code],
        harness_roots=[CONSOLE_JAR],
        runtime_values=[XML_PROPERTY],
    )
    view = provider.view()
    known = frozenset(view.code_deps) | frozenset(view.test_edges)
    selection = select_tests(SelectionInput(view, frozenset({unit}), known))

    # Apply the edit for real, then ask JUnit what the FULL suite thinks.
    compile_java(tmp_path, _sources(**{**BASELINE, **edit}), out="classes", classpath=[CONSOLE_JAR])
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
        compile_java(tmp_path, _sources(**BASELINE), out="classes", classpath=[CONSOLE_JAR])


@needs_junit
def test_selection_actually_skips_work(suite):
    """The other half of the deal: equivalence is trivial if we always run
    everything. Editing C must leave A's and B's tests unselected."""
    tmp_path, code, tests = suite
    provider = JavaProvider(
        test_roots=[tests],
        classpath=[code],
        harness_roots=[CONSOLE_JAR],
        runtime_values=[XML_PROPERTY],
    )
    view = provider.view()
    known = frozenset(view.code_deps) | frozenset(view.test_edges)
    selection = select_tests(SelectionInput(view, frozenset({"com/acme/C"}), known))
    assert not selection.run_all
    assert selection.tests == {"com/acme/CTest"}


# --- the cache-equivalence oracle --------------------------------------------
#
# The single-edit oracle above proves each selection is equivalent to a full
# run *in isolation*. Selection in a real build is not isolated: it runs over a
# sequence of edits against warm state (the `known` set grows, `prior_failed`
# carries), and skips accumulate. The danger this introduces is a *stale pass* —
# a test skipped across several edits because each edit alone looked unrelated,
# while a fresh full run would now fail it.
#
# This oracle drives that sequence for real. It maintains a `believed` verdict
# for every test — green until a *selected* run says otherwise, and never
# refreshed for a skipped test — exactly the memory a warm build carries. After
# each edit it compares `believed` against a from-scratch full run of the whole
# suite. Equivalence is: what the build believes without re-running == what
# reality says. A wrongful skip shows up as believed-green where reality is red.

# Which source parameter names which unit, so an edit maps to a changed unit.
_PARAM_UNIT = {
    "a_value": "com/acme/A",
    "b_bonus": "com/acme/B",
    "c_value": "com/acme/C",
    "p_value": "com/acme/Providers",
    "tool_value": "com/acme/Stamp",
    "xml_value": "com/acme/MyFactory",
}

# A deliberately adversarial sequence: a clean skip (the case a stale pass would
# corrupt), a prior-failure that must force a full rerun until green, and edits
# to each of the three name-addressed units (the string-named provider, the
# service-registered extension, the property-named factory) whose changes must
# fan out to a full run.
_SEQUENCE = [
    ("cold build", {}),
    ("break A (drags B via A->B)", {"a_value": 5}),
    ("edit C while A still broken", {"a_value": 5, "c_value": 0}),
    ("fix everything", {}),
    ("edit C only — A/B must stay skipped-green", {"c_value": 0}),
    ("fix", {}),
    ("edit the string-named provider", {"p_value": 4}),
    ("fix", {}),
    ("edit the service-registered extension", {"tool_value": 8}),
    ("fix", {}),
    ("edit the property-named factory", {"xml_value": 8}),
]


@needs_junit
def test_a_warm_sequence_of_selections_matches_from_scratch(suite):
    tmp_path, code, tests = suite
    provider = JavaProvider(
        test_roots=[tests],
        classpath=[code],
        harness_roots=[CONSOLE_JAR],
        runtime_values=[XML_PROPERTY],
    )
    all_tests = provider.view().all_tests

    believed: dict[str, bool] = {}  # test -> is-red, as the warm build remembers it
    known: frozenset[str] = frozenset()  # units the last selection knew about
    prior_failed = False
    prev_params = dict(BASELINE)

    try:
        for label, edit in _SEQUENCE:
            params = {**BASELINE, **edit}
            changed = frozenset(
                _PARAM_UNIT[k] for k in _PARAM_UNIT if params[k] != prev_params[k]
            )
            compile_java(tmp_path, _sources(**params), out="classes", classpath=[CONSOLE_JAR])

            view = provider.view()
            selection = select_tests(
                SelectionInput(view, changed, known, prior_failed=prior_failed)
            )

            # Run only what selection chose, and update memory only for those —
            # a skipped test keeps whatever verdict it last had.
            ran_failing = _verdicts(tests, code, selection.tests)
            for t in selection.tests:
                believed[t] = t in ran_failing
            prior_failed = bool(ran_failing)
            known = frozenset(view.code_deps) | frozenset(view.test_edges)

            # Ground truth: what a full from-scratch run says right now.
            fresh_failing = _verdicts(tests, code, all_tests)
            believed_failing = {t for t, red in believed.items() if red}
            assert believed_failing == fresh_failing, (
                f"after '{label}': the warm build believes {sorted(believed_failing)} "
                f"are failing, but a from-scratch run fails {sorted(fresh_failing)} "
                f"(selection ran {sorted(selection.tests)}, run_all={selection.run_all})"
            )
            prev_params = params
    finally:
        compile_java(tmp_path, _sources(**BASELINE), out="classes", classpath=[CONSOLE_JAR])
