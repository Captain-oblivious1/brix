from pathlib import Path

import pytest

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


def test_same_collection_expansions_do_not_false_cycle():
    # The animation shape: combine and finalize both expand one `videos`
    # collection. finalize's output out/{video}.mpg overlaps combine's input
    # out/{video}/{scene}.mpg only by splitting a recursive {video} two ways
    # (video="a/b" vs video="a", scene="b") — impossible when both iterate
    # the same member, so the planner must keep combine -> finalize (the
    # .raw.mpg handoff) and not invent the reverse edge.
    graph = Graph()
    data = graph.data
    videos = data.glob("src/{video}.py")
    graph.add(
        ExpandAll(
            element_name="video",
            collection=videos,
            child=ExternalProcessAction(
                name="combine {video}",
                command_line="combine {scenes} {raw}",
                inputs={"scenes": data.glob("out/{video}/{scene}.mpg")},
                outputs={"raw": data.file("out/{video}.raw.mpg")},
            ),
        ),
        ExpandAll(
            element_name="video",
            collection=videos,
            child=ExternalProcessAction(
                name="finalize {video}",
                command_line="finalize {raw} {mpg}",
                inputs={"raw": data.file("out/{video}.raw.mpg")},
                outputs={"mpg": data.file("out/{video}.mpg")},
            ),
        ),
    )
    plan = CleanBuilder(graph).plan()
    assert any(e.from_id == 0 and e.to_id == 1 for e in plan.edges)  # combine -> finalize
    assert not any(e.from_id == 1 and e.to_id == 0 for e in plan.edges)


def test_different_collections_keep_the_conservative_conflict():
    # Same shape, but the expansions iterate *different* collections: their
    # {video}s are independent values, so the conservative overlap (and the
    # cycle it implies) must stay — sharing is only inferred, never assumed.
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="video",
            collection=data.glob("src/{video}.py"),
            child=ExternalProcessAction(
                name="combine {video}",
                command_line="combine {scenes} {raw}",
                inputs={"scenes": data.glob("out/{video}/{scene}.mpg")},
                outputs={"raw": data.file("out/{video}.raw.mpg")},
            ),
        ),
        ExpandAll(
            element_name="video",
            collection=data.glob("other/{video}.toml"),
            child=ExternalProcessAction(
                name="finalize {video}",
                command_line="finalize {raw} {mpg}",
                inputs={"raw": data.file("out/{video}.raw.mpg")},
                outputs={"mpg": data.file("out/{video}.mpg")},
            ),
        ),
    )
    # The independent {video} values also make the two output patterns
    # conservatively overlap, so unique ownership now rejects this graph
    # before its old inferred cycle can reach the scheduler.
    with pytest.raises(ValueError, match="conflicting outputs"):
        CleanBuilder(graph).plan()


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


def _writer(name, output):
    return ExternalProcessAction(
        name=name,
        command_line="write {out}",
        outputs={"out": output},
    )


def test_plan_rejects_same_concrete_output():
    graph = Graph()
    graph.add(
        _writer("first", graph.data.file("build/result.txt")),
        _writer("second", graph.data.file("build/result.txt")),
    )
    with pytest.raises(
        ValueError,
        match=(
            r"conflicting outputs: 'first' and 'second' may both write "
            r"build/result\.txt; every output path must have exactly one owner"
        ),
    ):
        CleanBuilder(graph).plan()


def test_plan_rejects_pattern_and_concrete_output():
    graph = Graph()
    graph.add(
        _writer("many", graph.data.glob("build/{name}.txt")),
        _writer("one", graph.data.file("build/result.txt")),
    )
    with pytest.raises(ValueError, match=r"build/\{name\}\.txt ~ build/result\.txt"):
        CleanBuilder(graph).plan()


def test_plan_rejects_overlapping_output_patterns():
    graph = Graph()
    graph.add(
        _writer("text", graph.data.glob("build/{name}.txt")),
        _writer("all", graph.data.glob("build/{file}")),
    )
    with pytest.raises(ValueError, match="conflicting outputs"):
        CleanBuilder(graph).plan()


def test_plan_allows_non_overlapping_outputs():
    graph = Graph()
    graph.add(
        _writer("text", graph.data.glob("build/{name}.txt")),
        _writer("objects", graph.data.glob("build/{name}.o")),
    )
    assert len(CleanBuilder(graph).plan().nodes) == 2


def test_plan_rejects_conflicting_expanded_outputs():
    graph = Graph()
    sources = graph.data.glob("src/{name}.txt")
    graph.add(
        ExpandAll(
            element_name="name",
            collection=sources,
            child=_writer("first {name}", graph.data.file("build/{name}.out")),
        ),
        ExpandAll(
            element_name="name",
            collection=sources,
            child=_writer("second {name}", graph.data.file("build/{name}.out")),
        ),
    )
    with pytest.raises(ValueError, match="conflicting outputs"):
        CleanBuilder(graph).plan()


def test_describe_mentions_labels():
    plan = CleanBuilder(make_compile_link_graph()).plan()
    text = plan.describe()
    assert "compile {name}" in text
    assert "link app" in text


def test_builder_root_resolved(tmp_path):
    builder = CleanBuilder(Graph(), root=tmp_path)
    assert builder.root == Path(tmp_path).resolve()
