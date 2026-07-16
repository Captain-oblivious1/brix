"""End-to-end JVM phase 2: JarAction compiling through the javac worker, and
the compile-avoidance payoff — a dependency's implementation-only change does
not recompile its dependent, but an API change does.

Skips without a JDK on PATH.
"""

import shutil
import subprocess

import pytest

from brix import Graph, IncrementalBuilder, Reporter, java

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("javac", "jar", "java")),
    reason="needs a JDK (javac/jar/java) on PATH",
)


class Started(Reporter):
    """Records process_started — one entry per real spawn or worker dispatch."""

    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


APP = "public class App { public static void main(String[] a){ System.out.println(Dep.val()); } }\n"
DEP_BASE = "public class Dep { public static int val(){ return 42; } }\n"
# Same ABI (val() signature unchanged; compute() is private), different bytecode:
DEP_IMPL = (
    "public class Dep { public static int val(){ return compute(); }"
    " private static int compute(){ return 42; } }\n"
)
# API change: a new public method changes the ABI.
DEP_API = (
    "public class Dep { public static int val(){ return 42; }"
    " public static int extra(){ return 7; } }\n"
)


def workspace(root, dep_src):
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "lib/Dep.java").write_text(dep_src)
    (root / "app/App.java").write_text(APP)


def make_graph():
    graph = Graph()
    d = graph.data
    worker = java.javac_worker(graph)
    graph.add(
        java.JarAction(
            source_files=d.glob("lib/{name}.java"),
            output_file=d.file("build/lib.jar"),
            classes_dir="build/lib-classes",
            javac_worker=worker,
        ),
        java.JarAction(
            source_files=d.glob("app/{name}.java"),
            output_file=d.file("build/app.jar"),
            classes_dir="build/app-classes",
            classpath=[d.file("build/lib.jar")],
            javac_worker=worker,
            entry_point="App",
        ),
    )
    return graph


def compiled(labels, module):
    return any(f"{module}-classes" in label for label in labels)


def test_multi_module_worker_build_runs(tmp_path):
    workspace(tmp_path, DEP_BASE)
    result = IncrementalBuilder(make_graph, root=tmp_path).build()
    assert result.success, result.errors
    assert (tmp_path / "build/app.jar").is_file()
    run = subprocess.run(
        ["java", "-cp", "build/app.jar:build/lib.jar", "App"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert run.stdout.strip() == "42"


def test_impl_only_change_does_not_recompile_dependent(tmp_path):
    workspace(tmp_path, DEP_BASE)
    assert IncrementalBuilder(make_graph, root=tmp_path).build().success

    (tmp_path / "lib/Dep.java").write_text(DEP_IMPL)  # same ABI, new bytecode
    started = Started()
    assert IncrementalBuilder(make_graph, root=tmp_path).build(reporter=started).success

    assert compiled(started.labels, "lib")  # the dependency recompiled
    assert not compiled(started.labels, "app")  # the dependent did NOT — ABI unchanged


def test_api_change_recompiles_dependent(tmp_path):
    workspace(tmp_path, DEP_BASE)
    assert IncrementalBuilder(make_graph, root=tmp_path).build().success

    (tmp_path / "lib/Dep.java").write_text(DEP_API)  # new public method -> ABI changes
    started = Started()
    assert IncrementalBuilder(make_graph, root=tmp_path).build(reporter=started).success

    assert compiled(started.labels, "app")  # the dependent recompiled


def test_annotation_processor_implementation_change_recompiles(tmp_path):
    """A processor is build-time executable code, so its bytes—not its ABI—
    invalidate compilation and regenerate the application output."""
    processor_src = tmp_path / "processor-src"
    processor_classes = tmp_path / "processor-classes"
    app_src = tmp_path / "app"
    processor_src.mkdir()
    processor_classes.mkdir()
    app_src.mkdir()
    (processor_src / "Marker.java").write_text(
        "import java.lang.annotation.*;"
        "@Target(ElementType.TYPE) public @interface Marker {}\n"
    )
    (app_src / "App.java").write_text(
        "@Marker public class App { public static void main(String[] args) {"
        " System.out.println(Generated.message()); } }\n"
    )

    processor_jar = tmp_path / "processor.jar"

    def build_processor(message: str) -> None:
        (processor_src / "GeneratingProcessor.java").write_text(
            "import java.io.*; import java.util.*; import javax.annotation.processing.*;"
            "import javax.lang.model.*; import javax.lang.model.element.*;"
            '@SupportedAnnotationTypes("Marker") '
            "@SupportedSourceVersion(SourceVersion.RELEASE_17) "
            "public class GeneratingProcessor extends AbstractProcessor {"
            " private boolean generated;"
            " public boolean process(Set<? extends TypeElement> a, RoundEnvironment r) {"
            "  if (!generated && !r.processingOver()) { generated = true; try {"
            '   Writer w = processingEnv.getFiler().createSourceFile("Generated").openWriter();'
            '   w.write("public class Generated { public static String message() {'
            f' return \\\"{message}\\\"; }} }}\\n");'
            "   w.close(); } catch (IOException e) { throw new RuntimeException(e); } }"
            "  return true; } }\n"
        )
        subprocess.run(
            [
                "javac", "-d", str(processor_classes),
                str(processor_src / "Marker.java"),
                str(processor_src / "GeneratingProcessor.java"),
            ],
            check=True,
        )
        subprocess.run(
            ["jar", "cf", str(processor_jar), "-C", str(processor_classes), "."],
            check=True,
        )

    def graph():
        g = Graph()
        d = g.data
        worker = java.javac_worker(g)
        g.add(
            java.JarAction(
                source_files=d.glob("app/{name}.java"),
                output_file=d.file("build/app.jar"),
                classes_dir="build/app-classes",
                classpath=[d.file("processor.jar")],
                processorpath=[d.file("processor.jar")],
                options=["-processor", "GeneratingProcessor"],
                javac_worker=worker,
                entry_point="App",
            )
        )
        return g

    def output() -> str:
        return subprocess.run(
            ["java", "-jar", "build/app.jar"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    build_processor("one")
    assert IncrementalBuilder(graph, root=tmp_path).build().success
    assert output() == "one"

    # Public types and signatures stay the same; only processor behavior changes.
    build_processor("two")
    started = Started()
    result = IncrementalBuilder(graph, root=tmp_path).build(reporter=started)
    assert result.success, result.errors
    assert compiled(started.labels, "app")
    assert output() == "two"
