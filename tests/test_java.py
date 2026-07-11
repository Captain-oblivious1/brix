"""brix.java typed actions: lowering shape, and e2e when a JDK exists."""

import shutil

import pytest

from brix import ExternalProcessAction, Graph, java
from brix.data import DataFactory

data = DataFactory()


def _jar_action(**overrides):
    kwargs = dict(
        source_files=data.glob("src/main/java/{cls}.java"),
        output_file=data.file("build/libs/app.jar"),
    )
    kwargs.update(overrides)
    return java.JarAction(**kwargs)


def test_jar_lowers_to_javac_then_jar():
    graph = Graph()
    graph.add(_jar_action())
    compile_java, package = graph.actions
    assert isinstance(compile_java, ExternalProcessAction)
    assert compile_java.name == "javac -> build/classes"
    assert compile_java.inputs["javac"].name == "javac"
    # The classes dir is an output declaration: it stages under atomic.
    assert compile_java.outputs["classes_dir"].path == "build/classes"
    # Bare {name} is recursive, so packaged classes (com/foo/Bar.class) are tracked too.
    assert compile_java.outputs["classes"].pattern.template == "build/classes/{name}.class"

    assert package.name == "jar build/libs/app.jar"
    assert "cf {output}" in package.command_line
    # ... and an input declaration on the jar side: resolved via the overlay.
    assert package.inputs["classes_dir"].path == "build/classes"
    assert package.outputs["output"].path == "build/libs/app.jar"


def test_entry_point_uses_cfe():
    graph = Graph()
    graph.add(_jar_action(entry_point="App"))
    _, package = graph.actions
    assert "cfe {output} {entry_point}" in package.command_line
    assert package.inputs["entry_point"].text == "App"


def test_resource_dir_adds_second_jar_root():
    graph = Graph()
    graph.add(_jar_action(resource_dir="src/main/resources"))
    _, package = graph.actions
    # A second `-C <root> .` after the classes root, and the resource tree is
    # a jar-step input (an edited resource re-jars).
    assert package.command_line.count("-C ") == 2
    assert "-C {resource_dir} ." in package.command_line
    assert package.inputs["resource_dir"].path == "src/main/resources"
    assert package.inputs["resources"].pattern.template == "src/main/resources/{name}"


def test_no_resource_dir_by_default():
    graph = Graph()
    graph.add(_jar_action())
    _, package = graph.actions
    assert package.command_line.count("-C ") == 1
    assert "resources" not in package.inputs


def test_sources_union_flows_into_javac():
    graph = Graph()
    graph.add(
        _jar_action(
            source_files=data.glob("src/{c}.java") + data.glob("build/gen/{c}.java")
        )
    )
    compile_java, _ = graph.actions
    templates = [a.pattern.template for a in compile_java.inputs["sources"].atoms()]
    assert templates == ["src/{c}.java", "build/gen/{c}.java"]


def test_junit_test_action_uses_two_lenses():
    """The heart of selective test-running: the test *compile* depends on the
    classpath through the jvm-abi lens (API), the test *run* depends on the
    same jars through their bytes (implementation), and the run has no file
    outputs so it replays purely on its input fingerprint."""
    from brix.workers import WorkerSpec

    worker = WorkerSpec(name="javac", command_line="{java} -jar {jar}", inputs={})
    graph = Graph()
    graph.add(
        java.JUnitTestAction(
            name="mytests",
            test_sources=data.glob("src/test/java/{n}.java"),
            classpath=[data.file("build/libs/app.jar")],
            console_jar="lib/console.jar",
            javac_worker=worker,
        )
    )
    compile_tests, run_tests = graph.actions

    # Compile: classpath jars carry the jvm-abi lens (compile cares about API).
    cp_atoms = list(compile_tests.inputs["classpath"].atoms())
    cp = {a.path: a.content_key for a in cp_atoms}
    assert cp["build/libs/app.jar"] == "jvm-abi"
    assert cp["lib/console.jar"] == "jvm-abi"  # the console jar supplies the Jupiter API

    # Run: the code under test is a byte-lens input (no lens — impl matters),
    # the run has NO file outputs (replays on fingerprint), and drives the
    # console launcher with --class-path entries.
    under_test = list(run_tests.inputs["under_test"].atoms())
    assert [a.path for a in under_test] == ["build/libs/app.jar"]
    assert all(a.content_key is None for a in under_test)  # byte lens
    assert run_tests.outputs == {}
    assert "-jar {console}" in run_tests.command_line
    assert "--class-path build/test-classes" in run_tests.command_line
    assert "--class-path build/libs/app.jar" in run_tests.command_line


def test_junit_test_action_tracks_test_resources():
    """Test data dirs are on the run classpath and their files are byte-lens
    inputs — editing test data re-runs the suite (declare what a test reads)."""
    from brix.workers import WorkerSpec

    graph = Graph()
    graph.add(
        java.JUnitTestAction(
            test_sources=data.glob("src/test/java/{n}.java"),
            classpath=[data.file("build/libs/app.jar")],
            console_jar="lib/console.jar",
            test_resource_dirs=["src/test/resources"],
            javac_worker=WorkerSpec(name="javac", command_line="{j}", inputs={}),
        )
    )
    _, run_tests = graph.actions
    assert "--class-path src/test/resources" in run_tests.command_line
    res_inputs = [
        atom.pattern.template
        for key, expr in run_tests.inputs.items()
        if key.startswith("data")
        for atom in expr.atoms()
    ]
    assert res_inputs == ["src/test/resources/{name}"]


needs_jdk = pytest.mark.skipif(
    shutil.which("javac") is None or shutil.which("jar") is None or shutil.which("java") is None,
    reason="no JDK on PATH",
)


@needs_jdk
def test_jar_builds_and_runs(tmp_path):
    import subprocess

    from brix import AtomicBuilder, IncrementalBuilder

    (tmp_path / "src/main/java").mkdir(parents=True)
    (tmp_path / "src/main/java/App.java").write_text(
        'public class App { public static void main(String[] a)'
        ' { System.out.println("jarred by brix"); } }\n'
    )

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            java.JarAction(
                source_files=d.glob("src/main/java/{cls}.java"),
                output_file=d.file("build/libs/app.jar"),
                entry_point="App",
            )
        )
        return graph

    def builder():
        return AtomicBuilder(IncrementalBuilder(create_graph, root=tmp_path))

    assert builder().build().success
    run = subprocess.run(
        ["java", "-jar", str(tmp_path / "build/libs/app.jar")],
        capture_output=True,
        text=True,
    )
    assert run.stdout == "jarred by brix\n"

    second = builder().build()
    assert second.success
    assert all(r.source == "replayed" for r in second.process_results)


@needs_jdk
def test_packaged_class_change_updates_jar(tmp_path):
    """Changing a class in a *package* folder (com/foo/Bar) must re-run the jar
    step, not just javac — otherwise the jar keeps the stale class (a wrong
    incremental build). The default-package `App` calls into `com.foo.Bar`."""
    import subprocess

    from brix import AtomicBuilder, IncrementalBuilder

    (tmp_path / "src/main/java/com/foo").mkdir(parents=True)
    (tmp_path / "src/main/java/App.java").write_text(
        "public class App { public static void main(String[] a)"
        " { System.out.println(com.foo.Bar.msg()); } }\n"
    )

    def write_bar(value: str):
        (tmp_path / "src/main/java/com/foo/Bar.java").write_text(
            f'package com.foo; public class Bar {{ public static String msg()'
            f' {{ return "{value}"; }} }}\n'
        )

    write_bar("one")

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            java.JarAction(
                source_files=d.glob("src/main/java/{cls}.java")
                + d.glob("src/main/java/com/foo/{cls}.java"),
                output_file=d.file("build/libs/app.jar"),
                entry_point="App",
            )
        )
        return graph

    def builder():
        return AtomicBuilder(IncrementalBuilder(create_graph, root=tmp_path))

    def jar_output() -> str:
        return subprocess.run(
            ["java", "-jar", str(tmp_path / "build/libs/app.jar")],
            capture_output=True,
            text=True,
        ).stdout.strip()

    assert builder().build().success
    assert jar_output() == "one"

    write_bar("two")  # change only the packaged class
    third = builder().build()
    assert third.success
    assert jar_output() == "two"  # not "one" — the jar must not go stale


@needs_jdk
def test_resources_packaged_and_tracked(tmp_path):
    """A resource under resource_dir ships in the jar (a ServiceLoader file,
    a .properties), and editing only the resource re-jars — the class is not
    recompiled but the jar reflects the new resource, not a stale one."""
    import subprocess
    import zipfile

    from brix import AtomicBuilder, IncrementalBuilder

    (tmp_path / "src/main/java").mkdir(parents=True)
    (tmp_path / "src/main/java/App.java").write_text(
        "public class App { public static void main(String[] a)"
        " { System.out.println(App.class.getResourceAsStream"
        '("/msg.txt") != null ? "has-resource" : "missing"); } }\n'
    )
    res = tmp_path / "src/main/resources"
    (res / "META-INF/services").mkdir(parents=True)
    (res / "META-INF/services/com.example.Svc").write_text("com.example.Impl\n")
    (res / "msg.txt").write_text("one\n")

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            java.JarAction(
                source_files=d.glob("src/main/java/{cls}.java"),
                output_file=d.file("build/libs/app.jar"),
                resource_dir="src/main/resources",
                entry_point="App",
            )
        )
        return graph

    def builder():
        return AtomicBuilder(IncrementalBuilder(create_graph, root=tmp_path))

    def names() -> set[str]:
        with zipfile.ZipFile(tmp_path / "build/libs/app.jar") as z:
            return set(z.namelist())

    def read(entry: str) -> str:
        with zipfile.ZipFile(tmp_path / "build/libs/app.jar") as z:
            return z.read(entry).decode()

    assert builder().build().success
    entries = names()
    assert "App.class" in entries
    assert "META-INF/services/com.example.Svc" in entries  # nested resource shipped
    assert "msg.txt" in entries
    assert read("msg.txt") == "one\n"
    assert (
        subprocess.run(
            ["java", "-jar", str(tmp_path / "build/libs/app.jar")],
            capture_output=True, text=True,
        ).stdout.strip()
        == "has-resource"
    )

    (res / "msg.txt").write_text("two\n")  # edit only the resource
    assert builder().build().success
    assert read("msg.txt") == "two\n"  # re-jarred, not stale
