"""Target selection: build only what produces the requested outputs."""

import pytest

from brix import CleanBuilder, IncrementalBuilder, cli

from test_incremental import make_graph, runs, write_workspace
from test_nested_expand import nested_graph, write_workspace as write_nested


def test_target_intermediate_skips_downstream(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    result = CleanBuilder(make_graph(), root=tmp_path).build(
        targets=["build/upper/a.txt"]
    )
    assert result.success
    assert (tmp_path / "build/upper/a.txt").exists()
    assert not (tmp_path / "build/upper/b.txt").exists()  # not needed
    assert not (tmp_path / "build/all.txt").exists()  # downstream not requested
    counts = runs(tmp_path)
    assert counts == {"upper:src/a.txt": 1}


def test_target_final_output_pulls_in_everything_needed(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    result = CleanBuilder(make_graph(), root=tmp_path).build(targets=["build/all.txt"])
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "XY"


def test_binding_propagation_through_nested_expansion(tmp_path):
    """Targeting one video's output must not even realize the others."""
    write_nested(tmp_path)
    result = CleanBuilder(nested_graph(), root=tmp_path).build(
        targets=["build/alpha/02.out"]
    )
    assert result.success
    assert (tmp_path / "build/alpha/02.out").read_text() == "ALPHA 02"
    assert not (tmp_path / "build/alpha/01.out").exists()
    assert not (tmp_path / "build/beta/01.out").exists()
    # The plan only ever contained the constrained chain.
    labels = {n.label for n in result.plan.nodes}
    assert "render alpha/02" in labels
    assert "render alpha/01" not in labels
    assert not any("beta" in label for label in labels)


def test_two_targets_two_constrained_instances(tmp_path):
    write_nested(tmp_path)
    result = CleanBuilder(nested_graph(), root=tmp_path).build(
        targets=["build/alpha/01.out", "build/beta/01.out"]
    )
    assert result.success
    assert (tmp_path / "build/alpha/01.out").exists()
    assert (tmp_path / "build/beta/01.out").exists()
    assert not (tmp_path / "build/alpha/02.out").exists()


def test_targeted_and_full_builds_share_incremental_records(tmp_path):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()  # full
    after_full = runs(tmp_path)
    second = builder.build(targets=["build/upper/a.txt"])
    assert second.success
    assert runs(tmp_path) == after_full  # targeted rebuild replayed
    assert all(r.source == "replayed" for r in second.process_results)


def test_suffix_pipeline_terminates_via_shortest_stem(tmp_path):
    """{video}.mpg also matches wealth.raw.mpg (video='wealth.raw'), which
    without make's shortest-stem rule manufactures wealth.raw.raw... needs
    forever. Regression from the animation project."""
    import sys

    from brix import ExpandAll, ExternalProcessAction, Graph

    graph = Graph()
    data = graph.data
    copy = "'import sys,shutil; shutil.copy(sys.argv[1], sys.argv[2])'"
    graph.add(
        ExpandAll(
            element_name="video",
            collection=data.glob("src/{video}.txt"),
            child=ExternalProcessAction(
                name="combine {video}",
                command_line="{python} -c {code} {src} {raw}",
                inputs={
                    "python": data.tool(sys.executable),
                    "code": data.value(copy),
                    "src": data.file("src/{video}.txt"),
                },
                outputs={"raw": data.file("out/{video}.raw.mpg")},
            ),
        ),
        ExpandAll(
            element_name="video",
            collection=data.glob("src/{video}.txt"),
            child=ExternalProcessAction(
                name="finalize {video}",
                command_line="{python} -c {code} {raw} {final}",
                inputs={
                    "python": data.tool(sys.executable),
                    "code": data.value(copy),
                    "raw": data.file("out/{video}.raw.mpg"),
                },
                outputs={"final": data.file("out/{video}.mpg")},
            ),
        ),
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/wealth.txt").write_text("w")
    (tmp_path / "src/other.txt").write_text("o")
    result = CleanBuilder(graph, root=tmp_path).build(targets=["out/wealth.mpg"])
    assert result.success
    assert (tmp_path / "out/wealth.mpg").exists()
    assert not (tmp_path / "out/other.mpg").exists()
    labels = {n.label for n in result.plan.nodes}
    assert labels == {"combine wealth", "finalize wealth"}


def test_unknown_target_is_an_error(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    with pytest.raises(ValueError, match="no action produces"):
        CleanBuilder(make_graph(), root=tmp_path).build(targets=["no/such/output.bin"])


def test_cli_targets_and_aliases(tmp_path, capsys):
    write_workspace(tmp_path, {"a": "x", "b": "y"})
    aliases = {"a": "build/upper/a.txt", "everything": "build/all.txt"}

    code = cli.main(
        make_graph, root=tmp_path, argv=["build", "--silent", "a"], aliases=aliases
    )
    assert code == 0
    assert (tmp_path / "build/upper/a.txt").exists()
    assert not (tmp_path / "build/all.txt").exists()

    # Targeted status only reports the constrained subgraph.
    assert cli.main(make_graph, root=tmp_path, argv=["status", "a"], aliases=aliases) == 0
    out = capsys.readouterr().out
    assert "upper a" in out
    assert "combine" not in out

    # Unknown target: clear error, exit 2.
    assert cli.main(make_graph, root=tmp_path, argv=["build", "nope.bin"]) == 2
    assert "no action produces" in capsys.readouterr().err
