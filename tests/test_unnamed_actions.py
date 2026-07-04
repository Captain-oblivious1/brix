import sys

from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph
from brix.data import DataFactory

data = DataFactory()


def test_label_falls_back_to_command_line():
    action = ExternalProcessAction(command_line="cc -c {source} -o {object}")
    assert action.name is None
    assert action.label == "cc -c {source} -o {object}"


def test_explicit_name_wins():
    action = ExternalProcessAction(name="compile", command_line="cc")
    assert action.label == "compile"


def test_substitute_keeps_name_none():
    action = ExternalProcessAction(
        command_line="cp {source} {dest}",
        inputs={"source": data.file("src/{name}.txt")},
        outputs={"dest": data.file("out/{name}.txt")},
    )
    concrete = action.substitute({"name": "foo"})
    assert concrete.name is None
    assert concrete.inputs["source"].path == "src/foo.txt"


def test_unnamed_actions_build(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.txt").write_text("hi")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/upper.py").write_text(
        "import sys\n"
        "with open(sys.argv[1]) as f: text = f.read()\n"
        "with open(sys.argv[2], 'w') as f: f.write(text.upper())\n"
    )
    graph = Graph()
    d = graph.data
    graph.add(
        ExpandAll(
            element_name="name",
            collection=d.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": d.tool(sys.executable),
                    "tool": d.file("tools/upper.py"),
                    "source": d.file("src/{name}.txt"),
                },
                outputs={"dest": d.file("build/{name}.txt")},
            ),
        )
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success
    assert (tmp_path / "build/a.txt").read_text() == "HI"
    (proc,) = result.process_results
    assert proc.action_name == "{python} {tool} {source} {dest}"
