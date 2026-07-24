"""The Java test-impact provider, held to the equivalence invariant on *real*
compiled bytecode (design: docs/test-impact-selection.md).

Everything here compiles actual Java with `javac` rather than hand-building a
graph: the whole claim being tested is that the class-file constant pool is a
sound over-approximation of what a test can execute, and a hand-built graph
would just restate the claim instead of checking it.
"""

import shutil
import subprocess
import textwrap

import pytest

from brix.jvmabi import class_refs
from brix.testimpact import SelectionInput, select_tests
from brix.testimpact_java import JavaProvider, fqn, scan_classes

needs_jdk = pytest.mark.skipif(
    shutil.which("javac") is None or shutil.which("java") is None,
    reason="no JDK on PATH",
)


def compile_java(tmp_path, sources: dict, out="classes", classpath=None):
    """Compile ``{"com/acme/A.java": "<source>"}`` into ``tmp_path/out``."""
    src = tmp_path / f"src-{out}"
    for rel, text in sources.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())
    classes = tmp_path / out
    classes.mkdir(parents=True, exist_ok=True)
    cmd = ["javac", "-d", str(classes)]
    if classpath:
        cmd += ["-classpath", ":".join(str(p) for p in classpath)]
    cmd += [str(p) for p in sorted(src.rglob("*.java"))]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return classes


# --- the reference extractor -------------------------------------------------


@needs_jdk
def test_refs_include_types_only_reachable_through_a_body(tmp_path):
    """A type used *only* inside a method body is still a runtime dependency —
    it is exactly what the ABI lens drops and this must keep."""
    classes = compile_java(
        tmp_path,
        {
            "com/acme/Helper.java": "package com.acme; public class Helper { public static int n() { return 1; } }",
            "com/acme/User.java": """
                package com.acme;
                public class User {
                    private int hidden() { return Helper.n(); }   // private body
                    public int value() { return hidden(); }
                }
            """,
        },
    )
    refs = class_refs((classes / "com/acme/User.class").read_bytes())
    assert refs.name == "com/acme/User"
    assert "com/acme/Helper" in refs.refs
    assert not refs.reflective
    assert not refs.abstract


@needs_jdk
def test_refs_include_a_parameter_type_never_touched(tmp_path):
    """`void f(Bar b)` binds Bar through the descriptor even with no
    CONSTANT_Class entry — the descriptor scan is what catches it."""
    classes = compile_java(
        tmp_path,
        {
            "com/acme/Bar.java": "package com.acme; public class Bar {}",
            "com/acme/Taker.java": "package com.acme; public class Taker { public void f(Bar b) {} }",
        },
    )
    refs = class_refs((classes / "com/acme/Taker.class").read_bytes())
    assert "com/acme/Bar" in refs.refs


@needs_jdk
def test_reflection_is_detected(tmp_path):
    classes = compile_java(
        tmp_path,
        {
            "com/acme/Loader.java": """
                package com.acme;
                public class Loader {
                    public Object make(String n) throws Exception {
                        return Class.forName(n).getDeclaredConstructor().newInstance();
                    }
                }
            """,
        },
    )
    refs = class_refs((classes / "com/acme/Loader.class").read_bytes())
    assert refs.dynamic
    assert any(r.startswith("java/lang/Class.forName") for r in refs.reflective)


@needs_jdk
def test_lambdas_are_not_reflection(tmp_path):
    """Lambdas route through `java.lang.invoke`, but their targets are ordinary
    method handles in the constant pool. Marking them dynamic would make every
    modern class dynamic and the feature pointless."""
    classes = compile_java(
        tmp_path,
        {
            "com/acme/Lam.java": """
                package com.acme;
                import java.util.List;
                public class Lam {
                    public long count(List<String> xs) {
                        return xs.stream().filter(s -> s.length() > 2).count();
                    }
                }
            """,
        },
    )
    refs = class_refs((classes / "com/acme/Lam.class").read_bytes())
    assert not refs.dynamic, refs.reflective


# --- the provider view -------------------------------------------------------

WORLD = {
    "com/acme/A.java": "package com.acme; public class A { public static int v() { return 1; } }",
    "com/acme/B.java": "package com.acme; public class B { public static int v() { return A.v() + 1; } }",
    "com/acme/C.java": "package com.acme; public class C { public static int v() { return 9; } }",
}

STUB_JUPITER = {
    "org/junit/jupiter/api/Test.java": """
        package org.junit.jupiter.api;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
        public @interface Test {}
    """,
    "org/junit/jupiter/api/Nested.java": """
        package org.junit.jupiter.api;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.TYPE)
        public @interface Nested {}
    """,
}


@pytest.fixture
def world(tmp_path):
    """A/B/C under test (B depends on A) plus a test per class, compiled against
    a minimal stand-in for the Jupiter annotations so the suite needs no
    network or vendored jar."""
    jupiter = compile_java(tmp_path, STUB_JUPITER, out="jupiter")
    code = compile_java(tmp_path, WORLD, out="classes")
    tests = compile_java(
        tmp_path,
        {
            "com/acme/ATest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class ATest { @Test void t() { A.v(); } }
            """,
            "com/acme/BTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class BTest { @Test void t() { B.v(); } }
            """,
            "com/acme/CTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class CTest { @Test void t() { C.v(); } }
            """,
        },
        out="tests",
        classpath=[code, jupiter],
    )
    return JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[jupiter])


def _select(provider, changed):
    view = provider.view()
    known = frozenset(view.code_deps) | frozenset(view.test_edges)
    return select_tests(SelectionInput(view, frozenset(changed), known))


@needs_jdk
def test_view_discovers_tests_and_the_dependency_edge(world):
    view = world.view()
    assert set(view.test_edges) == {"com/acme/ATest", "com/acme/BTest", "com/acme/CTest"}
    assert "com/acme/A" in view.code_deps["com/acme/B"]
    assert "com/acme/A" not in view.code_deps["com/acme/C"]
    assert not view.uncovered
    assert view.binding_unit == "class"


@needs_jdk
def test_editing_a_leaf_selects_only_its_dependents(world):
    sel = _select(world, {"com/acme/A"})
    assert not sel.run_all
    # BTest too: B transitively uses A. CTest provably cannot be affected.
    assert sel.tests == {"com/acme/ATest", "com/acme/BTest"}


@needs_jdk
def test_editing_an_unrelated_class_selects_one_test(world):
    sel = _select(world, {"com/acme/C"})
    assert sel.tests == {"com/acme/CTest"}


@needs_jdk
def test_editing_a_test_runs_that_test(world):
    sel = _select(world, {"com/acme/BTest"})
    assert sel.tests == {"com/acme/BTest"}


@needs_jdk
def test_select_args_are_junit_console_flags(world):
    args = world.select_args({"com/acme/BTest", "com/acme/ATest"})
    assert args == [
        "--select-class",
        "com.acme.ATest",
        "--select-class",
        "com.acme.BTest",
    ]


# --- the escapes: each must degrade, never under-select -----------------------


@needs_jdk
def test_virtual_dispatch_does_not_escape_the_class_closure(tmp_path):
    """The soundness claim for coarse granularity: a test calling through an
    interface still reaches the implementation, because whatever *instantiated*
    it named the concrete class in its own constant pool."""
    jupiter = compile_java(tmp_path, STUB_JUPITER, out="jupiter")
    code = compile_java(
        tmp_path,
        {
            "com/acme/Shape.java": "package com.acme; public interface Shape { int area(); }",
            "com/acme/Square.java": "package com.acme; public class Square implements Shape { public int area() { return 4; } }",
            "com/acme/Shapes.java": "package com.acme; public class Shapes { public static Shape make() { return new Square(); } }",
        },
    )
    tests = compile_java(
        tmp_path,
        {
            "com/acme/ShapeTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class ShapeTest {
                    @Test void t() { Shape s = Shapes.make(); s.area(); }
                }
            """,
        },
        out="tests",
        classpath=[code, jupiter],
    )
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[jupiter])
    # ShapeTest never names Square, but Shapes does — so editing the
    # implementation still selects the test.
    sel = _select(provider, {"com/acme/Square"})
    assert sel.tests == {"com/acme/ShapeTest"}


@needs_jdk
def test_reflective_code_makes_its_tests_always_run(tmp_path):
    """`Class.forName` binds by name at run time, so the closure is no longer a
    superset. The test reaching it must run whatever changed."""
    jupiter = compile_java(tmp_path, STUB_JUPITER, out="jupiter")
    code = compile_java(
        tmp_path,
        {
            "com/acme/Plugin.java": "package com.acme; public class Plugin { public int v() { return 3; } }",
            "com/acme/Registry.java": """
                package com.acme;
                public class Registry {
                    public static Object get(String n) throws Exception {
                        return Class.forName(n).getDeclaredConstructor().newInstance();
                    }
                }
            """,
            "com/acme/Quiet.java": "package com.acme; public class Quiet { public int v() { return 1; } }",
        },
    )
    tests = compile_java(
        tmp_path,
        {
            "com/acme/RegistryTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class RegistryTest {
                    @Test void t() throws Exception { Registry.get("com.acme.Plugin"); }
                }
            """,
            "com/acme/QuietTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class QuietTest { @Test void t() { new Quiet().v(); } }
            """,
        },
        out="tests",
        classpath=[code, jupiter],
    )
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[jupiter])
    view = provider.view()
    assert "com/acme/Registry" in view.dynamic_units

    # Plugin is reached ONLY by name — invisible to the graph — yet RegistryTest
    # still runs. And it runs even for a wholly unrelated edit.
    for changed in ({"com/acme/Plugin"}, {"com/acme/Quiet"}):
        sel = _select(provider, changed)
        assert "com/acme/RegistryTest" in sel.tests, changed
    # The degradation is local: QuietTest is not dragged along by Registry.
    assert "com/acme/QuietTest" not in _select(provider, {"com/acme/Plugin"}).tests


@needs_jdk
def test_unresolvable_reference_degrades_locally(tmp_path):
    """A class referring to something outside the classpath we were given might
    be a framework that reflects back into the code under test. Treat it like
    reflection: its tests always run, everyone else still selects."""
    jupiter = compile_java(tmp_path, STUB_JUPITER, out="jupiter")
    lib = compile_java(
        tmp_path, {"ext/Ext.java": "package ext; public class Ext { public static int v() { return 7; } }"}, out="lib"
    )
    code = compile_java(
        tmp_path,
        {
            "com/acme/UsesExt.java": "package com.acme; public class UsesExt { public int v() { return ext.Ext.v(); } }",
            "com/acme/Plain.java": "package com.acme; public class Plain { public int v() { return 2; } }",
        },
        classpath=[lib],
    )
    tests = compile_java(
        tmp_path,
        {
            "com/acme/ExtTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class ExtTest { @Test void t() { new UsesExt().v(); } }
            """,
            "com/acme/PlainTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class PlainTest { @Test void t() { new Plain().v(); } }
            """,
        },
        out="tests",
        classpath=[code, lib, jupiter],
    )
    # Deliberately omit `lib` from the classpath we hand the provider.
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[jupiter])
    view = provider.view()
    assert "com/acme/UsesExt" in view.dynamic_units
    sel = _select(provider, {"com/acme/Plain"})
    assert "com/acme/ExtTest" in sel.tests  # always-run, unknowable deps
    assert "com/acme/PlainTest" in sel.tests
    # ...and with `lib` supplied, the same code selects normally again.
    complete = JavaProvider(test_roots=[tests], classpath=[code, lib], harness_roots=[jupiter])
    assert "com/acme/UsesExt" not in complete.view().dynamic_units
    assert _select(complete, {"com/acme/Plain"}).tests == {"com/acme/PlainTest"}


@needs_jdk
def test_the_harness_is_a_role_not_a_name(tmp_path):
    """The runner is the test's *caller*, not its dependency: it reflects to
    find a class whose name brix supplied. Walking into its internals follows
    reference edges that are not call edges, and would mark every test dynamic.

    The designation attaches to the artifact brix launched with — pass it and
    selection works; withhold it and the same bytes are ordinary code, so its
    reflection makes everything dynamic. Nothing keys off the package name."""
    runner = compile_java(
        tmp_path,
        {
            **STUB_JUPITER,
            # A launcher that loads test classes by name, as a real one does.
            "org/junit/platform/Launcher.java": """
                package org.junit.platform;
                public class Launcher {
                    public static Object load(String n) throws Exception {
                        return Class.forName(n).getDeclaredConstructor().newInstance();
                    }
                }
                """,
            # The assertion facade a test legitimately calls into, sharing a
            # package with the launcher — this is the edge the walk crosses.
            "org/junit/jupiter/api/Assertions.java": """
                package org.junit.jupiter.api;
                public class Assertions {
                    public static void check(boolean b) {
                        if (!b) throw new AssertionError(org.junit.platform.Launcher.class.getName());
                    }
                }
                """,
        },
        out="runner",
    )
    code = compile_java(tmp_path, WORLD)
    tests = compile_java(
        tmp_path,
        {
            "com/acme/ATest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                import org.junit.jupiter.api.Assertions;
                public class ATest { @Test void t() { Assertions.check(A.v() == 1); } }
            """,
            "com/acme/CTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                import org.junit.jupiter.api.Assertions;
                public class CTest { @Test void t() { Assertions.check(C.v() == 9); } }
            """,
        },
        out="tests",
        classpath=[code, runner],
    )
    designated = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[runner])
    assert _select(designated, {"com/acme/C"}).tests == {"com/acme/CTest"}

    # Same bytes, no designation: the walk reaches the launcher's reflection
    # through Assertions and every test becomes always-run. Safe, useless.
    undesignated = JavaProvider(test_roots=[tests], classpath=[code, runner])
    sel = _select(undesignated, {"com/acme/C"})
    assert sel.tests == {"com/acme/ATest", "com/acme/CTest"}
    assert sel.always_run == {"com/acme/ATest", "com/acme/CTest"}


STUB_METHOD_SOURCE = {
    "org/junit/jupiter/params/ParameterizedTest.java": """
        package org.junit.jupiter.params;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
        public @interface ParameterizedTest {}
    """,
    "org/junit/jupiter/params/provider/MethodSource.java": """
        package org.junit.jupiter.params.provider;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
        public @interface MethodSource { String[] value() default {}; }
    """,
}


@needs_jdk
def test_a_string_named_data_provider_becomes_a_real_edge(tmp_path):
    """`@MethodSource("com.acme.Providers#args")` makes the test's result depend
    on `Providers` with no bytecode reference to it — the class closure alone
    would silently skip this test when the provider changes."""
    runner = compile_java(tmp_path, {**STUB_JUPITER, **STUB_METHOD_SOURCE}, out="runner")
    code = compile_java(
        tmp_path,
        {
            **WORLD,
            "com/acme/Providers.java": """
                package com.acme;
                import java.util.stream.Stream;
                public class Providers { public static Stream<Object> args() { return Stream.of(1); } }
            """,
        },
    )
    tests = compile_java(
        tmp_path,
        {
            "com/acme/ParamTest.java": """
                package com.acme;
                import org.junit.jupiter.params.ParameterizedTest;
                import org.junit.jupiter.params.provider.MethodSource;
                public class ParamTest {
                    @ParameterizedTest
                    @MethodSource("com.acme.Providers#args")
                    void t(Object o) { C.v(); }
                }
            """,
            "com/acme/CTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class CTest { @Test void t() { C.v(); } }
            """,
        },
        out="tests",
        classpath=[code, runner],
    )
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[runner])
    view = provider.view()
    assert "com/acme/Providers" in view.code_deps["com/acme/ParamTest"]
    # Editing the data provider selects the parameterized test — and only it.
    sel = _select(provider, {"com/acme/Providers"})
    assert sel.tests == {"com/acme/ParamTest"}
    assert "com/acme/ParamTest" not in view.dynamic_units  # resolved, so provable


@needs_jdk
def test_an_unresolvable_string_named_provider_fails_open(tmp_path):
    """If the named code is not on the classpath we were given, we cannot prove
    anything about it — that test runs every time."""
    runner = compile_java(tmp_path, {**STUB_JUPITER, **STUB_METHOD_SOURCE}, out="runner")
    code = compile_java(tmp_path, WORLD)
    tests = compile_java(
        tmp_path,
        {
            "com/acme/ParamTest.java": """
                package com.acme;
                import org.junit.jupiter.params.ParameterizedTest;
                import org.junit.jupiter.params.provider.MethodSource;
                public class ParamTest {
                    @ParameterizedTest
                    @MethodSource("com.elsewhere.Unknown#args")
                    void t(Object o) { C.v(); }
                }
            """,
            "com/acme/CTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public class CTest { @Test void t() { C.v(); } }
            """,
        },
        out="tests",
        classpath=[code, runner],
    )
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[runner])
    assert "com/acme/ParamTest" in provider.view().dynamic_units
    sel = _select(provider, {"com/acme/A"})  # nothing to do with either test
    assert sel.tests == {"com/acme/ParamTest"}
    assert sel.always_run == {"com/acme/ParamTest"}


@needs_jdk
def test_inherited_tests_are_discovered(tmp_path):
    """`class FooTest extends AbstractContractTest` declares no annotation of
    its own; JUnit still runs it. Missing this would silently skip a real
    test — the exact failure the invariant forbids."""
    jupiter = compile_java(tmp_path, STUB_JUPITER, out="jupiter")
    code = compile_java(tmp_path, {"com/acme/A.java": WORLD["com/acme/A.java"]})
    tests = compile_java(
        tmp_path,
        {
            "com/acme/AbstractContractTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Test;
                public abstract class AbstractContractTest { @Test void contract() { A.v(); } }
            """,
            "com/acme/InheritedTest.java": """
                package com.acme;
                public class InheritedTest extends AbstractContractTest {}
            """,
        },
        out="tests",
        classpath=[code, jupiter],
    )
    view = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[jupiter]).view()
    # The concrete subclass is the unit; the abstract base is not runnable.
    assert set(view.test_edges) == {"com/acme/InheritedTest"}


@needs_jdk
def test_nested_tests_group_under_the_enclosing_class(tmp_path):
    """`--select-class Outer` runs `@Nested` inner classes, so they must be one
    unit — selecting `Outer$Inner` would address nothing."""
    jupiter = compile_java(tmp_path, STUB_JUPITER, out="jupiter")
    code = compile_java(tmp_path, WORLD)
    tests = compile_java(
        tmp_path,
        {
            "com/acme/OuterTest.java": """
                package com.acme;
                import org.junit.jupiter.api.Nested;
                import org.junit.jupiter.api.Test;
                public class OuterTest {
                    @Nested class Inner { @Test void t() { C.v(); } }
                }
            """,
        },
        out="tests",
        classpath=[code, jupiter],
    )
    provider = JavaProvider(test_roots=[tests], classpath=[code], harness_roots=[jupiter])
    view = provider.view()
    assert set(view.test_edges) == {"com/acme/OuterTest"}
    # The nested class's dependency counts as the group's.
    sel = _select(provider, {"com/acme/C"})
    assert sel.tests == {"com/acme/OuterTest"}
    assert provider.select_args(sel.tests) == ["--select-class", "com.acme.OuterTest"]


@needs_jdk
def test_unreadable_classpath_entry_forces_a_full_run(tmp_path, world):
    bogus = tmp_path / "broken.jar"
    bogus.write_bytes(b"not a jar")
    provider = JavaProvider(
        test_roots=world.test_roots,
        classpath=[*world.classpath, bogus],
        harness_roots=world.harness_roots,
    )
    view = provider.view()
    assert view.uncovered
    sel = _select(provider, {"com/acme/C"})
    assert sel.run_all
    assert sel.tests == view.all_tests


@needs_jdk
def test_scan_reads_jars(tmp_path, world):
    """Real classpaths are jars, not loose directories."""
    import zipfile

    code_dir = tmp_path / "classes"
    jar = tmp_path / "code.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        for cls in sorted(code_dir.rglob("*.class")):
            zf.write(cls, cls.relative_to(code_dir).as_posix())
    scan = scan_classes([jar])
    assert {"com/acme/A", "com/acme/B", "com/acme/C"} <= set(scan.classes)
    assert not scan.unreadable


def test_fqn_maps_internal_names_to_java_names():
    assert fqn("com/acme/Outer$Inner") == "com.acme.Outer$Inner"
