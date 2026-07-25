"""brix.cpp typed actions: lower to the generic model, compose with layers."""

import shutil
import sys

import pytest

from brix import AtomicBuilder, ExpandAll, ExternalProcessAction, Graph, IncrementalBuilder, cpp
from brix.data import DataFactory

data = DataFactory()


def test_compile_lowers_to_expandall():
    graph = Graph()
    graph.add(
        cpp.CompileSourceAction(
            source_file=data.glob("src/{name}.cpp"),
            object_file=data.glob("build/obj/{name}.o"),
            compile_options=["-O2"],
        )
    )
    (action,) = graph.actions
    assert isinstance(action, ExpandAll)
    assert action.element_name == "name"
    child = action.child
    assert isinstance(child, ExternalProcessAction)
    assert child.inputs["source"].path == "src/{name}.cpp"
    assert child.outputs["object"].path == "build/obj/{name}.o"
    assert child.inputs["compiler"].name == "c++"  # default
    assert child.inputs["options"].text == "-O2"


def test_link_lowers_to_process_action():
    graph = Graph()
    graph.add(
        cpp.LinkApplicationAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/bin/app"),
        )
    )
    (action,) = graph.actions
    assert isinstance(action, ExternalProcessAction)
    assert action.name == "link build/bin/app"
    assert action.outputs["output"].path == "build/bin/app"


def test_archive_lowers_to_ar():
    graph = Graph()
    graph.add(
        cpp.ArchiveAction(
            object_files=data.glob("build/obj/{name}.o"),
            output_file=data.file("build/libfoo.a"),
        )
    )
    (action,) = graph.actions
    assert isinstance(action, ExternalProcessAction)
    assert action.name == "ar build/libfoo.a"
    # Deterministic (D) so the archive is byte-reproducible; c=create,
    # r=replace, s=symbol index.
    assert action.command_line == "{ar} Dcrs {output} {objects}"
    assert action.outputs["output"].path == "build/libfoo.a"


def test_binding_mismatch_rejected():
    with pytest.raises(ValueError, match="must match"):
        cpp.CompileSourceAction(
            source_file=data.glob("src/{name}.cpp"),
            object_file=data.glob("build/obj/{unit}.o"),
        )


def test_typed_and_raw_forms_compose_in_one_graph():
    graph = Graph()
    graph.add(
        cpp.CompileSourceAction(
            source_file=data.glob("src/{name}.cpp"),
            object_file=data.glob("build/obj/{name}.o"),
        ),
        ExternalProcessAction(
            name="raw",
            command_line="echo",
        ),
    )
    kinds = [type(a).__name__ for a in graph.actions]
    assert kinds == ["ExpandAll", "ExternalProcessAction"]


needs_cxx = pytest.mark.skipif(shutil.which("c++") is None, reason="no c++ compiler")


@needs_cxx
def test_compile_link_run_with_all_layers(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.cpp").write_text(
        "#include <string>\nstd::string msg() { return \"built by brix\"; }\n"
    )
    (tmp_path / "src/main.cpp").write_text(
        "#include <iostream>\n#include <string>\nstd::string msg();\n"
        "int main() { std::cout << msg() << \"\\n\"; }\n"
    )

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            cpp.CompileSourceAction(
                source_file=d.glob("src/{name}.cpp"),
                object_file=d.glob("build/obj/{name}.o"),
                compile_options=["-O1"],
            ),
            cpp.LinkApplicationAction(
                object_files=d.glob("build/obj/{name}.o"),
                output_file=d.file("build/bin/app"),
            ),
        )
        return graph

    def builder():
        return AtomicBuilder(IncrementalBuilder(create_graph, root=tmp_path))

    first = builder().build()
    assert first.success
    import subprocess

    run = subprocess.run(
        [str(tmp_path / "build/bin/app")], capture_output=True, text=True
    )
    assert run.stdout == "built by brix\n"

    # Everything replays on the second build; nothing staged, still committed.
    second = builder().build()
    assert second.success
    assert all(r.source == "replayed" for r in second.process_results)
    assert not (tmp_path / "build/staging").exists()

    # Deleting a translation unit cleans its object and relinks.
    (tmp_path / "src/lib.cpp").unlink()
    (tmp_path / "src/main.cpp").write_text("int main() { return 0; }\n")
    third = builder().build()
    assert third.success
    assert not (tmp_path / "build/obj/lib.o").exists()


@needs_cxx
def test_header_change_rebuilds_only_dependent_object(tmp_path):
    """Editing a header rebuilds exactly the translation units that #include it
    — the classic make .d gap, closed via compiler depfiles. `main.cpp`
    includes `util.h`; `other.cpp` does not."""
    (tmp_path / "src").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "include/util.h").write_text("inline int val() { return 1; }\n")
    (tmp_path / "src/main.cpp").write_text(
        '#include "util.h"\nint main() { return val(); }\n'
    )
    (tmp_path / "src/other.cpp").write_text("int other() { return 0; }\n")

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            cpp.CompileSourceAction(
                source_file=d.glob("src/{name}.cpp"),
                object_file=d.glob("build/obj/{name}.o"),
                compile_options=["-I", "include"],
            )
        )
        return graph

    def builder():
        return IncrementalBuilder(create_graph, root=tmp_path)

    assert builder().build().success
    # Second build settles: discovered headers are folded into the fingerprint,
    # so with nothing changed everything replays (no spurious extra rebuild).
    second = builder().build()
    assert all(r.source == "replayed" for r in second.process_results), [
        (r.action_name, r.source) for r in second.process_results
    ]

    # Edit the header -> only main (which includes it) recompiles.
    (tmp_path / "include/util.h").write_text("inline int val() { return 2; }\n")
    third = builder().build()
    assert third.success
    ran = sorted(r.action_name for r in third.process_results if r.source == "process")
    assert ran == ["compile main"]  # not "compile other"


needs_ar = pytest.mark.skipif(
    shutil.which("c++") is None or shutil.which("ar") is None, reason="no c++/ar"
)


@needs_ar
def test_static_library_archive_link_run(tmp_path):
    """Compile library sources into a static lib (ar), then link an app
    against it and run — the compile -> archive -> link -> run chain leveldb
    and friends use. A library-impl change re-archives and re-links; the app
    object replays (it depends only on the header/API, unchanged here)."""
    import subprocess

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "lib/greet.cpp").write_text(
        '#include <string>\nstd::string greet() { return "hello"; }\n'
    )
    (tmp_path / "lib/who.cpp").write_text(
        '#include <string>\nstd::string who() { return "brix"; }\n'
    )
    (tmp_path / "app/main.cpp").write_text(
        "#include <iostream>\n#include <string>\n"
        "std::string greet(); std::string who();\n"
        'int main() { std::cout << greet() << " " << who() << "\\n"; }\n'
    )

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            cpp.CompileSourceAction(
                source_file=d.glob("lib/{name}.cpp"),
                object_file=d.glob("build/obj/lib/{name}.o"),
            ),
            cpp.ArchiveAction(
                object_files=d.glob("build/obj/lib/{name}.o"),
                output_file=d.file("build/libgreet.a"),
            ),
            cpp.CompileSourceAction(
                source_file=d.glob("app/{name}.cpp"),
                object_file=d.glob("build/obj/app/{name}.o"),
            ),
            cpp.LinkApplicationAction(
                object_files=d.glob("build/obj/app/{name}.o") + d.file("build/libgreet.a"),
                output_file=d.file("build/bin/app"),
            ),
        )
        return graph

    def builder():
        return AtomicBuilder(IncrementalBuilder(create_graph, root=tmp_path))

    assert builder().build().success
    out = subprocess.run(
        [str(tmp_path / "build/bin/app")], capture_output=True, text=True
    ).stdout.strip()
    assert out == "hello brix"

    # Change a library implementation: re-archive + re-link; app object replays.
    (tmp_path / "lib/greet.cpp").write_text(
        '#include <string>\nstd::string greet() { return "hi"; }\n'
    )
    second = builder().build()
    assert second.success
    ran = sorted(r.action_name for r in second.process_results if r.source == "process")
    assert "compile greet" in ran
    assert "ar build/libgreet.a" in ran
    assert "link build/bin/app" in ran
    assert "compile main" not in ran  # app object unchanged
    out = subprocess.run(
        [str(tmp_path / "build/bin/app")], capture_output=True, text=True
    ).stdout.strip()
    assert out == "hi brix"


def test_run_test_lowers_to_a_tracked_run():
    """A RunTestAction is an exit-code-only process: the binary is a byte-lens
    input and there are NO outputs, so it replays on its fingerprint."""
    graph = Graph()
    graph.add(cpp.RunTestAction(test_binary="build/test/foo_test"))
    (action,) = graph.actions
    assert isinstance(action, ExternalProcessAction)
    assert action.name == "run build/test/foo_test"
    assert action.outputs == {}  # exit code only
    assert action.inputs["binary"].path == "build/test/foo_test"  # byte lens
    # A path with a directory separator execs directly on every OS — no "./"
    # (which would break on Windows). Commands are exec'd, not shell'd.
    assert action.command_line.startswith("{binary}")


def test_run_test_binary_reference_is_platform_correct(monkeypatch):
    """Only a *bare* name in the cwd needs the POSIX `./`; a path with a
    separator never does, and Windows never gets `./` at all."""
    import brix.cpp as cppmod

    # Path with a separator: no prefix, either OS.
    graph = Graph()
    graph.add(cpp.RunTestAction(test_binary="build/test/foo_test"))
    assert graph.actions[0].command_line.startswith("{binary}")

    # Bare name on POSIX: needs "./" so exec finds it in cwd (no PATH lookup).
    monkeypatch.setattr(cppmod.os, "name", "posix")
    graph = Graph()
    graph.add(cpp.RunTestAction(test_binary="foo_test"))
    assert graph.actions[0].command_line.startswith("./{binary}")

    # Bare name on Windows: no "./" convention.
    monkeypatch.setattr(cppmod.os, "name", "nt")
    graph = Graph()
    graph.add(cpp.RunTestAction(test_binary="foo_test.exe"))
    assert graph.actions[0].command_line.startswith("{binary}")


def test_runtime_inputs_are_tracked():
    """A dlopen'd plugin / data file the linker never sees is declared here so a
    change to it re-runs the test."""
    graph = Graph()
    graph.add(
        cpp.RunTestAction(
            test_binary="build/test/foo_test",
            runtime_inputs=["build/plugins/libx.so"],
        )
    )
    (action,) = graph.actions
    assert action.inputs["runtime0"].path == "build/plugins/libx.so"


needs_cxx = pytest.mark.skipif(shutil.which("c++") is None, reason="no c++")


@needs_cxx
def test_editing_one_lib_reruns_only_its_test(tmp_path):
    """The whole C++ selection claim, end to end: one RunTestAction per test
    binary, and editing a source re-runs only the binaries that link it — the
    incremental engine does the selecting, because a binary is a build product."""
    (tmp_path / "src").mkdir()
    (tmp_path / "test").mkdir()
    (tmp_path / "src/a.cpp").write_text("int a() { return 1; }\n")
    (tmp_path / "src/b.cpp").write_text("int b() { return 2; }\n")
    (tmp_path / "test/a_test.cpp").write_text("int a(); int main() { return a()==1?0:1; }\n")
    (tmp_path / "test/b_test.cpp").write_text("int b(); int main() { return b()==2?0:1; }\n")

    def create_graph():
        graph = Graph()
        d = graph.data
        for tree in ("src", "test"):
            graph.add(cpp.CompileSourceAction(
                source_file=d.glob(f"{tree}/{{name}}.cpp"),
                object_file=d.glob(f"build/obj/{tree}/{{name}}.o"),
            ))
        for name, objs in {"a_test": ["src/a", "test/a_test"],
                           "b_test": ["src/b", "test/b_test"]}.items():
            link = d.file(f"build/obj/{objs[0]}.o")
            for o in objs[1:]:
                link = link + d.file(f"build/obj/{o}.o")
            graph.add(cpp.LinkApplicationAction(
                object_files=link, output_file=d.file(f"build/test/{name}")))
            graph.add(cpp.RunTestAction(test_binary=f"build/test/{name}"))
        return graph

    def builder():
        return IncrementalBuilder(create_graph, root=tmp_path)

    assert builder().build().success
    assert all(r.source == "replayed" for r in builder().build().process_results)

    # Edit a's implementation (behavior-preserving): only a_test re-runs.
    (tmp_path / "src/a.cpp").write_text("int a() { return 1; }\nint a_meta() { return 7; }\n")
    third = builder().build()
    assert third.success
    ran = sorted(r.action_name for r in third.process_results if r.source == "process")
    assert "run build/test/a_test" in ran
    assert "run build/test/b_test" not in ran  # provably unaffected -> replayed
