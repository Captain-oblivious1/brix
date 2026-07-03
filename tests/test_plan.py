from pathlib import Path

from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph, NodeKind


def make_compile_link_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.cpp"),
            child=ExternalProcessAction(
                name="compile {name}",
                command_line="{compiler} -c {source} -o {object}",
                inputs={
                    "compiler": data.tool("c++"),
                    "source": data.file("src/{name}.cpp"),
                },
                outputs={"object": data.file("build/obj/{name}.o")},
            ),
        ),
        ExternalProcessAction(
            name="link app",
            command_line="{compiler} {objects} -o {app}",
            inputs={
                "compiler": data.tool("c++"),
                "objects": data.glob("build/obj/{name}.o"),
            },
            outputs={"app": data.file("build/bin/app")},
        ),
    )
    return graph


def test_plan_nodes_and_kinds():
    plan = CleanBuilder(make_compile_link_graph()).plan()
    assert len(plan.nodes) == 2
    assert plan.nodes[0].kind is NodeKind.EXPAND
    assert plan.nodes[1].kind is NodeKind.PROCESS


def test_plan_infers_expand_to_link_edge():
    plan = CleanBuilder(make_compile_link_graph()).plan()
    assert any(e.from_id == 0 and e.to_id == 1 for e in plan.edges)
    assert not any(e.from_id == 1 and e.to_id == 0 for e in plan.edges)


def test_plan_infers_generator_to_expand_edge():
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="generate",
            command_line="gen {out_dir}",
            inputs={"out_dir": data.value("build/generated")},
            outputs={"generated": data.glob("build/generated/{name}.cpp")},
        ),
        ExpandAll(
            element_name="name",
            collection=data.glob("build/generated/{name}.cpp"),
            child=ExternalProcessAction(
                name="compile {name}",
                command_line="cc -c {source} -o {object}",
                inputs={"source": data.file("build/generated/{name}.cpp")},
                outputs={"object": data.file("build/obj/{name}.o")},
            ),
        ),
    )
    plan = CleanBuilder(graph).plan()
    assert any(e.from_id == 0 and e.to_id == 1 for e in plan.edges)


def test_plan_no_edge_between_unrelated_actions():
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="a",
            command_line="a",
            inputs={"in": data.file("one.txt")},
            outputs={"out": data.file("one.out")},
        ),
        ExternalProcessAction(
            name="b",
            command_line="b",
            inputs={"in": data.file("two.txt")},
            outputs={"out": data.file("two.out")},
        ),
    )
    plan = CleanBuilder(graph).plan()
    assert plan.edges == []


def test_describe_mentions_labels():
    plan = CleanBuilder(make_compile_link_graph()).plan()
    text = plan.describe()
    assert "compile {name}" in text
    assert "link app" in text


def test_builder_root_resolved(tmp_path):
    builder = CleanBuilder(Graph(), root=tmp_path)
    assert builder.root == Path(tmp_path).resolve()
