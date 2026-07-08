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
    # Recursive glob so packaged classes (com/foo/Bar.class) are tracked too.
    assert compile_java.outputs["classes"].pattern.template == "build/classes/{**name}.class"

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
