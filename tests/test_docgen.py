"""Documentation-generation actions: java.JavadocAction, ts.TypedocAction,
cpp.DoxygenAction.

These run an ecosystem's own doc generator over a *user's* project as an
ordinary tracked, incremental build step. The lowering-shape tests need no
tools installed; the one end-to-end test uses the real ``javadoc`` (present in
any JDK) to prove the action is incremental — it regenerates when a source
changes and replays when nothing did.
"""

import shutil
import subprocess

import pytest

from brix import AtomicBuilder, ExternalProcessAction, Graph, IncrementalBuilder, cpp, java, ts
from brix.data import DataFactory

data = DataFactory()


def test_javadoc_lowers_to_a_single_tracked_action():
    graph = Graph()
    graph.add(
        java.JavadocAction(
            source_files=data.glob("src/main/java/{name}.java"),
            output_dir="build/docs/api",
        )
    )
    (action,) = graph.actions
    assert isinstance(action, ExternalProcessAction)
    assert action.name == "javadoc -> build/docs/api"
    assert action.command_line == "{javadoc} -d {output_dir} {options} {sources}"
    # Sources are declared inputs — this is what makes the doc set incremental.
    assert "sources" in action.inputs
    # The whole output tree is one declared, atomically staged output.
    assert action.outputs["output_dir"].path == "build/docs/api"
    assert action.outputs["files"].pattern.template == "build/docs/api/{name}"


def test_javadoc_classpath_uses_the_abi_lens():
    # An impl-only change in a referenced dependency should let the docs replay,
    # so the classpath is tracked through the abi lens, not raw bytes.
    dep = data.file("libs/dep.jar")
    graph = Graph()
    graph.add(
        java.JavadocAction(
            source_files=data.glob("src/{name}.java"),
            output_dir="build/docs",
            classpath=[dep],
        )
    )
    (action,) = graph.actions
    assert "-classpath {classpath}" in action.command_line
    assert all(
        getattr(atom, "content_key", None) == "jvm-abi"
        for atom in action.inputs["classpath"].atoms()
    )


def test_typedoc_lowers_to_a_single_tracked_action():
    graph = Graph()
    graph.add(
        ts.TypedocAction(
            entry_points=data.glob("src/{name}.ts"),
            output_dir="build/docs/api",
        )
    )
    (action,) = graph.actions
    assert isinstance(action, ExternalProcessAction)
    assert action.name == "typedoc -> build/docs/api"
    assert action.command_line == "{typedoc} --out {output_dir} {options} {entry_points}"
    assert "entry_points" in action.inputs
    assert action.outputs["output_dir"].path == "build/docs/api"
    assert action.outputs["files"].pattern.template == "build/docs/api/{name}"


def test_doxygen_lowers_config_driven_with_sources_as_inputs():
    graph = Graph()
    graph.add(
        cpp.DoxygenAction(
            config="Doxyfile",
            source_files=data.glob("src/{name}.h"),
            output_dir="build/docs/html",
        )
    )
    (action,) = graph.actions
    assert isinstance(action, ExternalProcessAction)
    assert action.name == "doxygen -> build/docs/html"
    # Config-driven: no source args on the line, but the sources and the config
    # are declared inputs so an edited header or Doxyfile regenerates.
    assert action.command_line == "{doxygen} {config}"
    assert action.inputs["config"].path == "Doxyfile"
    assert "sources" in action.inputs
    assert action.outputs["output_dir"].path == "build/docs/html"
    assert action.outputs["files"].pattern.template == "build/docs/html/{name}"


needs_javadoc = pytest.mark.skipif(
    shutil.which("javadoc") is None, reason="no javadoc in PATH"
)


@needs_javadoc
def test_javadoc_is_incremental_on_the_real_tool(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Greeter.java").write_text(
        "/** Greets. */\npublic class Greeter {\n"
        "  /** @return a greeting */\n"
        "  public String hello() { return \"hi\"; }\n}\n"
    )

    def create_graph():
        graph = Graph()
        d = graph.data
        graph.add(
            java.JavadocAction(
                source_files=d.glob("src/{name}.java"),
                output_dir="build/docs",
                options=["-quiet", "-Xdoclint:none"],
            )
        )
        return graph

    def builder():
        return AtomicBuilder(IncrementalBuilder(create_graph, root=tmp_path))

    first = builder().build()
    assert first.success, first
    # javadoc actually produced the page tree.
    assert (tmp_path / "build/docs/Greeter.html").is_file()
    assert (tmp_path / "build/docs/index.html").is_file()

    # Nothing changed: the whole action replays, no staging left behind.
    second = builder().build()
    assert second.success
    assert all(r.source == "replayed" for r in second.process_results)
    assert not (tmp_path / "build/staging").exists()

    # A source edit regenerates the docs.
    (src / "Greeter.java").write_text(
        "/** Greets, politely. */\npublic class Greeter {\n"
        "  /** @return a polite greeting */\n"
        "  public String hello() { return \"good day\"; }\n}\n"
    )
    third = builder().build()
    assert third.success
    ran = [r.action_name for r in third.process_results if r.source == "process"]
    assert ran == ["javadoc -> build/docs"]
    html = (tmp_path / "build/docs/Greeter.html").read_text()
    assert "polite" in html
