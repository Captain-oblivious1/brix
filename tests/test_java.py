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


def test_processorpath_is_passed_to_worker_and_tracked_by_bytes():
    """Processor implementation changes matter even when its ABI is stable."""
    from brix.workers import WorkerSpec

    graph = Graph()
    graph.add(
        _jar_action(
            classpath=[data.file("lib/annotations.jar")],
            processorpath=[java.abi(data.file("lib/processors.jar"))],
            javac_worker=WorkerSpec(name="javac", command_line="{j}", inputs={}),
        )
    )
    compile_java, _ = graph.actions

    assert "--processorpath {processorpath}" in compile_java.command_line
    processor = list(compile_java.inputs["processorpath"].atoms())
    assert [atom.path for atom in processor] == ["lib/processors.jar"]
    assert all(atom.content_key is None for atom in processor)


def test_processorpath_requires_worker():
    with pytest.raises(ValueError, match="processorpath"):
        _jar_action(processorpath=[data.file("lib/processors.jar")])


def test_junit_test_action_compiles_abi_and_expands_per_class():
    """The heart of selective test-running: the test *compile* depends on the
    classpath through the jvm-abi lens (API), and the run lowers to an ExpandAll
    over the compiled test classes — one discovery-wrapped run per test class,
    each with its own depfile output so brix folds in the classes it loaded."""
    from brix.actions import ExpandAll
    from brix.workers import WorkerSpec

    worker = WorkerSpec(name="javac", command_line="{java} -jar {jar}", inputs={})
    graph = Graph()
    graph.add(
        java.JUnitTestAction(
            name="mytests",
            test_sources=data.glob("src/test/java/{n}.java"),
            classpath=[data.file("build/classes/app")],
            console_jar="lib/console.jar",
            javac_worker=worker,
        )
    )
    compile_tests, expand = graph.actions

    # Compile: classpath carries the jvm-abi lens (compile cares about API).
    cp = {a.path: a.content_key for a in compile_tests.inputs["classpath"].atoms()}
    assert cp["build/classes/app"] == "jvm-abi"
    assert cp["lib/console.jar"] == "jvm-abi"  # the console jar supplies the Jupiter API

    # Run: an ExpandAll over the compiled classes (inner `$` classes excluded),
    # one child run per class, discovery-driven (a depfile output, `depfile=`).
    assert isinstance(expand, ExpandAll)
    assert expand.element_name == "name"
    assert "*$*.class" in expand.collection.exclude
    run_one = expand.child
    assert run_one.depfile == "build/test-classes/.brix-classload/{name}.d"
    assert set(run_one.outputs) == {"depfile"}
    assert "{recorder}" in run_one.command_line
    # The run classpath (a value) carries the compiled tests + the code under test.
    classpath = run_one.inputs["classpath"].text
    assert "build/test-classes" in classpath
    assert "build/classes/app" in classpath


def test_junit_test_action_puts_test_resources_on_the_classpath():
    """Test data dirs join each run's classpath so a test can read its fixtures."""
    from brix.workers import WorkerSpec

    graph = Graph()
    graph.add(
        java.JUnitTestAction(
            test_sources=data.glob("src/test/java/{n}.java"),
            classpath=[data.file("build/classes/app")],
            console_jar="lib/console.jar",
            test_resource_dirs=["src/test/resources"],
            javac_worker=WorkerSpec(name="javac", command_line="{j}", inputs={}),
        )
    )
    _, expand = graph.actions
    assert "src/test/resources" in expand.child.inputs["classpath"].text


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
