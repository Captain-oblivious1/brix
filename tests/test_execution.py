"""End-to-end execution tests using small Python helper tools."""

import sys
import textwrap

from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph
from brix.plan import NodeState

UPPER_TOOL = textwrap.dedent(
    """
    import sys
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        text = f.read()
    with open(dst, "w") as f:
        f.write(text.upper())
    """
)

CONCAT_TOOL = textwrap.dedent(
    """
    import sys
    *srcs, dst = sys.argv[1:]
    with open(dst, "w") as out:
        for s in srcs:
            with open(s) as f:
                out.write(f.read())
    """
)

GENERATE_TOOL = textwrap.dedent(
    """
    import os, sys
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    for name in sys.argv[2:]:
        with open(os.path.join(out_dir, name + ".txt"), "w") as f:
            f.write(name + "\\n")
    """
)


def write_workspace(tmp_path, sources: dict[str, str]):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/upper.py").write_text(UPPER_TOOL)
    (tmp_path / "tools/concat.py").write_text(CONCAT_TOOL)
    (tmp_path / "tools/generate.py").write_text(GENERATE_TOOL)
    (tmp_path / "src").mkdir()
    for name, content in sources.items():
        (tmp_path / "src" / name).write_text(content)


def upper_then_concat_graph() -> Graph:
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": data.file("build/all.txt")},
        ),
    )
    return graph


def test_expand_and_build(tmp_path):
    write_workspace(tmp_path, {"a.txt": "hello ", "b.txt": "world"})
    result = CleanBuilder(upper_then_concat_graph(), root=tmp_path).build()
    assert result.success
    assert (tmp_path / "build/upper/a.txt").read_text() == "HELLO "
    assert (tmp_path / "build/upper/b.txt").read_text() == "WORLD"
    assert (tmp_path / "build/all.txt").read_text() == "HELLO WORLD"
    # ExpandAll node plus two expanded children plus combine.
    assert len(result.plan.nodes) == 4
    assert all(result.states.status(n.id) is NodeState.DONE for n in result.plan.nodes)


def test_expansion_is_initially_unrealized(tmp_path):
    write_workspace(tmp_path, {"a.txt": "x"})
    builder = CleanBuilder(upper_then_concat_graph(), root=tmp_path)
    plan = builder.plan()
    assert len(plan.nodes) == 2  # expand point and combine; no concrete compiles yet


def test_generated_files_expand_at_runtime(tmp_path):
    """The collection driving ExpandAll does not exist until a generator runs."""
    write_workspace(tmp_path, {})
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="generate sources",
            command_line="{python} {tool} {out_dir} alpha beta",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/generate.py"),
                "out_dir": data.value("build/generated"),
            },
            outputs={"generated": data.glob("build/generated/{name}.txt")},
        ),
        ExpandAll(
            element_name="name",
            collection=data.glob("build/generated/{name}.txt"),
            child=ExternalProcessAction(
                name="upper generated {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("build/generated/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": data.file("build/all.txt")},
        ),
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "ALPHA\nBETA\n"


def test_glob_exclusion_narrows_expansion_and_inputs(tmp_path):
    # `- "src/_*.txt"` drops members from both the expansion collection and
    # the combine's input glob; the excluded file is never uppercased and
    # never concatenated.
    write_workspace(tmp_path, {"a.txt": "keep ", "_b.txt": "drop", "c.txt": "also"})
    graph = Graph()
    data = graph.data
    sources = data.glob("src/{name}.txt") - "src/_*.txt"
    graph.add(
        ExpandAll(
            element_name="name",
            collection=sources,
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        ),
        ExternalProcessAction(
            name="combine",
            command_line="{python} {tool} {parts} {combined}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/concat.py"),
                "parts": data.glob("build/upper/{name}.txt"),
            },
            outputs={"combined": data.file("build/all.txt")},
        ),
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success, result.errors
    assert not (tmp_path / "build/upper/_b.txt").exists()
    assert (tmp_path / "build/all.txt").read_text() == "KEEP ALSO"


def test_stdout_stderr_exit_code_captured(tmp_path):
    graph = Graph()
    data = graph.data
    code = "import sys; print('to out'); print('to err', file=sys.stderr)"
    graph.add(
        ExternalProcessAction(
            name="speak",
            command_line='{python} -c "{code}"',
            inputs={"python": data.tool(sys.executable), "code": data.value(code)},
        )
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success
    (proc,) = result.process_results
    assert proc.stdout == "to out\n"
    assert proc.stderr == "to err\n"
    assert proc.exit_code == 0


def test_failure_is_structured_and_skips_downstream(tmp_path):
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="fail",
            command_line="{python} -c {code}",
            inputs={
                "python": data.tool(sys.executable),
                "code": data.value("'import sys; sys.exit(3)'"),
            },
            outputs={"out": data.file("build/fail.txt")},
        ),
        ExternalProcessAction(
            name="downstream",
            command_line="{python} -c {code}",
            inputs={
                "python": data.tool(sys.executable),
                "code": data.value("'print(1)'"),
                "needs": data.file("build/fail.txt"),
            },
        ),
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert not result.success
    fail_node, downstream_node = result.plan.nodes
    assert result.states.status(fail_node.id) is NodeState.FAILED
    assert result.states.result(fail_node.id).exit_code == 3
    assert result.states.status(downstream_node.id) is NodeState.SKIPPED
    assert result.states.result(downstream_node.id) is None
    assert any("exit 3" in e for e in result.errors)


def test_independent_branch_still_builds_on_failure(tmp_path):
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="fail",
            command_line="{python} -c {code}",
            inputs={
                "python": data.tool(sys.executable),
                "code": data.value("'import sys; sys.exit(1)'"),
            },
        ),
        ExternalProcessAction(
            name="fine",
            command_line="{python} {tool} {source} {dest}",
            inputs={
                "python": data.tool(sys.executable),
                "tool": data.file("tools/upper.py"),
                "source": data.file("src/a.txt"),
            },
            outputs={"dest": data.file("build/a.txt")},
        ),
    )
    write_workspace(tmp_path, {"a.txt": "ok"})
    result = CleanBuilder(graph, root=tmp_path).build()
    assert not result.success
    assert (tmp_path / "build/a.txt").read_text() == "OK"


def test_empty_collection_expands_to_nothing(tmp_path):
    write_workspace(tmp_path, {})
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="name",
            collection=data.glob("src/{name}.txt"),
            child=ExternalProcessAction(
                name="upper {name}",
                command_line="{python} {tool} {source} {dest}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "source": data.file("src/{name}.txt"),
                },
                outputs={"dest": data.file("build/upper/{name}.txt")},
            ),
        )
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success
    assert len(result.plan.nodes) == 1


def test_dependency_cycle_reported(tmp_path):
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="a",
            command_line="a",
            inputs={"in": data.file("x.txt")},
            outputs={"out": data.file("y.txt")},
        ),
        ExternalProcessAction(
            name="b",
            command_line="b",
            inputs={"in": data.file("y.txt")},
            outputs={"out": data.file("x.txt")},
        ),
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert not result.success
    assert any("cycle" in e for e in result.errors)


def render_combine_finalize_graph() -> Graph:
    """The animation shape: render scenes per video, combine each video's
    scenes, finalize each video — three expansions over one `videos`
    collection, with bare (recursive) `{video}` bindings throughout so a
    video may live in a nested directory (`rights/intro`)."""
    graph = Graph()
    data = graph.data
    videos = data.glob("src/videos/{video}.py")
    graph.add(
        ExpandAll(
            element_name="video",
            collection=videos,
            child=ExpandAll(
                element_name="scene",
                collection=data.regex(r"src/scenes/(?P<video>.+)/(?P<scene>[0-9][^/]*)\.py"),
                child=ExternalProcessAction(
                    name="render {video}/{scene}",
                    command_line="{python} {tool} {source} {mpg}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "tool": data.file("tools/upper.py"),
                        "source": data.file("src/scenes/{video}/{scene}.py"),
                    },
                    outputs={"mpg": data.file("out/videos/{video}/{scene}.mpg")},
                ),
            ),
        ),
        ExpandAll(
            element_name="video",
            collection=videos,
            child=ExternalProcessAction(
                name="combine {video}",
                command_line="{python} {tool} {scenes} {raw}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/concat.py"),
                    "scenes": data.glob("out/videos/{video}/{scene}.mpg"),
                },
                outputs={"raw": data.file("out/videos/{video}.raw.mpg")},
            ),
        ),
        ExpandAll(
            element_name="video",
            collection=videos,
            child=ExternalProcessAction(
                name="finalize {video}",
                command_line="{python} {tool} {raw} {mpg}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/upper.py"),
                    "raw": data.file("out/videos/{video}.raw.mpg"),
                },
                outputs={"mpg": data.file("out/videos/{video}.mpg")},
            ),
        ),
    )
    return graph


def test_same_collection_expansions_build_end_to_end(tmp_path):
    # Would deadlock as a false combine <-> finalize cycle if the planner
    # compared their recursive {video}s independently (see test_plan.py);
    # must build clean, nested video directory included.
    write_workspace(tmp_path, {})
    (tmp_path / "src/videos/rights").mkdir(parents=True)
    (tmp_path / "src/videos/wealth.py").write_text("")
    (tmp_path / "src/videos/rights/intro.py").write_text("")
    (tmp_path / "src/scenes/wealth").mkdir(parents=True)
    (tmp_path / "src/scenes/rights/intro").mkdir(parents=True)
    (tmp_path / "src/scenes/wealth/01_a.py").write_text("alpha ")
    (tmp_path / "src/scenes/wealth/02_b.py").write_text("beta")
    (tmp_path / "src/scenes/rights/intro/01_x.py").write_text("xray")

    result = CleanBuilder(render_combine_finalize_graph(), root=tmp_path).build()
    assert result.success, result.errors
    assert (tmp_path / "out/videos/wealth.mpg").read_text() == "ALPHA BETA"
    assert (tmp_path / "out/videos/rights/intro.mpg").read_text() == "XRAY"
    assert all(result.states.status(n.id) is NodeState.DONE for n in result.plan.nodes)
