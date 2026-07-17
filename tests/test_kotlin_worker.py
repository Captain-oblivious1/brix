"""The warm kotlinc worker: `kotlin.kotlinc_worker` + `JarAction(kotlinc_worker=)`.

A cold kotlinc reloads and re-JITs the whole Kotlin compiler on every call, so
keeping it warm is the Kotlin edit-loop win — the analogue of the javac worker.
The worker is *pure transport*: routing a module's compile through it must be
indistinguishable in results and in what recompiles a dependent. These tests
cover the routing/graph shape without a toolchain, then prove end-to-end (with
a real compiler) that a worker build is correct, actually uses the worker, and
preserves the kotlin-abi skip — an inline-free implementation edit does not
recompile the dependent, but an inline-body edit does.

kotlinc-gated: set `BRIX_KOTLINC=/path/to/kotlinc` (a dist layout, so
`../lib/kotlin-compiler.jar` and the stdlib sit beside it); falls back to a
`kotlinc` on PATH; skips when neither is present or no JDK is on PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from brix import Graph, IncrementalBuilder, Reporter, kotlin
from brix.data import FileData
from brix.workers import WorkerSpec

# --- toolchain discovery ---------------------------------------------------

KOTLINC = os.environ.get("BRIX_KOTLINC") or shutil.which("kotlinc")


def _dist_lib() -> Path | None:
    if not KOTLINC:
        return None
    lib = Path(KOTLINC).resolve().parent.parent / "lib"
    return lib if lib.is_dir() else None


_LIB = _dist_lib()
KOTLIN_COMPILER = _LIB / "kotlin-compiler.jar" if _LIB else None
KOTLIN_STDLIB = _LIB / "kotlin-stdlib.jar" if _LIB else None

_have_jdk = all(shutil.which(t) for t in ("java", "javac", "jar"))
needs_worker = pytest.mark.skipif(
    not (KOTLIN_COMPILER and KOTLIN_COMPILER.is_file() and _have_jdk),
    reason="needs a kotlinc dist (BRIX_KOTLINC) and a JDK (java/javac/jar) on PATH",
)


# --- toolchain-free unit tests (graph shape) -------------------------------

def test_kotlinc_worker_is_idempotent():
    """One graph, many JarActions: the bootstrap is added once and the same
    spec is shared (like java.javac_worker)."""
    graph = Graph()
    first = kotlin.kotlinc_worker(graph, kotlin_compiler="/opt/kotlinc/lib/kotlin-compiler.jar")
    before = len(graph.actions)
    second = kotlin.kotlinc_worker(graph, kotlin_compiler="/opt/kotlinc/lib/kotlin-compiler.jar")
    assert first is second
    assert len(graph.actions) == before  # no second bootstrap pair
    assert isinstance(first, WorkerSpec)
    assert first.name == "kotlinc"


def test_worker_launch_joins_worker_jar_and_compiler():
    graph = Graph()
    spec = kotlin.kotlinc_worker(graph, kotlin_compiler="/opt/kc/lib/kotlin-compiler.jar")
    # The -cp token glues worker jar + separator + compiler with no spaces, so
    # the three placeholders render into one argument.
    assert "-cp {worker_jar}{sep}{kotlin_compiler}" in spec.command_line
    assert spec.inputs["sep"].text == os.pathsep
    assert spec.inputs["kotlin_compiler"].path == "/opt/kc/lib/kotlin-compiler.jar"


def test_jaraction_routes_compile_through_the_worker():
    """With kotlinc_worker=, the compile step carries worker= and speaks the
    worker protocol (--sources/--classpath/--plugin), not a spawned kotlinc."""
    spec = WorkerSpec(name="kotlinc", command_line="{java}", inputs={})
    action = kotlin.JarAction(
        source_files=FileData("src/A.kt"),
        output_file=FileData("build/a.jar"),
        classpath=[FileData("build/dep.jar")],
        spring_allopen="/opt/kc/lib/allopen-compiler-plugin.jar",
        kotlinc_worker=spec,
    )
    compile_action, _package = action.lower()
    assert compile_action.worker is spec
    cmd = compile_action.command_line
    assert "--sources {sources}" in cmd
    assert "--classpath {classpath}" in cmd
    assert "--plugin {allopen}" in cmd and "--plugin-option {preset}" in cmd
    assert "-jvm-target {jvm_target}" in cmd
    # The classpath is kotlin-abi-lensed, exactly like the spawn path.
    assert all(
        getattr(a, "content_key", None) == "kotlin-abi"
        for a in compile_action.inputs["classpath"].atoms()
    )


def test_spawn_path_unchanged_without_worker():
    action = kotlin.JarAction(
        source_files=FileData("src/A.kt"),
        output_file=FileData("build/a.jar"),
    )
    compile_action, _package = action.lower()
    assert getattr(compile_action, "worker", None) is None
    assert compile_action.command_line.startswith("{kotlinc}")


# --- end-to-end (real compiler) --------------------------------------------

class Started(Reporter):
    """One label per real spawn or worker dispatch."""

    def __init__(self):
        self.labels = []

    def process_started(self, node):
        self.labels.append(node.label)


APP = (
    "package demo.app\n"
    "import demo.core.Greeter\nimport demo.core.wrap\n"
    'fun main() { println(wrap { Greeter().greet("brix") }) }\n'
)
# Base library.
LIB_BASE = (
    "package demo.core\n"
    'class Greeter { fun greet(n: String) = "hi-$n" }\n'
    'inline fun wrap(b: () -> String) = "[" + b() + "]"\n'
)
# Implementation-only edit in the inline-FREE class: greet's body changes, the
# inline fun is untouched -> the dependent's bytecode is invariant.
LIB_IMPL = (
    "package demo.core\n"
    'class Greeter { fun greet(n: String) = "hello-$n" }\n'
    'inline fun wrap(b: () -> String) = "[" + b() + "]"\n'
)
# Inline-body edit: wrap's body changes -> it is copied into the dependent, so
# the dependent MUST recompile or ship stale bytecode.
LIB_INLINE = (
    "package demo.core\n"
    'class Greeter { fun greet(n: String) = "hi-$n" }\n'
    'inline fun wrap(b: () -> String) = "{" + b() + "}"\n'
)


def _write(root, lib_src):
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "lib/core.kt").write_text(lib_src)
    (root / "app/app.kt").write_text(APP)


def _graph():
    graph = Graph()
    d = graph.data
    worker = kotlin.kotlinc_worker(graph, kotlin_compiler=str(KOTLIN_COMPILER))
    graph.add(
        kotlin.JarAction(
            source_files=d.glob("lib/{name}.kt"),
            output_file=d.file("build/core.jar"),
            classes_dir="build/core-classes",
            kotlinc_worker=worker,
        ),
        kotlin.JarAction(
            source_files=d.glob("app/{name}.kt"),
            output_file=d.file("build/app.jar"),
            classes_dir="build/app-classes",
            classpath=[d.file("build/core.jar")],
            entry_point="demo.app.AppKt",
            kotlinc_worker=worker,
        ),
    )
    return graph


def _compiled(labels, module):
    return any(f"{module}-classes" in label for label in labels)


def _run(root):
    cp = os.pathsep.join(["build/app.jar", "build/core.jar", str(KOTLIN_STDLIB)])
    return subprocess.run(
        ["java", "-cp", cp, "demo.app.AppKt"],
        cwd=root, capture_output=True, text=True,
    ).stdout.strip()


@needs_worker
def test_worker_build_runs_and_uses_the_worker(tmp_path):
    _write(tmp_path, LIB_BASE)
    started = Started()
    result = IncrementalBuilder(_graph, root=tmp_path).build(reporter=started)
    assert result.success, result.errors
    assert (tmp_path / "build/app.jar").is_file()
    assert _run(tmp_path) == "[hi-brix]"
    # Compilation went through the worker, not a spawned kotlinc.
    assert any("kotlinc(worker)" in label for label in started.labels)


@needs_worker
def test_impl_only_change_skips_dependent_through_worker(tmp_path):
    """The recovered cross-module skip, over the worker: a normal-body edit in
    an inline-free class recompiles core but the app REPLAYS."""
    _write(tmp_path, LIB_BASE)
    assert IncrementalBuilder(_graph, root=tmp_path).build().success

    (tmp_path / "lib/core.kt").write_text(LIB_IMPL)
    started = Started()
    assert IncrementalBuilder(_graph, root=tmp_path).build(reporter=started).success
    assert _compiled(started.labels, "core")  # dependency recompiled
    assert not _compiled(started.labels, "app")  # dependent did NOT — ABI stable


@needs_worker
def test_inline_body_change_recompiles_dependent_through_worker(tmp_path):
    """An inline-body edit is copied into the dependent, so it MUST recompile."""
    _write(tmp_path, LIB_BASE)
    assert IncrementalBuilder(_graph, root=tmp_path).build().success

    (tmp_path / "lib/core.kt").write_text(LIB_INLINE)
    started = Started()
    assert IncrementalBuilder(_graph, root=tmp_path).build(reporter=started).success
    assert _compiled(started.labels, "app")  # dependent recompiled
    assert _run(tmp_path) == "{hi-brix}"  # and picked up the new inline body
