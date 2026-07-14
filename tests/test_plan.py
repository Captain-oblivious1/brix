from pathlib import Path
import sys

import pytest

from brix import CleanBuilder, EdgeTrigger, ExpandAll, ExternalProcessAction, Graph, NodeKind


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


def test_force_order_adds_explicit_edge():
    graph = Graph()
    setup = _writer("setup", graph.data.file("setup.done"))
    tests = _writer("tests", graph.data.file("tests.done"))
    graph.add(setup, tests).force_order(setup, tests)
    plan = CleanBuilder(graph).plan()
    edge = next(e for e in plan.edges if e.reason == "explicit ordering")
    assert (edge.from_id, edge.to_id) == (0, 1)
    assert edge.trigger is EdgeTrigger.ON_SUCCESS
    assert "explicit ordering" in plan.describe()


def test_force_order_supports_on_completion():
    graph = Graph()
    setup = _writer("setup", graph.data.file("setup.done"))
    cleanup = _writer("cleanup", graph.data.file("cleanup.done"))
    graph.add(setup, cleanup).force_order(
        setup, cleanup, trigger=EdgeTrigger.ON_COMPLETION
    )
    edge = CleanBuilder(graph).plan().edges[0]
    assert edge.trigger is EdgeTrigger.ON_COMPLETION


def test_force_order_target_pulls_in_predecessor():
    graph = Graph()
    setup = _writer("setup", graph.data.file("setup.done"))
    tests = _writer("tests", graph.data.file("tests.done"))
    unrelated = _writer("unrelated", graph.data.file("other.done"))
    graph.add(setup, tests, unrelated).force_order(setup, tests)
    plan = CleanBuilder(graph).plan(targets=["tests.done"])
    assert [node.label for node in plan.nodes] == ["setup", "tests"]
    assert any(e.reason == "explicit ordering" for e in plan.edges)


def test_force_order_requires_registered_distinct_actions():
    graph = Graph()
    setup = _writer("setup", graph.data.file("setup.done"))
    tests = _writer("tests", graph.data.file("tests.done"))
    graph.add(setup)
    with pytest.raises(ValueError, match="must each be added"):
        graph.force_order(setup, tests)
    with pytest.raises(ValueError, match="two different"):
        graph.force_order(setup, setup)
    with pytest.raises(TypeError, match="EdgeTrigger"):
        graph.force_order(setup, setup, trigger="on-completion")


@pytest.mark.parametrize(
    ("trigger", "after_runs"),
    [(EdgeTrigger.ON_SUCCESS, False), (EdgeTrigger.ON_COMPLETION, True)],
)
def test_force_order_trigger_behavior_through_default_engine(tmp_path, trigger, after_runs):
    graph = Graph()
    failed = ExternalProcessAction(
        name="setup", command_line=f"{sys.executable} -c 'raise SystemExit(1)'"
    )
    marker = tmp_path / "after-ran"
    script = tmp_path / "after.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    after = ExternalProcessAction(
        name="after",
        command_line="{python} {script}",
        inputs={
            "python": graph.data.tool(sys.executable),
            "script": graph.data.file("after.py"),
        },
    )
    graph.add(failed, after).force_order(failed, after, trigger=trigger)
    result = CleanBuilder(graph, root=tmp_path).build()
    assert not result.success
    assert marker.exists() is after_runs


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
