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
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/obj/{name}.o"),
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


def test_binding_mismatch_rejected():
    with pytest.raises(ValueError, match="must match"):
        cpp.CompileSourceAction(
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/obj/{unit}.o"),
        )


def test_typed_and_raw_forms_compose_in_one_graph():
    graph = Graph()
    graph.add(
        cpp.CompileSourceAction(
            source_files=data.glob("src/{name}.cpp"),
            object_files=data.glob("build/obj/{name}.o"),
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
                source_files=d.glob("src/{name}.cpp"),
                object_files=d.glob("build/obj/{name}.o"),
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
                source_files=d.glob("src/{name}.cpp"),
                object_files=d.glob("build/obj/{name}.o"),
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
