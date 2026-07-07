"""IncrementalBuilder: fingerprint-fresh nodes complete without processes."""

import json
import sys
from collections import Counter

from brix import IncrementalBuilder, NodeState

UPPER_TOOL = """\
import sys
from pathlib import Path
Path("runs.log").open("a").write("upper:" + sys.argv[1] + "\\n")
text = Path(sys.argv[1]).read_text()
Path(sys.argv[2]).write_text(text.upper())
print("uppercased", sys.argv[1])
"""

CONCAT_TOOL = """\
import sys
from pathlib import Path
Path("runs.log").open("a").write("combine\\n")
parts = [Path(p).read_text() for p in sys.argv[1:-1]]
Path(sys.argv[-1]).write_text("".join(parts))
"""


def make_graph():
    from brix import ExpandAll, ExternalProcessAction, Graph

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


def write_workspace(tmp_path, sources):
    (tmp_path / "src").mkdir(exist_ok=True)
    for name, text in sources.items():
        (tmp_path / "src" / f"{name}.txt").write_text(text)
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools/upper.py").write_text(UPPER_TOOL)
    (tmp_path / "tools/concat.py").write_text(CONCAT_TOOL)


def runs(tmp_path) -> Counter:
    log = tmp_path / "runs.log"
    if not log.exists():
        return Counter()
    return Counter(log.read_text().splitlines())


def test_second_build_runs_nothing(tmp_path):
    write_workspace(tmp_path, {"a": "hi ", "b": "there"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)

    first = builder.build()
    assert first.success
    assert (tmp_path / "build/all.txt").read_text() == "HI THERE"
    after_first = runs(tmp_path)
    assert after_first == Counter(
        {"upper:src/a.txt": 1, "upper:src/b.txt": 1, "combine": 1}
    )

    second = builder.build()
    assert second.success
    assert runs(tmp_path) == after_first  # no process ran
    assert all(
        second.states.status(n.id) is NodeState.DONE for n in second.plan.nodes
    )


def test_replayed_result_carries_previous_stdout(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()
    second = builder.build()
    replayed = [r for r in second.process_results if "uppercased" in r.stdout]
    assert replayed, "previous stdout should replay on skip"


def test_changed_source_reruns_only_affected(tmp_path):
    write_workspace(tmp_path, {"a": "one", "b": "two"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()

    (tmp_path / "src/a.txt").write_text("ONE CHANGED")
    result = builder.build()
    assert result.success
    counts = runs(tmp_path)
    assert counts["upper:src/a.txt"] == 2  # reran
    assert counts["upper:src/b.txt"] == 1  # skipped
    assert counts["combine"] == 2  # its input content changed
    assert "ONE CHANGED" in (tmp_path / "build/all.txt").read_text()


def test_deleted_output_rebuilds_but_downstream_stays_fresh(tmp_path):
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()

    (tmp_path / "build/upper/a.txt").unlink()
    result = builder.build()
    assert result.success
    counts = runs(tmp_path)
    assert counts["upper:src/a.txt"] == 2  # output was gone, must rerun
    assert counts["upper:src/b.txt"] == 1
    # The rerun produced byte-identical output, so combine stayed fresh.
    assert counts["combine"] == 1
    assert (tmp_path / "build/upper/a.txt").read_text() == "AA"


def test_changed_value_input_reruns(tmp_path):
    from brix import ExternalProcessAction, Graph

    def graph_with_flag(flag: str):
        def supplier():
            graph = Graph()
            data = graph.data
            graph.add(
                ExternalProcessAction(
                    name="gen",
                    command_line="{python} {tool} {flag} {out}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "tool": data.file("tools/gen.py"),
                        "flag": data.value(flag),
                    },
                    outputs={"out": data.file("build/out.txt")},
                )
            )
            return graph

        return supplier

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/gen.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        'Path("runs.log").open("a").write("gen\\n")\n'
        "Path(sys.argv[2]).write_text(sys.argv[1])\n"
    )

    IncrementalBuilder(graph_with_flag("-O2"), root=tmp_path).build()
    assert (tmp_path / "build/out.txt").read_text() == "-O2"
    assert runs(tmp_path)["gen"] == 1

    # Same command shape, different value: fingerprint changes, key does not.
    IncrementalBuilder(graph_with_flag("-O3"), root=tmp_path).build()
    assert (tmp_path / "build/out.txt").read_text() == "-O3"
    assert runs(tmp_path)["gen"] == 2

    IncrementalBuilder(graph_with_flag("-O3"), root=tmp_path).build()
    assert runs(tmp_path)["gen"] == 2  # fresh again


def test_failed_action_outputs_removed_and_not_replayed(tmp_path):
    from brix import ExternalProcessAction, Graph

    def make():
        graph = Graph()
        data = graph.data
        graph.add(
            ExternalProcessAction(
                name="half-write then fail",
                command_line="{python} {tool} {out}",
                inputs={
                    "python": data.tool(sys.executable),
                    "tool": data.file("tools/fail.py"),
                },
                outputs={"out": data.file("build/out.txt")},
            )
        )
        return graph

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/fail.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        'Path("runs.log").open("a").write("fail\\n")\n'
        'Path(sys.argv[1]).write_text("partial garbage")\n'
        "raise SystemExit(2)\n"
    )

    builder = IncrementalBuilder(make, root=tmp_path)
    first = builder.build()
    assert not first.success
    # A clean build would not have produced this output; neither may we.
    assert not (tmp_path / "build/out.txt").exists()

    second = builder.build()
    assert not second.success
    assert runs(tmp_path)["fail"] == 2  # failure was never treated as fresh


def test_deleted_source_removes_stale_output_and_recombines(tmp_path):
    write_workspace(tmp_path, {"a": "aa", "b": "bb", "c": "cc"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()
    assert (tmp_path / "build/all.txt").read_text() == "AABBCC"

    (tmp_path / "src/c.txt").unlink()
    result = builder.build()
    assert result.success
    # The stale output is gone and the combined output matches a clean build.
    assert not (tmp_path / "build/upper/c.txt").exists()
    assert (tmp_path / "build/all.txt").read_text() == "AABB"
    # The deletion was a real, visible node in the plan.
    rm_nodes = [n for n in result.plan.nodes if n.label.startswith("rm ")]
    assert len(rm_nodes) == 1
    assert "build/upper/c.txt" in rm_nodes[0].label
    # Surviving members stayed fresh; only the combine reran.
    counts = runs(tmp_path)
    assert counts["upper:src/a.txt"] == 1
    assert counts["upper:src/b.txt"] == 1
    assert counts["combine"] == 2

    # Next build: membership history is settled, nothing to delete or run.
    third = builder.build()
    assert third.success
    assert not any(n.label.startswith("rm ") for n in third.plan.nodes)
    assert runs(tmp_path) == counts


def test_readded_source_rebuilds_without_deletion(tmp_path):
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()
    (tmp_path / "src/b.txt").unlink()
    builder.build()
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})  # b comes back
    result = builder.build()
    assert result.success
    assert not any(n.label.startswith("rm ") for n in result.plan.nodes)
    assert (tmp_path / "build/all.txt").read_text() == "AABB"


def test_metadata_file_is_written(tmp_path):
    write_workspace(tmp_path, {"a": "x"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()
    metadata_path = tmp_path / "build/.brix/metadata.json"
    assert metadata_path.exists()
    stored = json.loads(metadata_path.read_text())
    assert stored["actions"], "action records should be persisted"


# -- "never make a stale hit" invariants -------------------------------------
#
# These pin the completeness of fingerprint invalidation: any change that could
# affect an output must re-stale its action (and only content, not timestamps,
# decides downstream freshness). They are the production-safety property for a
# monorepo — a missed invalidation ships stale artifacts — and the behaviours a
# future Rust port must reproduce exactly.


def test_added_source_propagates_to_glob_consumer(tmp_path):
    """Growing a glob input's membership must re-stale its consumer even though
    no existing file changed — the counterpart to deleted-member cleanup. A
    Rust port that keyed the glob fingerprint only on existing members' hashes
    (not the member set) would leave `combine` stale-fresh; this catches it."""
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()
    assert (tmp_path / "build/all.txt").read_text() == "AABB"

    write_workspace(tmp_path, {"a": "aa", "b": "bb", "c": "cc"})  # add member c
    result = builder.build()
    assert result.success
    assert (tmp_path / "build/all.txt").read_text() == "AABBCC"
    counts = runs(tmp_path)
    assert counts["upper:src/c.txt"] == 1  # new member compiled
    assert counts["upper:src/a.txt"] == 1  # rewritten same-content -> stayed fresh
    assert counts["upper:src/b.txt"] == 1
    assert counts["combine"] == 2  # glob consumer re-staled by the new member

    third = builder.build()  # settled: nothing left to do
    assert third.success
    assert runs(tmp_path) == counts


def test_tool_change_invalidates_everything_it_built(tmp_path):
    """Changing a shared file input (here the upper.py script every `upper`
    action depends on) re-stales every action that used it — the 'compiler
    upgrade invalidates everything it built' property. Yet because the rebuilt
    outputs are byte-identical, the downstream consumer stays fresh: freshness
    is content-based, not timestamp-based. A port that dropped this shared
    input from the fingerprint would leave the uppers stale-fresh (counts
    1, 1); one that used mtimes downstream would spuriously rerun combine."""
    write_workspace(tmp_path, {"a": "aa", "b": "bb"})
    builder = IncrementalBuilder(make_graph, root=tmp_path)
    builder.build()

    (tmp_path / "tools/upper.py").write_text(UPPER_TOOL + "# retuned\n")  # same outputs
    result = builder.build()
    assert result.success
    counts = runs(tmp_path)
    assert counts["upper:src/a.txt"] == 2  # both re-staled by the tool change
    assert counts["upper:src/b.txt"] == 2
    assert counts["combine"] == 1  # outputs unchanged -> downstream stayed fresh


def test_command_line_change_refingerprints(tmp_path):
    """The command template is fingerprint material: changing it re-stales the
    action even when every declared input is byte-identical."""
    from brix import ExternalProcessAction, Graph

    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools/echo.py").write_text(
        "import sys, pathlib\n"
        "pathlib.Path('runs.log').open('a').write('run\\n')\n"
        "pathlib.Path(sys.argv[-1]).write_text(sys.argv[1])\n"
    )

    def graph_with_label(label):
        def supplier():
            graph = Graph()
            data = graph.data
            graph.add(
                ExternalProcessAction(
                    name="gen",
                    command_line="{python} {tool} " + label + " {out}",
                    inputs={
                        "python": data.tool(sys.executable),
                        "tool": data.file("tools/echo.py"),
                    },
                    outputs={"out": data.file("out.txt")},
                )
            )
            return graph

        return supplier

    IncrementalBuilder(graph_with_label("first"), root=tmp_path).build()
    assert (tmp_path / "out.txt").read_text() == "first"
    IncrementalBuilder(graph_with_label("second"), root=tmp_path).build()
    assert (tmp_path / "out.txt").read_text() == "second"  # command change reran it
    IncrementalBuilder(graph_with_label("second"), root=tmp_path).build()
    assert runs(tmp_path)["run"] == 2  # third build was fresh
