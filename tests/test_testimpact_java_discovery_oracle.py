"""Equivalence oracle for Java test selection **by runtime discovery**.

This exercises the shipped path (runtime discovery): a real brix build whose
`JUnitTestAction` fans out to one discovery-wrapped run per test class. It edits
real code, runs a real brix build, observes which per-class runs actually
executed (vs replayed), and asserts the non-negotiable invariant:

    every test that FAILS in a full run is re-run by discovery — no under-selection.

The teeth: a control edit that cannot affect a test must let that test **replay**
(else "selection" that re-runs everything would pass this trivially). And the
escape cases the static provider had to *fail open* on — a `@MethodSource`
provider named only by a string, a class reached only by reflection — are caught
here **for free**, because the JVM actually loads them and the recorder sees it.

Needs a JDK and a `junit-platform-console-standalone` jar
(`BRIX_JUNIT_CONSOLE_JAR`, or under `~/Projects/brix_test/*/lib/`).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brix import Graph, IncrementalBuilder, Reporter, java  # noqa: E402


def _console_jar():
    env = os.environ.get("BRIX_JUNIT_CONSOLE_JAR")
    if env and Path(env).is_file():
        return env
    found = sorted(
        Path.home().glob("Projects/brix_test/*/lib/junit-platform-console-standalone*.jar")
    )
    return str(found[-1]) if found else None


CONSOLE = _console_jar()
needs_junit = pytest.mark.skipif(
    CONSOLE is None or shutil.which("javac") is None or shutil.which("java") is None,
    reason="no JDK + junit-platform-console-standalone jar (set BRIX_JUNIT_CONSOLE_JAR)",
)


def _code(a: int, b_bonus: int, c: int, prov: int, r: int) -> dict[str, str]:
    return {
        "src/com/acme/A.java": f"package com.acme; public class A {{ public static int v() {{ return {a}; }} }}",
        "src/com/acme/B.java": f"package com.acme; public class B {{ public static int v() {{ return A.v() + {b_bonus}; }} }}",
        "src/com/acme/C.java": f"package com.acme; public class C {{ public static int v() {{ return {c}; }} }}",
        # Named ONLY by ParamTest's @MethodSource string — no bytecode reference
        # anywhere. The static provider needed a name-addressed fail-open for
        # this; discovery just sees the JVM load it.
        "src/com/acme/Providers.java": (
            "package com.acme; import java.util.stream.Stream;"
            f" public class Providers {{ static Stream<Integer> args() {{ return Stream.of({prov}); }} }}"
        ),
        # Reached only by reflection from RTest — no static reference.
        "src/com/acme/R.java": (
            "package com.acme; public class R {"
            f" public static int v() {{ return {r}; }} }}"
        ),
    }


TESTS = {
    "test/com/acme/ATest.java": """
        package com.acme; import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class ATest { @Test void v() { assertEquals(1, A.v()); } }
    """,
    "test/com/acme/BTest.java": """
        package com.acme; import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class BTest { @Test void v() { assertEquals(2, B.v()); } }
    """,
    "test/com/acme/CTest.java": """
        package com.acme; import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class CTest { @Test void v() { assertEquals(9, C.v()); } }
    """,
    "test/com/acme/ParamTest.java": """
        package com.acme;
        import org.junit.jupiter.params.ParameterizedTest;
        import org.junit.jupiter.params.provider.MethodSource;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class ParamTest {
            @ParameterizedTest @MethodSource("com.acme.Providers#args")
            void v(int given) { assertEquals(3, given); }
        }
    """,
    "test/com/acme/RTest.java": """
        package com.acme; import org.junit.jupiter.api.Test;
        import static org.junit.jupiter.api.Assertions.assertEquals;
        public class RTest {
            @Test void v() throws Exception {
                int got = (int) Class.forName("com.acme.R").getMethod("v").invoke(null);
                assertEquals(9, got);
            }
        }
    """,
}

BASELINE = dict(a=1, b_bonus=1, c=9, prov=3, r=9)
ALL_TESTS = {"ATest", "BTest", "CTest", "ParamTest", "RTest"}
LIB_CLASSES = "build/classes/lib"
TEST_CLASSES = "build/test-classes/lib"
OPTIONS = ["--release", "17", "-encoding", "UTF-8"]


class _Ran(Reporter):
    """Captures the test units whose per-class run actually executed."""

    def __init__(self) -> None:
        self.ran: set[str] = set()

    def process_started(self, node) -> None:
        label = node.label
        marker = "run lib tests com/acme/"
        if marker in label:
            self.ran.add(label.split(marker, 1)[1].strip())


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _graph_factory(root: Path):
    def create_graph() -> Graph:
        graph = Graph()
        d = graph.data
        worker = java.javac_worker(graph)
        graph.add(
            java.JarAction(
                source_files=d.glob("src/com/acme/{name}.java"),
                output_file=d.file("build/libs/lib.jar"),
                classes_dir=LIB_CLASSES,
                javac_worker=worker,
                options=OPTIONS,
            )
        )
        graph.add(
            java.JUnitTestAction(
                name="lib tests",
                test_sources=d.glob("test/com/acme/{name}.java"),
                classpath=[d.file(LIB_CLASSES)],
                console_jar=CONSOLE,
                test_classes_dir=TEST_CLASSES,
                javac_worker=worker,
                options=OPTIONS,
            )
        )
        return graph

    return create_graph


def _build(root: Path) -> set[str]:
    """Run one brix build; return the set of test units that actually ran."""
    ran = _Ran()
    IncrementalBuilder(_graph_factory(root), root=root, metadata_dir="build/.brix").build(
        reporter=ran
    )
    return ran.ran


def _full_run_failures(root: Path) -> set[str]:
    """Independent full-run verdict: run each compiled test class standalone and
    collect the failing ones. The classes share no state, so per-class == suite."""
    failing: set[str] = set()
    for unit in ALL_TESTS:
        proc = subprocess.run(
            [
                "java", "-jar", CONSOLE, "execute", "--disable-banner",
                "--details=none", "--fail-if-no-tests",
                "--select-class", f"com.acme.{unit}",
                "--class-path", str(root / TEST_CLASSES),
                "--class-path", str(root / LIB_CLASSES),
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            failing.add(unit)
    return failing


@pytest.fixture
def workspace(tmp_path):
    _write(tmp_path, {**_code(**BASELINE), **TESTS})
    return tmp_path


@needs_junit
def test_cold_build_runs_every_test(workspace):
    assert _build(workspace) == ALL_TESTS
    # A no-op rebuild replays everything.
    assert _build(workspace) == set()


@needs_junit
@pytest.mark.parametrize(
    "edit,expect_fail",
    [
        # Break A: ATest (asserts A) and BTest (B calls A) must fail.
        (dict(a=2), {"ATest", "BTest"}),
        # Break C: only CTest.
        (dict(c=8), {"CTest"}),
        # Break the @MethodSource provider — no test's bytecode names it, yet
        # ParamTest loads it at run time, so discovery must re-run ParamTest.
        (dict(prov=4), {"ParamTest"}),
    ],
)
def test_selected_run_never_under_selects(workspace, edit, expect_fail):
    _build(workspace)  # warm: establish depfiles
    _write(workspace, _code(**{**BASELINE, **edit}))
    ran = _build(workspace)
    failures = _full_run_failures(workspace)
    assert failures == expect_fail, f"oracle setup wrong: {failures} != {expect_fail}"
    # THE INVARIANT: every failing test was re-run (no stale green).
    assert failures <= ran, f"UNDER-SELECTION: failing={failures} not all re-run={ran}"


@needs_junit
def test_reflection_dependency_is_discovered(workspace):
    """R is reached only via Class.forName in RTest — no static edge. Discovery
    sees the load, so breaking R re-runs RTest (the static provider would have
    had to mark RTest dynamic and always run it)."""
    _build(workspace)
    _write(workspace, _code(**{**BASELINE, "r": 8}))  # R.v() returns r
    ran = _build(workspace)
    assert "RTest" in ran  # discovered through reflection


@needs_junit
def test_unrelated_edit_lets_provably_safe_tests_replay(workspace):
    """The teeth: editing C must NOT re-run ATest/BTest — they never load C.
    Without this, a 'selector' that re-runs everything would pass silently."""
    _build(workspace)
    _write(workspace, _code(**{**BASELINE, "c": 8}))
    ran = _build(workspace)
    assert "CTest" in ran
    assert "ATest" not in ran and "BTest" not in ran
