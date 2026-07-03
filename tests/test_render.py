import sys

import pytest

from brix.actions import ExternalProcessAction
from brix.data import DataFactory
from brix.render import RenderError, prepare_output_dirs, render_command

data = DataFactory()


def test_render_basic(tmp_path):
    action = ExternalProcessAction(
        name="say",
        command_line="{python} -c {code}",
        inputs={
            "python": data.tool(sys.executable),
            "code": data.value("'print(1)'"),
        },
    )
    assert render_command(action, tmp_path) == f"{sys.executable} -c 'print(1)'"


def test_render_glob_input_joins_sorted(tmp_path):
    (tmp_path / "obj").mkdir()
    (tmp_path / "obj/b.o").write_text("")
    (tmp_path / "obj/a.o").write_text("")
    action = ExternalProcessAction(
        name="link",
        command_line="ld {objects} -o {app}",
        inputs={"objects": data.glob("obj/{n}.o")},
        outputs={"app": data.file("bin/app")},
    )
    assert render_command(action, tmp_path) == "ld obj/a.o obj/b.o -o bin/app"


def test_render_union(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a/x.c").write_text("")
    (tmp_path / "b/y.c").write_text("")
    action = ExternalProcessAction(
        name="all",
        command_line="cc {sources}",
        inputs={"sources": data.glob("a/{n}.c") + data.glob("b/{n}.c")},
    )
    assert render_command(action, tmp_path) == "cc a/x.c b/y.c"


def test_unknown_placeholder(tmp_path):
    action = ExternalProcessAction(name="bad", command_line="echo {missing}")
    with pytest.raises(RenderError, match="unknown placeholder"):
        render_command(action, tmp_path)


def test_unresolved_binding_is_error(tmp_path):
    action = ExternalProcessAction(
        name="bad",
        command_line="cat {source}",
        inputs={"source": data.file("src/{name}.txt")},
    )
    with pytest.raises(RenderError, match="unresolved bindings"):
        render_command(action, tmp_path)


def test_missing_tool_is_error(tmp_path):
    action = ExternalProcessAction(
        name="bad",
        command_line="{tool}",
        inputs={"tool": data.tool("definitely-not-a-real-tool-xyz")},
    )
    with pytest.raises(RenderError, match="tool not found"):
        render_command(action, tmp_path)


def test_output_glob_in_command_is_error(tmp_path):
    action = ExternalProcessAction(
        name="gen",
        command_line="gen {generated}",
        outputs={"generated": data.glob("out/{n}.txt")},
    )
    with pytest.raises(RenderError, match="cannot be rendered"):
        render_command(action, tmp_path)


def test_prepare_output_dirs(tmp_path):
    action = ExternalProcessAction(
        name="x",
        command_line="x",
        outputs={
            "obj": data.file("build/obj/foo.o"),
            "generated": data.glob("build/generated/{n}.cpp"),
        },
    )
    prepare_output_dirs(action, tmp_path)
    assert (tmp_path / "build/obj").is_dir()
    assert (tmp_path / "build/generated").is_dir()
