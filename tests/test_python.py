"""brix.python.PytestAction lowering shape."""

from brix import ExpandAll, Graph, python
from brix.data import DataFactory

data = DataFactory()


def test_pytest_action_expands_per_test_file_with_a_manifest_depfile():
    graph = Graph()
    graph.add(
        python.PytestAction(
            test_sources=data.glob("tests/test_{name}.py"),
            python=data.tool("venv/bin/python"),
            manifest_dir="build/deps",
            options=["-q"],
        )
    )
    (expand,) = graph.actions
    assert isinstance(expand, ExpandAll)
    assert expand.element_name == "name"
    assert expand.collection.pattern.template == "tests/test_{name}.py"

    run = expand.child
    # One pytest run per file, wrapped with the brix plugin, writing a JSON
    # manifest that is the run's depfile (the discovered-import footprint).
    assert "-m pytest {testfile}" in run.command_line
    assert "-p brix._python.pytest_brixdeps" in run.command_line
    assert run.depfile == "build/deps/{name}.json"
    assert run.outputs["manifest"].path == "build/deps/{name}.json"
    assert run.inputs["testfile"].pattern.template == "tests/test_{name}.py"


def test_pytest_action_requires_a_single_binding():
    import pytest

    with pytest.raises(ValueError, match="exactly one"):
        python.PytestAction(test_sources=data.glob("tests/test_all.py"))
