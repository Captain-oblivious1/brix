"""Nested ExpandAll (the Expandable interface) and env= on actions."""

import sys

import pytest

from brix import CleanBuilder, ExpandAll, ExternalProcessAction, Graph, IncrementalBuilder, NodeKind

COPY_TOOL = (
    "import sys\n"
    "from pathlib import Path\n"
    "Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text().upper())\n"
)


def nested_graph():
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="video",
            collection=data.glob("videos/{video:*}.py"),
            child=ExpandAll(
                element_name="scene",
                collection=data.glob("videos/{video:*}/{scene:*}.py"),
                child=ExternalProcessAction(
                    name="render {video}/{scene}",
                    command_line="{python} {tool} {source} {out}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "tool": data.file("tools/copy.py"),
                        "source": data.file("videos/{video}/{scene}.py"),
                    },
                    outputs={"out": data.file("build/{video}/{scene}.out")},
                ),
            ),
        )
    )
    return graph


def write_workspace(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/copy.py").write_text(COPY_TOOL)
    for video, scenes in {"alpha": ["01", "02"], "beta": ["01"]}.items():
        (tmp_path / "videos" / video).mkdir(parents=True)
        (tmp_path / "videos" / f"{video}.py").write_text(f"# {video}\n")
        for scene in scenes:
            (tmp_path / "videos" / video / f"{scene}.py").write_text(f"{video} {scene}")


def test_nested_expansion_builds_per_video_per_scene(tmp_path):
    write_workspace(tmp_path)
    result = CleanBuilder(nested_graph(), root=tmp_path).build()
    assert result.success
    assert (tmp_path / "build/alpha/01.out").read_text() == "ALPHA 01"
    assert (tmp_path / "build/alpha/02.out").read_text() == "ALPHA 02"
    assert (tmp_path / "build/beta/01.out").read_text() == "BETA 01"
    # Outer expand -> 2 inner expand nodes -> 3 render nodes.
    kinds = [n.kind for n in result.plan.nodes]
    assert kinds.count(NodeKind.EXPAND) == 3  # outer + one per video
    assert kinds.count(NodeKind.PROCESS) == 3
    labels = {n.label for n in result.plan.nodes}
    assert "expand: render alpha/{scene}" in labels  # outer binding applied
    assert "render alpha/01" in labels


def test_nested_expansion_is_incremental_per_scene(tmp_path):
    write_workspace(tmp_path)
    builder = IncrementalBuilder(nested_graph, root=tmp_path)
    builder.build()
    second = builder.build()
    assert all(r.source == "replayed" for r in second.process_results)

    (tmp_path / "videos/alpha/02.py").write_text("alpha 02 CHANGED")
    third = builder.build()
    ran = [r.action_name for r in third.process_results if r.source == "process"]
    assert ran == ["render alpha/02"]


def test_deleted_video_removes_nested_stale_outputs(tmp_path):
    """Deleting a whole outer member (a video) must clean the outputs of its
    *nested* expansion (every scene), not just a top-level output — matching a
    clean build. The inner expansion never realizes this build (its subtree is
    pruned), so cleanup is driven from the outer collection by reading the
    vanished video's recorded scene membership."""
    import shutil

    write_workspace(tmp_path)
    builder = IncrementalBuilder(nested_graph, root=tmp_path)
    builder.build()
    assert (tmp_path / "build/alpha/01.out").exists()
    assert (tmp_path / "build/alpha/02.out").exists()
    assert (tmp_path / "build/beta/01.out").exists()

    # Remove the entire 'alpha' video: its outer collection entry and its scenes.
    (tmp_path / "videos/alpha.py").unlink()
    shutil.rmtree(tmp_path / "videos/alpha")

    result = builder.build()
    assert result.success
    # Every stale output under the vanished video is gone; the survivor stays.
    assert not (tmp_path / "build/alpha/01.out").exists()
    assert not (tmp_path / "build/alpha/02.out").exists()
    assert (tmp_path / "build/beta/01.out").exists()
    # The deletions were real, visible nodes in the plan.
    rm_labels = [n.label for n in result.plan.nodes if n.label.startswith("rm ")]
    assert any("build/alpha/01.out" in label for label in rm_labels)
    assert any("build/alpha/02.out" in label for label in rm_labels)

    # Settled: the next build has nothing left to delete.
    third = builder.build()
    assert third.success
    assert not any(n.label.startswith("rm ") for n in third.plan.nodes)


def test_nested_regex_collection_excludes_non_scenes(tmp_path):
    """Regex collections substitute outer bindings and filter membership —
    e.g. numbered scene files only, no __init__.py."""
    graph = Graph()
    data = graph.data
    graph.add(
        ExpandAll(
            element_name="video",
            collection=data.glob("videos/{video:*}.py"),
            child=ExpandAll(
                element_name="scene",
                collection=data.regex(r"videos/(?P<video>[^/]+)/(?P<scene>[0-9][^/]*)\.py"),
                child=ExternalProcessAction(
                    name="render {video}/{scene}",
                    command_line="{python} {tool} {source} {out}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "tool": data.file("tools/copy.py"),
                        "source": data.file("videos/{video}/{scene}.py"),
                    },
                    outputs={"out": data.file("build/{video}/{scene}.out")},
                ),
            ),
        )
    )
    write_workspace(tmp_path)
    (tmp_path / "videos/alpha/__init__.py").write_text("")
    (tmp_path / "videos/alpha/01.py.bak").write_text("junk")
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success
    assert (tmp_path / "build/alpha/01.out").exists()
    assert (tmp_path / "build/alpha/02.out").exists()
    assert not (tmp_path / "build/alpha/__init__.out").exists()
    labels = {n.label for n in result.plan.nodes}
    assert "render alpha/01" in labels
    assert "render alpha/__init__" not in labels


def test_non_expandable_child_rejected():
    with pytest.raises(TypeError, match="Expandable"):
        ExpandAll(element_name="x", collection=None, child="not an action")


def test_env_is_passed_to_process(tmp_path):
    graph = Graph()
    data = graph.data
    graph.add(
        ExternalProcessAction(
            name="show env",
            command_line="{python} -c {code}",
            inputs={
                "python": data.tool(sys.executable),
                "code": data.value(
                    "'import os; open(\"out.txt\", \"w\").write(os.environ[\"BRIX_GREETING\"])'"
                ),
            },
            outputs={"out": data.file("out.txt")},
            env={"BRIX_GREETING": "hello env"},  # plain strings become values
        )
    )
    result = CleanBuilder(graph, root=tmp_path).build()
    assert result.success
    assert (tmp_path / "out.txt").read_text() == "hello env"


def test_env_change_refingerprints(tmp_path):
    from brix import DataFactory  # noqa: F401  (factory via graph below)

    def graph_with_env(value: str):
        def supplier():
            graph = Graph()
            data = graph.data
            graph.add(
                ExternalProcessAction(
                    name="gen",
                    command_line="{python} -c {code}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "code": data.value(
                            "'import os,pathlib; pathlib.Path(\"runs.log\").open(\"a\").write(\"x\");"
                            " pathlib.Path(\"out.txt\").write_text(os.environ[\"MODE\"])'"
                        ),
                    },
                    outputs={"out": data.file("out.txt")},
                    env={"MODE": value},
                )
            )
            return graph

        return supplier

    IncrementalBuilder(graph_with_env("fast"), root=tmp_path).build()
    assert (tmp_path / "out.txt").read_text() == "fast"
    IncrementalBuilder(graph_with_env("slow"), root=tmp_path).build()
    assert (tmp_path / "out.txt").read_text() == "slow"
    IncrementalBuilder(graph_with_env("slow"), root=tmp_path).build()
    assert (tmp_path / "runs.log").read_text() == "xx"  # third build was fresh
